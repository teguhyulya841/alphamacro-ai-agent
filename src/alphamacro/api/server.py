"""Process entry point: ``alphamacro-api``."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from alphamacro.config import ApiSettings, Settings, StateSettings
from alphamacro.exceptions import ConfigurationError
from alphamacro.infra.state import build_state
from alphamacro.logging_config import configure_logging


def main() -> int:
    try:
        settings = Settings.from_env()
        api_settings = ApiSettings.from_env()
        state_settings = StateSettings.from_env()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        import uvicorn  # noqa: PLC0415
        from fastapi import FastAPI  # noqa: PLC0415
    except ImportError:
        print('The API needs extra packages: pip install -e ".[api]"', file=sys.stderr)
        return 2

    from alphamacro.api.app import create_app  # noqa: PLC0415
    from alphamacro.engine import AlphaMacroEngine  # noqa: PLC0415

    configure_logging(
        settings.log_level,
        secrets=(*settings.secrets(), *state_settings.secrets()),
        json_logs=settings.log_format == "json",
    )
    state_bundle = build_state(state_settings)
    engine = AlphaMacroEngine(settings, state_bundle=state_bundle)
    app = create_app(engine, api_settings, state_bundle=state_bundle)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await engine.aclose()
            await state_bundle.aclose()

    app.router.lifespan_context = lifespan
    logging.getLogger("alphamacro.api").info(
        "API listening on %s:%s (%d key(s) configured)",
        api_settings.host,
        api_settings.port,
        len(api_settings.api_key_hashes),
    )
    try:
        uvicorn.run(
            app, host=api_settings.host, port=api_settings.port, log_config=None
        )
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
