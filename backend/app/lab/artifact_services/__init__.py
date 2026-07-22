"""Independent Artifact trust-plane service primitives.

The package is intentionally not wired into the Lab Runner at import time.  Each
service must be constructed with an explicit durable store, storage identity,
authentication keyring, and receipt signer.
"""

from app.lab.artifact_services.canonical import canonical_digest, canonical_json_bytes

__all__ = ["canonical_digest", "canonical_json_bytes"]
