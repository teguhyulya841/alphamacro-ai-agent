import json
import logging

import pytest

from alphamacro import cli
from alphamacro.config import BotSettings
from alphamacro.engine import AlphaMacroEngine
from alphamacro.exceptions import MarketDataError
from alphamacro.logging_config import REDACTED, configure_logging
from alphamacro.models import AgentResult
from alphamacro.telegram_bot.bot import build_bot
from alphamacro.tools.coingecko import CoinGeckoClient, build_market_data_tool

SECRET = "sk-super-secret-value"


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:], root.level = handlers, level


def test_text_logs_redact_secrets_including_tracebacks(capsys):
    configure_logging("INFO", secrets=[SECRET])
    log = logging.getLogger("t")
    try:
        raise RuntimeError(f"failed with {SECRET}")
    except RuntimeError:
        log.exception("boom %s", SECRET)
    err = capsys.readouterr().err
    assert SECRET not in err
    assert REDACTED in err


def test_json_logs_are_valid_and_redacted(capsys):
    configure_logging("INFO", secrets=[SECRET], json_logs=True)
    logging.getLogger("t").warning("token=%s", SECRET)
    line = capsys.readouterr().err.strip().splitlines()[-1]
    payload = json.loads(line)
    assert SECRET not in line
    assert payload["level"] == "WARNING"


def test_short_secrets_ignored_to_avoid_mangling_logs(capsys):
    configure_logging("INFO", secrets=["abc"])
    logging.getLogger("t").info("abcdef")
    assert "abcdef" in capsys.readouterr().err


# ---- CLI -------------------------------------------------------------
def test_cli_parse_args():
    args = cli.parse_args(["--json", "-v", "harga", "btc"])
    assert args.json and args.verbose and args.query == ["harga", "btc"]


def test_cli_returns_2_on_missing_config(clean_env, capsys, monkeypatch):
    monkeypatch.setattr("alphamacro.config.load_dotenv", lambda: None)  # ignore a real .env
    assert cli.main(["halo"]) == 2
    assert "Configuration error" in capsys.readouterr().err


def test_cli_runs_query_and_writes_output(settings, tmp_path, capsys, monkeypatch):
    class StubEngine:
        def __init__(self, _settings):
            pass

        def run(self, query):
            return AgentResult(query, "MACRO", "r", "now", "isi laporan")

    out = tmp_path / "r.md"
    monkeypatch.setattr("alphamacro.cli.Settings.from_env", lambda: settings)
    monkeypatch.setattr("alphamacro.cli.AlphaMacroEngine", StubEngine)
    assert cli.main(["-o", str(out), "cpi", "naik"]) == 0
    assert "isi laporan" in capsys.readouterr().out
    assert "isi laporan" in out.read_text(encoding="utf-8")


# ---- Tool wrapper ----------------------------------------------------
def test_market_tool_returns_error_string_instead_of_raising():
    class Failing(CoinGeckoClient):
        def get_market_snapshot(self, coin_id):
            raise MarketDataError("down")

    tool = build_market_data_tool(Failing())
    assert tool.invoke({"coin_id": "bitcoin"}) == "TOOL ERROR: down"


def test_market_tool_formats_snapshot():
    class Ok(CoinGeckoClient):
        def get_market_snapshot(self, coin_id):
            return {"id": coin_id, "name": "Bitcoin", "symbol": "btc", "current_price": 100000}

    out = build_market_data_tool(Ok()).invoke({"coin_id": "bitcoin"})
    assert "Price USD: $100,000.00" in out
    assert "Retrieved At:" in out


# ---- Real wiring smoke test (no network is used) ---------------------
def test_engine_default_wiring_builds(settings):
    engine = AlphaMacroEngine(settings)
    assert [t.name for t in engine.tools] == [
        "get_crypto_market_data",
        "get_technical_indicators",
        "tavily_search",
    ]


def test_build_bot_registers_handlers(settings):
    engine = AlphaMacroEngine(settings)
    bot_settings = BotSettings(telegram_token="123456789:" + "A" * 35)
    bot = build_bot(engine, bot_settings)
    assert len(bot.message_handlers) == 5
