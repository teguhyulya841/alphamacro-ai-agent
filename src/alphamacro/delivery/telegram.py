"""Async Telegram Bot API delivery client with aesthetic HTML formatting."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any

import httpx

from alphamacro.constants import DISCLAIMER, WIB
from alphamacro.infra.http import create_async_client
from alphamacro.models import AgentResult, AlertEvent, DeliveryReceipt

logger = logging.getLogger(__name__)

_TELEGRAM_MAX_CHARS = 3800

_SECTION_ICONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ringkasan|eksekutif|summary", re.I), "🧭"),
    (re.compile(r"data pasar|market|harga|price", re.I), "💰"),
    (re.compile(r"teknikal|indikator|technical", re.I), "📈"),
    (re.compile(r"fundamental|makro|macro|sentimen", re.I), "🌐"),
    (re.compile(r"skenario|scenario", re.I), "⚖️"),
    (re.compile(r"risiko|risk", re.I), "⚠️"),
    (re.compile(r"tindakan|action|strategi", re.I), "🎯"),
    (re.compile(r"validasi|multi-venue|kepercayaan|confidence", re.I), "🛡️"),
    (re.compile(r"sumber|source", re.I), "🔗"),
]


def _pick_section_icon(title: str) -> str:
    for pattern, icon in _SECTION_ICONS:
        if pattern.search(title):
            return f"{icon} "
    return "📌 "


def _inline_md_to_html(text: str) -> str:
    """Safely escape HTML entities and convert inline Markdown to Telegram HTML."""
    stashed_links: list[tuple[str, str]] = []

    def _stash_link(match: re.Match[str]) -> str:
        label = html.escape(match.group(1), quote=False)
        url = html.escape(match.group(2), quote=True)
        token = f"\x00LINK{len(stashed_links)}\x00"
        stashed_links.append((token, f'<a href="{url}">{label}</a>'))
        return token

    # 1. Stash markdown links [title](url) before escaping
    working = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", _stash_link, text)

    # 2. Escape remaining raw HTML characters
    working = html.escape(working, quote=False)

    # 3. Inline code `...`
    working = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", working)

    # 4. Bold **...**
    working = re.sub(r"\*\*([^\*\n]+?)\*\*", r"<b>\1</b>", working)

    # 5. Italic *...* (single asterisk not adjacent to another asterisk)
    working = re.sub(r"(?<!\*)\*([^\*\n]+?)\*(?!\*)", r"<i>\1</i>", working)

    # 6. Restore links
    for token, anchor in stashed_links:
        working = working.replace(token, anchor)

    return working


def markdown_to_telegram_html(md_text: str) -> str:
    """Convert raw LLM Markdown report into clean, chat-friendly Telegram HTML."""
    out_lines: list[str] = []

    for raw_line in md_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            continue

        if stripped in {"---", "***", "___"}:
            out_lines.append("━━━━━━━━━━━━━━━━━━━━")
            continue

        # Top-level / H2 / H3 headings -> clean bold headers with icons
        h1_match = re.match(r"^#\s+(.+)$", stripped)
        if h1_match:
            title = _inline_md_to_html(h1_match.group(1).strip())
            out_lines.append(f"📊 <b>{title}</b>")
            continue

        h2_match = re.match(r"^##\s+(.+)$", stripped)
        if h2_match:
            raw_title = h2_match.group(1).strip()
            icon = _pick_section_icon(raw_title)
            title = _inline_md_to_html(raw_title)
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            out_lines.append(f"{icon}<b>{title}</b>")
            continue

        h3_match = re.match(r"^###+\s+(.+)$", stripped)
        if h3_match:
            title = _inline_md_to_html(h3_match.group(1).strip())
            if out_lines and out_lines[-1] != "":
                out_lines.append("")
            out_lines.append(f"▸ <b>{title}</b>")
            continue

        # Bullet lists (* item or - item) -> indented clean bullet dot
        bullet_match = re.match(r"^(\s*)[*-]\s+(.+)$", line)
        if bullet_match:
            indent = "   " if len(bullet_match.group(1)) >= 2 else ""
            content = _inline_md_to_html(bullet_match.group(2).strip())
            out_lines.append(f"{indent}• {content}")
            continue

        # Blockquote > ...
        quote_match = re.match(r"^>\s*(.+)$", stripped)
        if quote_match:
            content = _inline_md_to_html(quote_match.group(1).strip())
            out_lines.append(f"<blockquote>{content}</blockquote>")
            continue

        out_lines.append(_inline_md_to_html(stripped))

    return "\n".join(out_lines).strip()


def _strip_html_tags(text: str) -> str:
    """Fallback plain-text cleaner if Telegram rejects HTML markup."""
    no_tags = re.sub(r"<[^>]+>", "", text)
    return html.unescape(no_tags)


def split_telegram_message(text: str, max_len: int = _TELEGRAM_MAX_CHARS) -> list[str]:
    """Split long messages cleanly on paragraph/line boundaries under Telegram limits."""
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= max_len:
        return [cleaned]

    chunks: list[str] = []
    remaining = cleaned
    while len(remaining) > max_len:
        split_at = remaining.rfind("\n\n", 0, max_len)
        if split_at <= 0:
            split_at = remaining.rfind("\n", 0, max_len)
        if split_at <= 0:
            split_at = remaining.rfind(" ", 0, max_len)
        if split_at <= 0:
            split_at = max_len
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def format_alert_message(event: AlertEvent) -> str:
    """Render an AlertEvent as clean, scannable Telegram HTML."""
    ts_wib = event.triggered_at.astimezone(WIB).strftime("%d %b %Y, %H:%M WIB")
    coin = html.escape(event.coin_id.upper())
    rule = html.escape(event.rule_id)
    op = html.escape(event.operator.value)
    summary = html.escape(event.summary)
    disclaimer_clean = html.escape(DISCLAIMER.strip("*"))

    lines = [
        f"🚨 <b>ALPHA ALERT — {coin}</b>",
        f"🕒 <i>{ts_wib}</i>",
        "",
        f"• <b>Rule:</b> <code>{rule}</code> (<code>{op}</code>)",
        f"• <b>Status:</b> {summary}",
    ]
    if event.cross_validation is not None and event.cross_validation.quotes:
        cv = event.cross_validation
        badge = "✅ KONSISTEN" if cv.is_consistent else "⚠️ DIVERGEN"
        lines.append(
            f"• <b>Multi-Venue:</b> {badge} | Konsensus "
            f"<code>${cv.consensus_price_usd:,.2f}</code> "
            f"(Spread {cv.max_spread_pct:.2f}%)"
        )
    lines.extend(["", "━━━━━━━━━━━━━━━━━━━━", f"<i>{disclaimer_clean}</i>"])
    return "\n".join(lines)


def format_digest_message(result: AgentResult) -> str:
    """Render an AgentResult digest report as aesthetic Telegram HTML."""
    disclaimer_clean = html.escape(DISCLAIMER.strip("*"))
    conf_badge = ""
    if result.confidence is not None:
        lvl = result.confidence.level.value
        emoji = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}.get(lvl, "⚪")
        conf_badge = (
            f" | {emoji} Confidence: <b>{lvl}</b> ({result.confidence.score:.0%})"
        )

    header = [
        "📊 <b>ALPHA INTELLIGENCE BRIEF</b>",
        f"🕒 <i>{html.escape(result.analysis_time)}</i> • Mode: <code>{html.escape(result.category)}</code>{conf_badge}",
        "━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    body_html = markdown_to_telegram_html(result.report.strip())

    footer: list[str] = ["", "━━━━━━━━━━━━━━━━━━━━"]
    if result.cross_validations:
        for cv in result.cross_validations:
            badge = "✅" if cv.is_consistent else "⚠️"
            footer.append(
                f"🛡️ <b>Validasi {html.escape(cv.coin_id.upper())}:</b> {badge} "
                f"Konsensus <code>${cv.consensus_price_usd:,.2f}</code> "
                f"(Spread {cv.max_spread_pct:.2f}%)"
            )
    if result.sources:
        top_sources = result.sources[:4]
        links = " • ".join(
            f'<a href="{html.escape(s.url, quote=True)}">{html.escape(s.title[:38])}</a>'
            for s in top_sources
        )
        footer.append(f"🔗 <b>Sumber:</b> {links}")

    footer.extend(["", f"<i>{disclaimer_clean}</i>"])
    return "\n".join(header) + body_html + "\n".join(footer)


class TelegramDeliveryClient:
    """Async Telegram Bot API client that never leaks the bot token in errors."""

    BASE_URL = "https://api.telegram.org"

    def __init__(
        self,
        bot_token: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = 15.0,
        max_retries: int = 2,
    ) -> None:
        token = (bot_token or "").strip()
        if not token:
            raise ValueError("Telegram bot token tidak boleh kosong.")
        self._token = token
        self._max_retries = max_retries
        self._owns_client = http_client is None
        self._client = (
            http_client
            if http_client is not None
            else create_async_client(base_url=self.BASE_URL, timeout_s=timeout_s)
        )

    async def _post_chunk(
        self,
        chat_id: str,
        text: str,
        parse_mode: str | None,
    ) -> None:
        path = f"/bot{self._token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.post(path, json=payload)
                if response.status_code == 400 and parse_mode is not None:
                    fallback_payload = dict(payload)
                    fallback_payload.pop("parse_mode", None)
                    fallback_payload["text"] = _strip_html_tags(text)
                    fallback_resp = await self._client.post(path, json=fallback_payload)
                    fallback_resp.raise_for_status()
                    return
                if (
                    response.status_code in {429, 500, 502, 503, 504}
                    and attempt < self._max_retries
                ):
                    await asyncio.sleep(0.3 * (2**attempt))
                    continue
                response.raise_for_status()
                return
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                raise RuntimeError(
                    f"Telegram API mengembalikan HTTP {status}."
                ) from None
            except httpx.RequestError as exc:
                if attempt >= self._max_retries:
                    raise RuntimeError(
                        f"Gagal terhubung ke Telegram API ({type(exc).__name__})."
                    ) from None
                await asyncio.sleep(0.3 * (2**attempt))

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        parse_mode: str | None = "HTML",
    ) -> DeliveryReceipt:
        """Send formatted message to ``chat_id``, auto-splitting long reports."""
        target = str(chat_id).strip()
        if not target:
            return DeliveryReceipt(
                channel="telegram",
                target="",
                delivered=False,
                chunks_sent=0,
                error="chat_id tidak boleh kosong.",
            )
        chunks = split_telegram_message(text)
        if not chunks:
            return DeliveryReceipt(
                channel="telegram",
                target=target,
                delivered=False,
                chunks_sent=0,
                error="Pesan kosong.",
            )

        sent = 0
        try:
            for chunk in chunks:
                await self._post_chunk(target, chunk, parse_mode=parse_mode)
                sent += 1
            return DeliveryReceipt(
                channel="telegram",
                target=target,
                delivered=True,
                chunks_sent=sent,
            )
        except Exception as exc:
            logger.warning(
                "Telegram delivery failed target=%s sent=%d/%d: %s",
                target,
                sent,
                len(chunks),
                exc,
            )
            return DeliveryReceipt(
                channel="telegram",
                target=target,
                delivered=False,
                chunks_sent=sent,
                error=str(exc),
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
