"""Fail-closed MIME, archive, and external malware scanning policy."""
from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.lab.artifact_services.mime import (
    declared_mime_matches,
    normalize_content_type,
    sniff_mime,
)


_BOUNDED_PARSER_TYPES = frozenset(
    {
        "application/json",
        "application/pdf",
        "application/zip",
        "image/jpeg",
        "image/png",
        "text/csv",
        "text/markdown",
        "text/plain",
    }
)
_DECLARED_TEXT_TYPES = frozenset({"text/csv", "text/markdown", "text/plain"})


@dataclass(frozen=True)
class ScanPolicyConfig:
    policy_version: str
    engine_version: str
    allowed_content_types: frozenset[str]
    malware_command: tuple[str, ...]
    malware_timeout_seconds: float = 60.0
    max_file_bytes: int = 100 * 1024 * 1024
    max_archive_depth: int = 3
    max_archive_files: int = 1_000
    max_archive_expanded_bytes: int = 500 * 1024 * 1024
    max_nested_archive_bytes: int = 32 * 1024 * 1024
    max_archive_ratio: float = 100.0
    parser_timeout_seconds: float = 30.0
    parser_max_memory_bytes: int = 512 * 1024 * 1024
    max_image_pixels: int = 100_000_000
    max_image_decoded_bytes: int = 512 * 1024 * 1024
    max_text_field_bytes: int = 1024 * 1024
    max_csv_columns: int = 4096

    def __post_init__(self) -> None:
        if not self.policy_version or not self.engine_version:
            raise ValueError("scanner policy and engine versions are required")
        if not self.allowed_content_types:
            raise ValueError("scanner allowlist must not be empty")
        normalized_types = frozenset(
            normalize_content_type(value) for value in self.allowed_content_types
        )
        if normalized_types != self.allowed_content_types:
            raise ValueError("scanner allowlist content types must be normalized")
        unsupported_types = normalized_types - _BOUNDED_PARSER_TYPES
        if unsupported_types:
            raise ValueError(
                "scanner allowlist contains content types without bounded parsers: "
                + ", ".join(sorted(unsupported_types))
            )
        if (
            self.malware_timeout_seconds <= 0
            or self.parser_timeout_seconds <= 0
            or self.max_file_bytes <= 0
        ):
            raise ValueError("scanner time and byte limits must be positive")
        if self.max_archive_depth < 0 or self.max_archive_files <= 0:
            raise ValueError("archive depth/count limits are invalid")
        if (
            self.max_archive_expanded_bytes <= 0
            or self.max_nested_archive_bytes <= 0
            or self.max_archive_ratio <= 0
            or self.parser_max_memory_bytes <= 0
            or self.max_image_pixels <= 0
            or self.max_image_decoded_bytes <= 0
            or self.max_text_field_bytes <= 0
            or self.max_csv_columns <= 0
        ):
            raise ValueError("scanner parser limits are invalid")
        if self.malware_command and sum(part.count("{path}") for part in self.malware_command) != 1:
            raise ValueError("malware command must contain exactly one {path} argument")


@dataclass(frozen=True)
class PolicyResult:
    status: Literal["clean", "flagged", "failed"]
    error_code: str | None


class ScanPolicy:
    def __init__(self, config: ScanPolicyConfig) -> None:
        self.config = config

    def ready(self) -> bool:
        if not self.config.malware_command:
            return False
        executable = self.config.malware_command[0]
        return bool(
            Path(executable).is_file() if os.path.isabs(executable) else shutil.which(executable)
        )

    @staticmethod
    def _probe(path: Path) -> tuple[int, str]:
        size = path.stat().st_size
        with path.open("rb") as handle:
            actual_mime = sniff_mime(handle.read(8192))
        return size, actual_mime

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()

    async def _bounded_format_scan(
        self, path: Path, *, mime_type: str
    ) -> PolicyResult | None:
        config = self.config
        worker = Path(__file__).with_name("parser_worker.py")
        command = (
            sys.executable,
            "-I",
            str(worker),
            "--path",
            str(path),
            "--mime-type",
            mime_type,
            "--max-file-bytes",
            str(config.max_file_bytes),
            "--max-archive-depth",
            str(config.max_archive_depth),
            "--max-archive-files",
            str(config.max_archive_files),
            "--max-archive-expanded-bytes",
            str(config.max_archive_expanded_bytes),
            "--max-nested-archive-bytes",
            str(config.max_nested_archive_bytes),
            "--max-archive-ratio",
            str(config.max_archive_ratio),
            "--max-image-pixels",
            str(config.max_image_pixels),
            "--max-image-decoded-bytes",
            str(config.max_image_decoded_bytes),
            "--max-text-field-bytes",
            str(config.max_text_field_bytes),
            "--max-csv-columns",
            str(config.max_csv_columns),
            "--max-memory-bytes",
            str(config.parser_max_memory_bytes),
            "--max-cpu-seconds",
            str(max(1, math.ceil(config.parser_timeout_seconds))),
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except (OSError, ValueError):
            return PolicyResult("failed", "format_parser_unavailable")
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=config.parser_timeout_seconds
            )
        except TimeoutError:
            await self._stop_process(process)
            return PolicyResult("failed", "format_parser_timeout")
        except asyncio.CancelledError:
            await self._stop_process(process)
            raise
        if process.returncode != 0 or len(stdout) > 4096:
            return PolicyResult("failed", "format_parser_failed")
        try:
            result = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return PolicyResult("failed", "format_parser_protocol_error")
        if result == {"status": "valid", "error_code": None}:
            return None
        status = result.get("status") if isinstance(result, dict) else None
        error_code = result.get("error_code") if isinstance(result, dict) else None
        if (
            status not in {"flagged", "failed"}
            or not isinstance(error_code, str)
            or not error_code
            or len(error_code) > 100
        ):
            return PolicyResult("failed", "format_parser_protocol_error")
        return PolicyResult(status, error_code)

    async def _malware_scan(self, path: Path) -> PolicyResult:
        if not self.config.malware_command:
            return PolicyResult("failed", "malware_scanner_unconfigured")
        argv = [part.replace("{path}", str(path)) for part in self.config.malware_command]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                return_code = await asyncio.wait_for(
                    process.wait(), timeout=self.config.malware_timeout_seconds
                )
            except TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
                return PolicyResult("failed", "malware_scanner_timeout")
            except asyncio.CancelledError:
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.wait()
                raise
        except (OSError, ValueError):
            return PolicyResult("failed", "malware_scanner_unavailable")
        if return_code == 0:
            return PolicyResult("clean", None)
        if return_code == 1:
            return PolicyResult("flagged", "malware_detected")
        return PolicyResult("failed", "malware_scanner_error")

    async def scan(self, path: Path, *, declared_content_type: str) -> PolicyResult:
        try:
            size, actual_mime = await asyncio.to_thread(self._probe, path)
            if size > self.config.max_file_bytes:
                return PolicyResult("flagged", "file_size_exceeded")
        except OSError:
            return PolicyResult("failed", "scanner_input_unreadable")
        if not declared_mime_matches(declared_content_type, actual_mime):
            return PolicyResult("flagged", "mime_mismatch")
        declared_mime = normalize_content_type(declared_content_type)
        parser_mime = (
            declared_mime
            if actual_mime == "text/plain" and declared_mime in _DECLARED_TEXT_TYPES
            else actual_mime
        )
        if parser_mime not in self.config.allowed_content_types:
            return PolicyResult("flagged", "mime_not_allowed")
        format_result = await self._bounded_format_scan(path, mime_type=parser_mime)
        if format_result is not None:
            return format_result
        return await self._malware_scan(path)
