"""Convert LLM Markdown into Telegram-safe HTML and split long messages.

Telegram's legacy Markdown mode rejects headings, tables and many LLM constructs, so the
bot renders a conservative subset to HTML instead (bold, italic, strike, code, pre, links,
bullets, quotes). Anything unsupported degrades to readable plain text.
"""

from __future__ import annotations

import html
import re

TELEGRAM_MESSAGE_LIMIT = 4096
DEFAULT_CHUNK_SIZE = 3800  # headroom below Telegram's hard limit

_FENCE_RE = re.compile(r"^\s*```")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+\u2022]\s+(.*)$")
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_CELL_RE = re.compile(r"^:?-+:?$")
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_LINK_RE = re.compile(r'\[([^\]\n]+)\]\((https?://[^)\s"<>]+)\)')
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC_RE = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _format_text(text: str) -> str:
    """Escape, then apply inline formatting (links are stashed to protect their URLs)."""
    escaped = _escape(text)
    links: list[tuple[str, str]] = []

    def stash(match: re.Match[str]) -> str:
        links.append((match.group(1), match.group(2)))
        return f"\x00{len(links) - 1}\x00"

    escaped = _LINK_RE.sub(stash, escaped)
    escaped = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    escaped = _ITALIC_RE.sub(r"<i>\1</i>", escaped)
    escaped = _STRIKE_RE.sub(r"<s>\1</s>", escaped)

    def restore(match: re.Match[str]) -> str:
        label, url = links[int(match.group(1))]
        return f'<a href="{url}">{label}</a>'

    return _PLACEHOLDER_RE.sub(restore, escaped)


def _format_inline(text: str) -> str:
    """Handle inline code first so its content is never formatted."""
    parts = _CODE_SPAN_RE.split(text)
    out: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            out.append(f"<code>{_escape(part)}</code>")
        else:
            out.append(_format_text(part))
    return "".join(out)


def _render_table(rows: list[str]) -> str:
    rendered: list[str] = []
    for row in rows:
        cells = [c.strip() for c in row.strip().strip("|").split("|")]
        if cells and all(_TABLE_SEP_CELL_RE.match(c) for c in cells):
            continue
        rendered.append(" | ".join(c.replace("**", "").replace("`", "") for c in cells))
    return f"<pre>{_escape(chr(10).join(rendered))}</pre>"


def markdown_to_telegram_html(text: str) -> str:
    """Render a Markdown subset to Telegram-supported HTML."""
    lines = text.replace("\r\n", "\n").replace("\x00", "").split("\n")
    out: list[str] = []
    fence: list[str] = []
    in_fence = False
    i = 0

    def flush_fence() -> None:
        if fence:
            out.append(f"<pre>{_escape(chr(10).join(fence))}</pre>")
        fence.clear()

    while i < len(lines):
        line = lines[i]

        if _FENCE_RE.match(line):
            if in_fence:
                flush_fence()
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            fence.append(line)
            i += 1
            continue

        if _TABLE_ROW_RE.match(line):
            rows: list[str] = []
            while i < len(lines) and _TABLE_ROW_RE.match(lines[i]):
                rows.append(lines[i])
                i += 1
            out.append(_render_table(rows))
            continue

        if heading := _HEADING_RE.match(line):
            out.append(f"<b>{_format_inline(heading.group(1).replace('**', ''))}</b>")
        elif _HR_RE.match(line):
            out.append("\u2500" * 10)
        elif bullet := _BULLET_RE.match(line):
            depth = len(bullet.group(1).replace("\t", "  ")) // 2
            marker = "\u2022" if depth == 0 else "\u25e6"
            out.append(f"{'  ' * depth}{marker} {_format_inline(bullet.group(2))}")
        elif quote := _QUOTE_RE.match(line):
            out.append(f"<blockquote>{_format_inline(quote.group(1))}</blockquote>")
        else:
            out.append(_format_inline(line))
        i += 1

    if in_fence:
        flush_fence()

    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _hard_wrap(line: str, limit: int) -> list[str]:
    """Split a single over-long line, preferring spaces."""
    if len(line) <= limit:
        return [line]
    pieces: list[str] = []
    rest = line
    while len(rest) > limit:
        cut = rest.rfind(" ", 0, limit)
        cut = cut if cut > limit // 2 else limit
        pieces.append(rest[:cut])
        rest = rest[cut:].lstrip(" ")
    pieces.append(rest)
    return pieces


def split_message(text: str, limit: int = DEFAULT_CHUNK_SIZE) -> list[str]:
    """Split Markdown into chunks <= ``limit`` chars, keeping code fences balanced."""
    if limit < 100:
        raise ValueError("limit must be >= 100")
    text = text.strip()
    if not text:
        return []

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    in_fence = False

    def flush() -> None:
        nonlocal current, size
        if current and current != ["```"]:
            body = "\n".join(current).strip("\n")
            if in_fence:
                body += "\n```"
            if body.strip():
                chunks.append(body)
        current = ["```"] if in_fence else []
        size = sum(len(item) + 1 for item in current)

    for raw_line in text.split("\n"):
        for piece in _hard_wrap(raw_line, limit - 8):
            is_fence = bool(_FENCE_RE.match(piece))
            if size + len(piece) + 1 > limit and current:
                flush()
            if is_fence and in_fence and current == ["```"]:
                # Closing fence right after a re-opened one: drop the empty block.
                current, size, in_fence = [], 0, False
                continue
            current.append(piece)
            size += len(piece) + 1
            if is_fence:
                in_fence = not in_fence
    flush()
    return chunks
