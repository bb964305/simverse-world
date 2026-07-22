"""Killable, resource-bounded structural parsers for scanner inputs.

This module is executed as an isolated subprocess by ``ScanPolicy``.  It uses
only the standard library so a malformed document cannot block the scanner's
event loop or keep running after the policy timeout.
"""
from __future__ import annotations

import argparse
import codecs
import csv
import io
import json
import math
import re
import stat
import struct
import sys
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO


_CHUNK_BYTES = 64 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_PDF_HEADER = re.compile(br"%PDF-(?:1\.[0-7]|2\.0)(?:\r\n|\r|\n)")
_PDF_STARTXREF = re.compile(br"startxref\s+(\d+)\s*$", re.DOTALL)
_PDF_XREF_OBJECT = re.compile(br"\d+\s+\d+\s+obj(?:\s|<)")


class InvalidFormat(ValueError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class WorkerFailure(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True)
class ParseLimits:
    max_file_bytes: int
    max_archive_depth: int
    max_archive_files: int
    max_archive_expanded_bytes: int
    max_nested_archive_bytes: int
    max_archive_ratio: float
    max_image_pixels: int
    max_image_decoded_bytes: int
    max_text_field_bytes: int
    max_csv_columns: int


@dataclass
class _ArchiveBudget:
    file_count: int = 0
    expanded_bytes: int = 0


def _read_exact(handle: BinaryIO, count: int, error_code: str) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = handle.read(min(remaining, _CHUNK_BYTES))
        if not chunk:
            raise InvalidFormat(error_code)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _consume_exact(handle: BinaryIO, count: int, error_code: str) -> None:
    remaining = count
    while remaining:
        chunk = handle.read(min(remaining, _CHUNK_BYTES))
        if not chunk:
            raise InvalidFormat(error_code)
        remaining -= len(chunk)


def _validate_json(path: Path) -> None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            json.load(handle)
    except UnicodeDecodeError as exc:
        raise InvalidFormat("json_encoding_invalid") from exc
    except json.JSONDecodeError as exc:
        raise InvalidFormat("json_invalid") from exc
    except RecursionError as exc:
        raise InvalidFormat("json_nesting_exceeded") from exc


def _validate_utf8(path: Path) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                if b"\x00" in chunk:
                    raise InvalidFormat("text_nul_byte")
                decoder.decode(chunk, final=False)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError as exc:
        raise InvalidFormat("text_encoding_invalid") from exc


def _validate_csv(path: Path, limits: ParseLimits) -> None:
    _validate_utf8(path)
    previous_limit = csv.field_size_limit()
    csv.field_size_limit(limits.max_text_field_bytes)
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.reader(handle, strict=True):
                if len(row) > limits.max_csv_columns:
                    raise InvalidFormat("csv_column_count_exceeded")
    except csv.Error as exc:
        message = str(exc).lower()
        code = "csv_field_size_exceeded" if "field larger" in message else "csv_invalid"
        raise InvalidFormat(code) from exc
    finally:
        csv.field_size_limit(previous_limit)


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name.replace("\\", "/"))
    return bool(name) and not path.is_absolute() and ".." not in path.parts


def _inspect_zip(
    source: Path | io.BytesIO,
    *,
    limits: ParseLimits,
    depth: int,
    budget: _ArchiveBudget,
) -> None:
    if depth > limits.max_archive_depth:
        raise InvalidFormat("archive_depth_exceeded")
    try:
        with zipfile.ZipFile(source) as archive:
            for info in archive.infolist():
                budget.file_count += 1
                if budget.file_count > limits.max_archive_files:
                    raise InvalidFormat("archive_file_count_exceeded")
                if not _safe_archive_name(info.filename):
                    raise InvalidFormat("archive_path_unsafe")
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    raise InvalidFormat("archive_link_unsupported")
                if info.is_dir():
                    continue
                if info.file_size < 0 or info.compress_size < 0:
                    raise InvalidFormat("archive_entry_size_invalid")
                budget.expanded_bytes += info.file_size
                if budget.expanded_bytes > limits.max_archive_expanded_bytes:
                    raise InvalidFormat("archive_expanded_bytes_exceeded")
                if info.flag_bits & 0x1:
                    raise InvalidFormat("archive_encrypted")
                ratio = (
                    math.inf
                    if info.compress_size == 0 and info.file_size > 0
                    else info.file_size / max(1, info.compress_size)
                )
                if ratio > limits.max_archive_ratio:
                    raise InvalidFormat("archive_ratio_exceeded")

                suffix = PurePosixPath(info.filename).suffix.lower()
                nested_by_name = suffix in {".zip", ".jar", ".docx", ".xlsx", ".pptx"}
                nested_payload: bytearray | None = None
                observed = 0
                with archive.open(info) as member:
                    first = True
                    while chunk := member.read(_CHUNK_BYTES):
                        observed += len(chunk)
                        if observed > info.file_size:
                            raise InvalidFormat("archive_entry_size_mismatch")
                        if first:
                            first = False
                            if nested_by_name or chunk.startswith(_ZIP_SIGNATURES):
                                if info.file_size > limits.max_nested_archive_bytes:
                                    raise InvalidFormat("archive_nested_entry_too_large")
                                nested_payload = bytearray()
                        if nested_payload is not None:
                            nested_payload.extend(chunk)
                if observed != info.file_size:
                    raise InvalidFormat("archive_entry_size_mismatch")
                if nested_by_name and nested_payload is None:
                    nested_payload = bytearray()
                if nested_payload is not None:
                    _inspect_zip(
                        io.BytesIO(nested_payload),
                        limits=limits,
                        depth=depth + 1,
                        budget=budget,
                    )
    except InvalidFormat:
        raise
    except (zipfile.BadZipFile, EOFError, RuntimeError, ValueError, zlib.error) as exc:
        raise InvalidFormat("archive_invalid") from exc


def _validate_pdf(path: Path) -> None:
    size = path.stat().st_size
    if size < 16:
        raise InvalidFormat("pdf_invalid")
    with path.open("rb") as handle:
        header = handle.read(16)
        if _PDF_HEADER.match(header) is None:
            raise InvalidFormat("pdf_header_invalid")
        tail_size = min(size, 64 * 1024)
        handle.seek(size - tail_size)
        tail = handle.read(tail_size)
        eof_index = tail.rfind(b"%%EOF")
        if eof_index < 0:
            raise InvalidFormat("pdf_eof_missing")
        if tail[eof_index + 5 :].strip(b"\x00\x09\x0a\x0c\x0d\x20"):
            raise InvalidFormat("pdf_trailing_data")
        startxref_region = tail[:eof_index].rstrip()
        match = _PDF_STARTXREF.search(startxref_region)
        if match is None:
            raise InvalidFormat("pdf_startxref_missing")
        xref_offset = int(match.group(1))
        if xref_offset <= 0 or xref_offset >= size:
            raise InvalidFormat("pdf_startxref_invalid")
        handle.seek(xref_offset)
        xref_prefix = handle.read(96).lstrip()
        if not (
            xref_prefix.startswith(b"xref")
            or _PDF_XREF_OBJECT.match(xref_prefix) is not None
        ):
            raise InvalidFormat("pdf_xref_invalid")


_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_PNG_BIT_DEPTHS = {
    0: {1, 2, 4, 8, 16},
    2: {8, 16},
    3: {1, 2, 4, 8},
    4: {8, 16},
    6: {8, 16},
}
_ADAM7 = (
    (0, 0, 8, 8),
    (4, 0, 8, 8),
    (0, 4, 4, 8),
    (2, 0, 4, 4),
    (0, 2, 2, 4),
    (1, 0, 2, 2),
    (0, 1, 1, 2),
)


def _pass_size(total: int, start: int, step: int) -> int:
    return 0 if total <= start else (total - start + step - 1) // step


def _png_decoded_bytes(
    width: int, height: int, bit_depth: int, color_type: int, interlace: int
) -> int:
    bits_per_pixel = _PNG_CHANNELS[color_type] * bit_depth
    if interlace == 0:
        return height * (((width * bits_per_pixel + 7) // 8) + 1)
    total = 0
    for x_start, y_start, x_step, y_step in _ADAM7:
        pass_width = _pass_size(width, x_start, x_step)
        pass_height = _pass_size(height, y_start, y_step)
        if pass_width and pass_height:
            total += pass_height * (((pass_width * bits_per_pixel + 7) // 8) + 1)
    return total


def _validate_png(path: Path, limits: ParseLimits) -> None:
    with path.open("rb") as handle:
        if _read_exact(handle, len(_PNG_SIGNATURE), "png_signature_invalid") != _PNG_SIGNATURE:
            raise InvalidFormat("png_signature_invalid")

        saw_ihdr = False
        saw_plte = False
        saw_idat = False
        idat_closed = False
        saw_iend = False
        color_type = -1
        expected_decoded = 0
        decoded_bytes = 0
        decompressor: zlib.Decompress | None = None

        while not saw_iend:
            length_bytes = _read_exact(handle, 4, "png_truncated")
            chunk_length = struct.unpack(">I", length_bytes)[0]
            chunk_type = _read_exact(handle, 4, "png_truncated")
            if not all(65 <= byte <= 90 or 97 <= byte <= 122 for byte in chunk_type):
                raise InvalidFormat("png_chunk_type_invalid")
            if chunk_length > limits.max_file_bytes:
                raise InvalidFormat("png_chunk_size_exceeded")
            if not saw_ihdr and chunk_type != b"IHDR":
                raise InvalidFormat("png_ihdr_missing")
            if saw_idat and chunk_type != b"IDAT":
                idat_closed = True
            if chunk_type == b"IDAT" and idat_closed:
                raise InvalidFormat("png_idat_order_invalid")

            collect = chunk_type == b"IHDR"
            chunk_data = bytearray() if collect else None
            crc = zlib.crc32(chunk_type)
            remaining = chunk_length
            while remaining:
                piece = _read_exact(
                    handle, min(remaining, _CHUNK_BYTES), "png_truncated"
                )
                remaining -= len(piece)
                crc = zlib.crc32(piece, crc)
                if chunk_data is not None:
                    chunk_data.extend(piece)
                if chunk_type == b"IDAT":
                    if decompressor is None:
                        decompressor = zlib.decompressobj()
                    try:
                        output = decompressor.decompress(
                            piece, max(1, expected_decoded - decoded_bytes + 1)
                        )
                    except zlib.error as exc:
                        raise InvalidFormat("png_image_data_invalid") from exc
                    decoded_bytes += len(output)
                    if (
                        decoded_bytes > expected_decoded
                        or decompressor.unconsumed_tail
                        or decompressor.unused_data
                    ):
                        raise InvalidFormat("png_decoded_size_mismatch")
            expected_crc = struct.unpack(
                ">I", _read_exact(handle, 4, "png_truncated")
            )[0]
            if crc & 0xFFFFFFFF != expected_crc:
                raise InvalidFormat("png_crc_invalid")

            if chunk_type == b"IHDR":
                if saw_ihdr or chunk_length != 13 or chunk_data is None:
                    raise InvalidFormat("png_ihdr_invalid")
                width, height, bit_depth, color_type, compression, filtering, interlace = (
                    struct.unpack(">IIBBBBB", chunk_data)
                )
                if (
                    width <= 0
                    or height <= 0
                    or width * height > limits.max_image_pixels
                    or color_type not in _PNG_CHANNELS
                    or bit_depth not in _PNG_BIT_DEPTHS[color_type]
                    or compression != 0
                    or filtering != 0
                    or interlace not in {0, 1}
                ):
                    raise InvalidFormat("png_ihdr_invalid")
                expected_decoded = _png_decoded_bytes(
                    width, height, bit_depth, color_type, interlace
                )
                if expected_decoded > limits.max_image_decoded_bytes:
                    raise InvalidFormat("image_decoded_bytes_exceeded")
                saw_ihdr = True
            elif chunk_type == b"PLTE":
                if saw_plte or saw_idat or chunk_length == 0 or chunk_length % 3:
                    raise InvalidFormat("png_palette_invalid")
                if chunk_length > 768 or color_type in {0, 4}:
                    raise InvalidFormat("png_palette_invalid")
                saw_plte = True
            elif chunk_type == b"IDAT":
                if color_type == 3 and not saw_plte:
                    raise InvalidFormat("png_palette_missing")
                saw_idat = True
            elif chunk_type == b"IEND":
                if chunk_length != 0 or not saw_idat or decompressor is None:
                    raise InvalidFormat("png_iend_invalid")
                try:
                    output = decompressor.flush(
                        max(1, expected_decoded - decoded_bytes + 1)
                    )
                except zlib.error as exc:
                    raise InvalidFormat("png_image_data_invalid") from exc
                decoded_bytes += len(output)
                if not decompressor.eof or decoded_bytes != expected_decoded:
                    raise InvalidFormat("png_decoded_size_mismatch")
                saw_iend = True
            elif not chunk_type[0] & 0x20:
                raise InvalidFormat("png_unknown_critical_chunk")

        if handle.read(1):
            raise InvalidFormat("png_trailing_data")


class _BufferedByteReader:
    def __init__(self, handle: BinaryIO) -> None:
        self.handle = handle
        self.buffer = b""
        self.offset = 0

    def read_byte(self) -> int | None:
        if self.offset >= len(self.buffer):
            self.buffer = self.handle.read(_CHUNK_BYTES)
            self.offset = 0
            if not self.buffer:
                return None
        value = self.buffer[self.offset]
        self.offset += 1
        return value

    def read_exact(self, count: int, error_code: str) -> bytes:
        output = bytearray()
        while len(output) < count:
            value = self.read_byte()
            if value is None:
                raise InvalidFormat(error_code)
            output.append(value)
        return bytes(output)


_JPEG_SOF_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


def _jpeg_next_marker(reader: _BufferedByteReader, *, entropy: bool) -> int:
    while True:
        value = reader.read_byte()
        if value is None:
            raise InvalidFormat("jpeg_eoi_missing")
        if value != 0xFF:
            if entropy:
                continue
            raise InvalidFormat("jpeg_marker_invalid")
        while True:
            marker = reader.read_byte()
            if marker is None:
                raise InvalidFormat("jpeg_truncated")
            if marker != 0xFF:
                break
        if entropy and marker == 0x00:
            continue
        if entropy and 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0x00:
            raise InvalidFormat("jpeg_marker_invalid")
        return marker


def _validate_jpeg(path: Path, limits: ParseLimits) -> None:
    with path.open("rb") as handle:
        reader = _BufferedByteReader(handle)
        if reader.read_exact(2, "jpeg_truncated") != b"\xff\xd8":
            raise InvalidFormat("jpeg_soi_invalid")
        marker = _jpeg_next_marker(reader, entropy=False)
        saw_sof = False
        saw_sos = False
        while marker != 0xD9:
            if marker == 0xD8 or 0xD0 <= marker <= 0xD7:
                raise InvalidFormat("jpeg_marker_order_invalid")
            if marker == 0x01:
                marker = _jpeg_next_marker(reader, entropy=False)
                continue
            length = struct.unpack(">H", reader.read_exact(2, "jpeg_truncated"))[0]
            if length < 2:
                raise InvalidFormat("jpeg_segment_length_invalid")
            payload = reader.read_exact(length - 2, "jpeg_truncated")
            if marker in _JPEG_SOF_MARKERS:
                if saw_sof or len(payload) < 6:
                    raise InvalidFormat("jpeg_frame_invalid")
                precision = payload[0]
                height, width = struct.unpack(">HH", payload[1:5])
                components = payload[5]
                if (
                    precision not in {8, 12, 16}
                    or width <= 0
                    or height <= 0
                    or components <= 0
                    or len(payload) != 6 + 3 * components
                    or width * height > limits.max_image_pixels
                ):
                    raise InvalidFormat("jpeg_frame_invalid")
                saw_sof = True
            elif marker == 0xDA:
                if not saw_sof or len(payload) < 4:
                    raise InvalidFormat("jpeg_scan_header_invalid")
                components = payload[0]
                if components <= 0 or len(payload) != 4 + 2 * components:
                    raise InvalidFormat("jpeg_scan_header_invalid")
                saw_sos = True
                marker = _jpeg_next_marker(reader, entropy=True)
                continue
            marker = _jpeg_next_marker(reader, entropy=False)
        if not saw_sof or not saw_sos:
            raise InvalidFormat("jpeg_structure_incomplete")
        if reader.read_byte() is not None:
            raise InvalidFormat("jpeg_trailing_data")


def _validate(path: Path, mime_type: str, limits: ParseLimits) -> None:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise WorkerFailure("parser_input_unreadable") from exc
    if size < 0 or size > limits.max_file_bytes:
        raise InvalidFormat("file_size_exceeded")
    try:
        if mime_type == "application/json":
            _validate_json(path)
        elif mime_type == "application/zip":
            _inspect_zip(path, limits=limits, depth=0, budget=_ArchiveBudget())
        elif mime_type == "application/pdf":
            _validate_pdf(path)
        elif mime_type == "image/png":
            _validate_png(path, limits)
        elif mime_type == "image/jpeg":
            _validate_jpeg(path, limits)
        elif mime_type == "text/csv":
            _validate_csv(path, limits)
        elif mime_type in {"text/markdown", "text/plain"}:
            _validate_utf8(path)
        else:
            raise InvalidFormat("format_parser_unavailable")
    except OSError as exc:
        raise WorkerFailure("parser_input_unreadable") from exc


def _apply_resource_limits(*, max_memory_bytes: int, max_cpu_seconds: int) -> None:
    try:
        import resource
    except ImportError as exc:  # pragma: no cover - production target is Linux
        raise WorkerFailure("parser_resource_limits_unavailable") from exc
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if sys.platform != "darwin":
            resource.setrlimit(
                resource.RLIMIT_AS, (max_memory_bytes, max_memory_bytes)
            )
        # Python on Darwin cannot lower RLIMIT_AS. Local scans still have parser
        # byte/structure bounds, this CPU limit, and the parent wall-clock limit;
        # production admission remains Linux-only and requires the memory limit.
        resource.setrlimit(
            resource.RLIMIT_CPU, (max_cpu_seconds, max_cpu_seconds + 1)
        )
    except (OSError, ValueError) as exc:
        raise WorkerFailure("parser_resource_limits_unavailable") from exc


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be nonnegative")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive and finite")
    return parsed


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--path", required=True)
    parser.add_argument("--mime-type", required=True)
    parser.add_argument("--max-file-bytes", required=True, type=_positive_int)
    parser.add_argument("--max-archive-depth", required=True, type=_nonnegative_int)
    parser.add_argument("--max-archive-files", required=True, type=_positive_int)
    parser.add_argument(
        "--max-archive-expanded-bytes", required=True, type=_positive_int
    )
    parser.add_argument(
        "--max-nested-archive-bytes", required=True, type=_positive_int
    )
    parser.add_argument("--max-archive-ratio", required=True, type=_positive_float)
    parser.add_argument("--max-image-pixels", required=True, type=_positive_int)
    parser.add_argument(
        "--max-image-decoded-bytes", required=True, type=_positive_int
    )
    parser.add_argument("--max-text-field-bytes", required=True, type=_positive_int)
    parser.add_argument("--max-csv-columns", required=True, type=_positive_int)
    parser.add_argument("--max-memory-bytes", required=True, type=_positive_int)
    parser.add_argument("--max-cpu-seconds", required=True, type=_positive_int)
    return parser.parse_args()


def main() -> None:
    try:
        args = _arguments()
        _apply_resource_limits(
            max_memory_bytes=args.max_memory_bytes,
            max_cpu_seconds=args.max_cpu_seconds,
        )
        _validate(
            Path(args.path),
            args.mime_type,
            ParseLimits(
                max_file_bytes=args.max_file_bytes,
                max_archive_depth=args.max_archive_depth,
                max_archive_files=args.max_archive_files,
                max_archive_expanded_bytes=args.max_archive_expanded_bytes,
                max_nested_archive_bytes=args.max_nested_archive_bytes,
                max_archive_ratio=args.max_archive_ratio,
                max_image_pixels=args.max_image_pixels,
                max_image_decoded_bytes=args.max_image_decoded_bytes,
                max_text_field_bytes=args.max_text_field_bytes,
                max_csv_columns=args.max_csv_columns,
            ),
        )
        result = {"status": "valid", "error_code": None}
    except InvalidFormat as exc:
        result = {"status": "flagged", "error_code": exc.error_code}
    except WorkerFailure as exc:
        result = {"status": "failed", "error_code": exc.error_code}
    except MemoryError:
        result = {"status": "failed", "error_code": "parser_memory_exceeded"}
    except Exception:  # noqa: BLE001 - worker must fail closed without leaking detail
        result = {"status": "failed", "error_code": "format_parser_failed"}
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
