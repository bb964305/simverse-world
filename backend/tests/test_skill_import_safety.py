import io
import zipfile

import pytest

from app.services.skill_import_service import (
    IMPORT_MAX_LAYER_CHARS,
    IMPORT_MAX_ZIP_MEMBER_BYTES,
    IMPORT_MAX_ZIP_MEMBERS,
    SkillImportValidationError,
    _read_zip_member,
    parse_skill_zip,
    validate_import_identity,
    validate_import_layers,
)


def _archive(entries: dict[str, bytes], *, compression=zipfile.ZIP_STORED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_zip_member_count_is_bounded():
    entries = {f"extra-{i}.txt": b"x" for i in range(IMPORT_MAX_ZIP_MEMBERS + 1)}
    with pytest.raises(SkillImportValidationError) as exc:
        parse_skill_zip(_archive(entries))
    assert exc.value.status_code == 413


def test_zip_total_expansion_is_bounded():
    entries = {
        "ability.md": b"A" * 400_000,
        "extra-a.bin": b"B" * 400_000,
        "extra-b.bin": b"C" * 400_000,
    }
    with pytest.raises(SkillImportValidationError) as exc:
        parse_skill_zip(_archive(entries))
    assert exc.value.status_code == 413


def test_zip_compression_ratio_is_bounded():
    archive = _archive(
        {"ability.md": b"A" * 100_000},
        compression=zipfile.ZIP_DEFLATED,
    )
    with pytest.raises(SkillImportValidationError) as exc:
        parse_skill_zip(archive)
    assert exc.value.status_code == 413
    assert "compression ratio" in exc.value.detail.lower()


def test_zip_member_paths_cannot_traverse():
    with pytest.raises(SkillImportValidationError, match="unsafe member path"):
        parse_skill_zip(_archive({"../ability.md": b"content"}))


class _FakeZipMember:
    def __init__(self, data: bytes):
        self.data = data
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.data[:size]


class _FakeZipFile:
    def __init__(self, member: _FakeZipMember):
        self.member = member

    def open(self, _info, mode: str):
        assert mode == "r"
        return self.member


def test_zip_member_read_caps_actual_decompressed_output():
    member = _FakeZipMember(b"x" * (IMPORT_MAX_ZIP_MEMBER_BYTES + 1))
    info = type("Info", (), {"file_size": 1})()

    with pytest.raises(SkillImportValidationError) as exc:
        _read_zip_member(_FakeZipFile(member), info)

    assert exc.value.status_code == 413
    assert "exceeds" in exc.value.detail.lower()
    assert member.read_sizes == [IMPORT_MAX_ZIP_MEMBER_BYTES + 1]


def test_zip_member_rejects_header_and_actual_size_mismatch():
    member = _FakeZipMember(b"actual")
    info = type("Info", (), {"file_size": 2})()

    with pytest.raises(SkillImportValidationError) as exc:
        _read_zip_member(_FakeZipFile(member), info)

    assert exc.value.status_code == 413
    assert "header" in exc.value.detail.lower()


def test_identity_and_generated_layer_lengths_are_bounded():
    with pytest.raises(SkillImportValidationError, match="Name is too long"):
        validate_import_identity("N" * 101, "valid")
    with pytest.raises(SkillImportValidationError, match="Slug is too long"):
        validate_import_identity("Valid", "s" * 101)
    with pytest.raises(SkillImportValidationError) as exc:
        validate_import_layers({"ability_md": "A" * (IMPORT_MAX_LAYER_CHARS + 1)})
    assert exc.value.status_code == 413
