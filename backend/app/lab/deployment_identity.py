"""Deployment identity advertised by production Lab service health endpoints."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass


_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_SOURCE_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


@dataclass(frozen=True)
class DeploymentIdentity:
    image_digest: str
    source_sha: str

    def __post_init__(self) -> None:
        if _IMAGE_DIGEST.fullmatch(self.image_digest) is None:
            raise ValueError("service image digest must be a pinned sha256 digest")
        if _SOURCE_SHA.fullmatch(self.source_sha) is None:
            raise ValueError("service source SHA must be a full 40 or 64 character digest")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str],
        *,
        image_digest_name: str,
        source_sha_name: str = "LAB_SERVICE_SHA",
    ) -> "DeploymentIdentity":
        return cls(
            image_digest=env.get(image_digest_name, ""),
            source_sha=env.get(source_sha_name, ""),
        )

    def health_fields(self) -> dict[str, str]:
        return {"image_digest": self.image_digest, "sha": self.source_sha}
