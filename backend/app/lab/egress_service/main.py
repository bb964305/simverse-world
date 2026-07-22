"""Uvicorn entrypoint for the independent Lab egress service."""
from __future__ import annotations

import os

import uvicorn

from .config import load_egress_config
from .service import create_app


config = load_egress_config()
app = create_app(config)


def main() -> None:
    uvicorn.run(
        app,
        host=os.getenv("LAB_EGRESS_LISTEN_HOST", "0.0.0.0"),
        port=int(os.getenv("LAB_EGRESS_LISTEN_PORT", "8094")),
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
