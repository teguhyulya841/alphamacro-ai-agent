"""Process entry point: ``alphamacro-api``."""

from __future__ import annotations

import logging
import sys

from alphamacro.config import ApiSettings, Settings
from alphamacro.exceptions import ConfigurationError
from alphamacro.logging_config import configure_logging


def main() -> int:
    try:
        settings = Settings.from_env()
        api_settings = ApiSettings.from_env()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    try:
        import uvicorn
    except ImportError:
        print('The API needs extra packages: pip install -e ".[api]"', file=sys.stderr)
        return 2

    from alphamacro.api.app import create_app
    from alphamacro.engine import AlphaMacroEngine

    configure_logging(
        settings.log_level,
        secrets=settings.secrets(),
        json_logs=settings.log_format == "json",
    )
    app = create_app(AlphaMacroEngine(settings), api_settings)
    logging.getLogger("alphamacro.api").info(
        "API listening on %s:%s (%d key(s) configured)",
        api_settings.host,
        api_settings.port,
        len(api_settings.api_key_hashes),
    )
    uvicorn.run(app, host=api_settings.host, port=api_settings.port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
