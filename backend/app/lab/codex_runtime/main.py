from __future__ import annotations

import uvicorn

from app.lab.codex_runtime.config import CodexRuntimeConfig
from app.lab.codex_runtime.service import create_app


def main() -> None:
    try:
        config = CodexRuntimeConfig.from_env()
    except ValueError as exc:
        raise SystemExit(f"Codex runtime configuration error: {exc}") from exc
    uvicorn.run(
        create_app(config),
        host=config.bind_host,
        port=config.bind_port,
        log_level="info",
        workers=1,
    )


if __name__ == "__main__":
    main()
