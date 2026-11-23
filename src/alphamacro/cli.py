"""Command-line interface."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from alphamacro import __version__
from alphamacro.config import Settings
from alphamacro.engine import AlphaMacroEngine
from alphamacro.exceptions import (
    AgentExecutionError,
    AlphaMacroError,
    ConfigurationError,
    InvalidQueryError,
)
from alphamacro.logging_config import configure_logging
from alphamacro.models import AgentResult

logger = logging.getLogger("alphamacro.cli")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="alphamacro",
        description="AlphaMacro-Crypto: AI-powered macro & crypto research.",
    )
    parser.add_argument("query", nargs="*", help="Question. Omit for interactive mode.")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON.")
    parser.add_argument(
        "-o", "--output", metavar="FILE", help="Also write the report to FILE."
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser.parse_args(argv)


def emit(result: AgentResult, as_json: bool, output: str | None) -> None:
    text = (
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
        if as_json
        else result.render_markdown()
    )
    if output:
        with open(output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        logger.info("Report written to %s", output)
    print(text)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(
        "DEBUG" if args.verbose else settings.log_level,
        secrets=settings.secrets(),
        json_logs=settings.log_format == "json",
    )
    engine = AlphaMacroEngine(settings)

    try:
        if args.query:
            emit(engine.run(" ".join(args.query)), args.json, args.output)
            return 0

        print(f"AlphaMacro-Crypto v{__version__} | model: {settings.gemini_model}")
        print("Type your question ('exit' to quit).\n")
        while True:
            try:
                query = input("You: ").strip()
            except EOFError:
                break
            if query.lower() in {"exit", "quit", "keluar"}:
                break
            if not query:
                continue
            try:
                emit(engine.run(query), args.json, args.output)
            except (InvalidQueryError, AgentExecutionError) as exc:
                print(f"\n[!] {exc}\n", file=sys.stderr)
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    except AlphaMacroError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        logger.exception("Unexpected error")
        return 1
    finally:
        close_engine = getattr(engine, "close", None)
        if callable(close_engine):
            close_engine()


if __name__ == "__main__":
    sys.exit(main())
