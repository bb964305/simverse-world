from __future__ import annotations

import uvicorn

from app.lab.model_gateway.config import GatewayConfig
from app.lab.model_gateway.service import create_app


def main() -> None:
    try:
        config = GatewayConfig.from_env()
    except ValueError as exc:
        raise SystemExit(f"Model gateway configuration error: {exc}") from exc
    uvicorn.run(
        create_app(config),
        host=config.bind_host,
        port=config.bind_port,
        log_level="info",
        workers=1,
    )


if __name__ == "__main__":
    main()
