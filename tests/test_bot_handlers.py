from types import SimpleNamespace

import pytest
import requests
from telebot.apihelper import ApiTelegramException

from alphamacro.config import BotSettings
from alphamacro.exceptions import InvalidQueryError
from alphamacro.models import AgentResult, Source
from alphamacro.telegram_bot.handlers import BotController
from alphamacro.telegram_bot.throttle import (
    ActiveUserRegistry,
    SlidingWindowRateLimiter,
)


class FakeBot:
    def __init__(self, fail_html_once=False):
        self.sent, self.deleted, self.actions = [], [], []
        self._fail_html_once = fail_html_once

    def send_message(self, chat_id, text, **kwargs):
        if (
            self._fail_html_once
            and kwargs.get("parse_mode") == "HTML"
            and "<b>" in text
        ):
            self._fail_html_once = False
            raise ApiTelegramException(
                "sendMessage", None,
                {"error_code": 400, "description": "Bad Request: can't parse entities"},
            )  # fmt: skip
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(self.sent))

    def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    def send_chat_action(self, chat_id, action):
        self.actions.append((chat_id, action))


class FakeEngine:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def run(self, query, **_kwargs):
        self.calls.append(query)
        if self.error:
            raise self.error
        return AgentResult(
            "q", "MACRO", "r", "now", "## Ringkasan\n**BTC** stabil",
            sources=[Source("CG", "https://www.coingecko.com")],
        )  # fmt: skip


def make_message(text="harga btc", user_id=42, chat_type="private"):
    return SimpleNamespace(
        chat=SimpleNamespace(id=user_id, type=chat_type),
        from_user=SimpleNamespace(id=user_id),
        text=text,
    )


def controller(engine=None, bot=None, allowed=frozenset(), rate=5):
    settings = BotSettings(
        telegram_token="x",
        allowed_user_ids=frozenset(allowed),
        rate_limit_per_minute=rate,
    )
    bot = bot or FakeBot()
    engine = engine or FakeEngine()
    ctrl = BotController(
        bot, engine, settings, max_query_chars=100,
        limiter=SlidingWindowRateLimiter(rate), active_users=ActiveUserRegistry(),
        sleep=lambda _s: None,
    )  # fmt: skip
    return ctrl, bot, engine


def texts(bot):
    return [t for _, t, _ in bot.sent]


def test_success_flow_sends_html_and_cleans_placeholder():
    ctrl, bot, engine = controller()
    ctrl.on_text(make_message())
    assert engine.calls == ["harga btc"]
    assert bot.deleted  # placeholder removed
    report = [t for t in texts(bot) if "Ringkasan" in t][0]
    assert "<b>Ringkasan</b>" in report
    assert any(kw.get("parse_mode") == "HTML" for _, _, kw in bot.sent)
    assert any("Sumber" in t for t in texts(bot))


def test_internal_errors_are_not_leaked():
    ctrl, bot, _ = controller(
        engine=FakeEngine(error=RuntimeError("api_key=SECRET123"))
    )
    ctrl.on_text(make_message())
    message = texts(bot)[-1]
    assert "SECRET123" not in message
    assert "RuntimeError" not in message
    assert "Kode:" in message


def test_invalid_query_message_is_shown():
    ctrl, bot, _ = controller(
        engine=FakeEngine(error=InvalidQueryError("Pertanyaan kosong <x>"))
    )
    ctrl.on_text(make_message())
    assert "Pertanyaan kosong &lt;x&gt;" in texts(bot)[-1]


def test_unauthorized_user_blocked():
    ctrl, bot, engine = controller(allowed={1})
    ctrl.on_text(make_message(user_id=42))
    assert engine.calls == []
    assert "tidak memiliki akses" in texts(bot)[-1]


def test_allowed_user_passes():
    ctrl, _, engine = controller(allowed={42})
    ctrl.on_text(make_message(user_id=42))
    assert engine.calls


def test_rate_limit_enforced():
    ctrl, bot, engine = controller(rate=1)
    ctrl.on_text(make_message())
    ctrl.on_text(make_message())
    assert len(engine.calls) == 1
    assert "Terlalu banyak" in texts(bot)[-1]


def test_concurrent_request_from_same_user_rejected():
    ctrl, bot, engine = controller()
    ctrl._active.try_acquire(42)
    ctrl.on_text(make_message())
    assert engine.calls == []
    assert "masih diproses" in texts(bot)[-1]


def test_query_too_long_rejected():
    ctrl, bot, engine = controller()
    ctrl.on_text(make_message(text="x" * 101))
    assert engine.calls == []
    assert "terlalu panjang" in texts(bot)[-1]


def test_group_messages_ignored():
    ctrl, bot, engine = controller()
    ctrl.on_text(make_message(chat_type="group"))
    assert engine.calls == [] and bot.sent == []


def test_html_rejection_falls_back_to_plain_text():
    ctrl, bot, _ = controller(bot=FakeBot(fail_html_once=True))
    ctrl.on_text(make_message())
    plain = [kw for _, t, kw in bot.sent if "Ringkasan" in t and "parse_mode" not in kw]
    assert plain, "expected a plain-text resend"


@pytest.mark.parametrize(
    "handler", ["on_start", "on_about", "on_unknown_command", "on_unsupported"]
)
def test_static_handlers_reply_in_private(handler):
    ctrl, bot, _ = controller()
    getattr(ctrl, handler)(make_message())
    assert len(bot.sent) == 1


class FlakyBot(FakeBot):
    """Fails the first N send_message calls with a connection error."""

    def __init__(self, failures):
        super().__init__()
        self.failures = failures

    def send_message(self, chat_id, text, **kwargs):
        if "Menganalisis" not in text and self.failures > 0:
            self.failures -= 1
            raise requests.exceptions.ConnectionError("reset")
        return super().send_message(chat_id, text, **kwargs)


def test_delivery_retries_transient_network_errors():
    ctrl, bot, _ = controller(bot=FlakyBot(failures=2))
    ctrl.on_text(make_message())
    assert any("Ringkasan" in t for t in texts(bot))


def test_delivery_gives_up_quietly_after_retries(caplog):
    ctrl, bot, _ = controller(bot=FlakyBot(failures=99))
    ctrl.on_text(make_message())  # must not raise
    assert not any("Ringkasan" in t for t in texts(bot))
    assert "Telegram unreachable" in caplog.text
