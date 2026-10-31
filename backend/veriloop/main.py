"""`python -m veriloop` / `veriloop` console script: run the API with uvicorn.

Startup failures (e.g. `model_resolution_failed`) make uvicorn exit with status 3, satisfying the
fail-fast rule (D-01). Logging is configured before uvicorn starts so its own lines are JSON too.
"""

from __future__ import annotations

import argparse

import uvicorn

from veriloop.core.config import get_settings
from veriloop.core.logging import configure_logging, register_secrets


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="veriloop", description="Run the VeriLoop API server.")
    parser.add_argument("--host", default=settings.host)
    parser.add_argument("--port", type=int, default=settings.port)
    parser.add_argument("--log-level", default=settings.log_level)
    args = parser.parse_args(argv)

    configure_logging(args.log_level, json_logs=settings.log_json)
    register_secrets(settings.secret_values())
    uvicorn.run(
        "veriloop.api.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_config=None,  # keep our structlog/JSON root handler
        log_level=args.log_level.lower(),
        lifespan="on",
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
