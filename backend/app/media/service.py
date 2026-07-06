"""Media upload service: validate, save, and resolve uploaded files."""
import uuid
from pathlib import Path
from fastapi import UploadFile
from app.config import settings


ALLOWED_IMAGE_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

ALLOWED_VIDEO_TYPES = {
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
}


class MediaValidationError(ValueError):
    """Raised when uploaded media fails validation."""
    pass


def sniff_image_type(content: bytes) -> str | None:
    """Detect image content-type from magic bytes. Returns None if unrecognized."""
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def sniff_video_type(content: bytes) -> str | None:
    """Detect video content-type from magic bytes. Returns None if unrecognized."""
    # ISO base media (mp4/mov): size(4) + 'ftyp' + major brand(4)
    if len(content) >= 12 and content[4:8] == b"ftyp":
        if content[8:12] == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    # EBML header (webm/matroska)
    if content.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return None


class MediaService:
    """Handles saving and resolving uploaded media files."""

    def __init__(
        self,
        upload_base: str | None = None,
        max_image_size: int | None = None,
        max_video_size: int | None = None,
    ):
        self.upload_base = Path(upload_base or settings.media_upload_dir)
        self.max_image_size = max_image_size or settings.media_max_image_size
        self.max_video_size = max_video_size or settings.media_max_video_size

    async def save_upload(self, file: UploadFile, media_type: str) -> dict:
        """Validate and save an uploaded file. Returns media_url, media_type, filename.

        Args:
            file: FastAPI UploadFile object.
            media_type: "image" or "video".

        Returns:
            dict with keys: media_url (str), media_type (str), filename (str)

        Raises:
            MediaValidationError: if file is too large or unsupported type.
        """
        content = await file.read()
        size = len(content)

        if media_type == "image":
            if size > self.max_image_size:
                raise MediaValidationError(
                    f"Image too large: {size} bytes (max {self.max_image_size})"
                )
            content_type = file.content_type or ""
            if content_type not in ALLOWED_IMAGE_TYPES:
                raise MediaValidationError(
                    f"Unsupported image type: {content_type!r}. "
                    f"Allowed: {', '.join(ALLOWED_IMAGE_TYPES)}"
                )
            # Don't trust the declared content-type: sniff magic bytes (P0-4d)
            sniffed = sniff_image_type(content)
            if sniffed is None or sniffed not in ALLOWED_IMAGE_TYPES:
                raise MediaValidationError(
                    "File content does not match a supported image format"
                )
            ext = ALLOWED_IMAGE_TYPES[sniffed]
            subdir = "images"
        elif media_type == "video":
            if size > self.max_video_size:
                raise MediaValidationError(
                    f"Video too large: {size} bytes (max {self.max_video_size})"
                )
            content_type = file.content_type or ""
            if content_type not in ALLOWED_VIDEO_TYPES:
                raise MediaValidationError(
                    f"Unsupported video type: {content_type!r}. "
                    f"Allowed: {', '.join(ALLOWED_VIDEO_TYPES)}"
                )
            # Don't trust the declared content-type: sniff magic bytes (P0-4d)
            sniffed = sniff_video_type(content)
            if sniffed is None or sniffed not in ALLOWED_VIDEO_TYPES:
                raise MediaValidationError(
                    "File content does not match a supported video format"
                )
            ext = ALLOWED_VIDEO_TYPES[sniffed]
            subdir = "videos"
        else:
            raise MediaValidationError(f"Unknown media_type: {media_type!r}")

        filename = f"{uuid.uuid4()}{ext}"
        dest_dir = self.upload_base / subdir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / filename

        dest_path.write_bytes(content)

        media_url = f"/static/uploads/{subdir}/{filename}"
        return {
            "media_url": media_url,
            "media_type": media_type,
            "filename": filename,
        }

    def get_file_path(self, media_url: str) -> Path:
        """Resolve a media_url (e.g. /static/uploads/images/abc.jpg) to an absolute Path.

        Strips the /static/uploads/ prefix and resolves relative to upload_base.
        """
        # Strip leading /static/uploads/
        relative = media_url.removeprefix("/static/uploads/")
        return self.upload_base / relative
