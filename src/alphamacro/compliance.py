"""Input and output guardrails for a financial-research product.

Input: block secrets (API keys, private keys, seed phrases), redact PII, flag prompt
injection. Output: flag guarantee language, personalised buy/sell instructions and
unhedged price targets. All checks are pattern-based heuristics; they reduce risk, they
do not eliminate it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from alphamacro.exceptions import PolicyViolationError

BLOCKED_INPUT_MESSAGE = (
    "Pesan Anda tampaknya berisi kunci rahasia (API key, private key, atau seed "
    "phrase). Demi keamanan, pesan tidak diproses dan tidak dikirim ke penyedia AI. "
    "Hapus pesan itu dari chat dan jangan pernah membagikan kunci tersebut. "
    "Jika sudah terlanjur terbuka, anggap kunci itu bocor dan ganti segera."
)

_SEED_KEYWORDS_RE = re.compile(
    r"seed\s*phrase|mnemonic|recovery\s*phrase|secret\s*recovery|frasa\s*pemulihan|"
    r"kata\s*pemulihan|12\s*kata|24\s*kata|12[- ]word|24[- ]word",
    re.I,
)
_WORD_RUN_RE = re.compile(r"(?:\b[a-z]{3,8}\b[ \t,]+){11,}\b[a-z]{3,8}\b")
_PRIVKEY_KEYWORD_RE = re.compile(
    r"private\s*key|kunci\s*privat|secret\s*key|priv\s*key", re.I
)
_HEX64_RE = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")
_BASE58 = "[1-9A-HJ-NP-Za-km-z]"
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("Tavily API key", re.compile(r"\btvly-[A-Za-z0-9_\-]{16,}\b")),
    ("Secret key (sk-)", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("Telegram bot token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("Bitcoin WIF private key", re.compile(rf"\b[5KL]{_BASE58}{{50,51}}\b")),
    ("Extended private key", re.compile(rf"\bxprv{_BASE58}{{100,}}\b")),
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<!\w)(?:\+62|62|0)8\d{8,11}(?!\d)"
    r"|(?<!\w)\+\d{1,3}[ -]?\d{2,4}[ -]?\d{3,4}[ -]?\d{3,5}(?!\d)"
)
_CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_LONG_ID_RE = re.compile(r"(?<!\d)\d{16}(?!\d)")

_INJECTION_RE = re.compile(
    r"ignore\s+(?:all\s+)?(?:the\s+)?(?:previous|prior|above)\s+instructions|"
    r"disregard\s+(?:all\s+)?(?:previous|prior)|"
    r"abaikan\s+(?:semua\s+)?(?:instruksi|perintah|aturan)|"
    r"(?:reveal|show|print|tampilkan|bocorkan)\s+(?:your\s+|the\s+)?(?:system\s+)?prompt|"
    r"you\s+are\s+now\s+(?:a|an|in)\b|jailbreak|\bDAN\s+mode\b",
    re.I,
)


@dataclass
class InputScan:
    redacted_query: str
    flags: list[str] = field(default_factory=list)


def _luhn_valid(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def scan_input(query: str) -> InputScan:
    """Raise :class:`PolicyViolationError` for secrets.
    otherwise redact PII and flag risks."""
    if _SEED_KEYWORDS_RE.search(query) and _WORD_RUN_RE.search(query):
        raise PolicyViolationError(BLOCKED_INPUT_MESSAGE)
    if _PRIVKEY_KEYWORD_RE.search(query) and _HEX64_RE.search(query):
        raise PolicyViolationError(BLOCKED_INPUT_MESSAGE)
    for _name, pattern in _SECRET_PATTERNS:
        if pattern.search(query):
            raise PolicyViolationError(BLOCKED_INPUT_MESSAGE)

    flags: list[str] = []
    redacted = query
    redacted_kinds: list[str] = []

    def redact_cards(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            if "kartu" not in redacted_kinds:
                redacted_kinds.append("kartu pembayaran")
            return "[KARTU]"
        return match.group(0)

    redacted = _CARD_RE.sub(redact_cards, redacted)
    for kind, pattern, token in (
        ("email", _EMAIL_RE, "[EMAIL]"),
        ("nomor telepon", _PHONE_RE, "[TELEPON]"),
        ("nomor identitas 16 digit", _LONG_ID_RE, "[ID]"),
    ):
        redacted, count = pattern.subn(token, redacted)
        if count:
            redacted_kinds.append(kind)
    if redacted_kinds:
        flags.append(
            "Data pribadi dihapus dari pertanyaan sebelum diproses: "
            + ", ".join(redacted_kinds)
        )
    if _INJECTION_RE.search(query):
        flags.append(
            "Pola prompt-injection terdeteksi pada pertanyaan; diproses dengan "
            "kewaspadaan."
        )
    return InputScan(redacted_query=redacted, flags=flags)


_OUTPUT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "Bahasa jaminan hasil",
        re.compile(
            r"\b(?:pasti\s+(?:naik|turun|untung|profit|cuan)|dijamin|guaranteed|"
            r"tanpa\s+risiko|risk[- ]?free|tidak\s+mungkin\s+rugi|"
            r"100\s*%\s*(?:aman|profit|untung))\b",
            re.I,
        ),
    ),
    (
        "Instruksi beli/jual yang bersifat personal",
        re.compile(
            r"\b(?:segera|harus|wajib|sebaiknya\s+anda)\s+(?:beli|jual|buy|sell)\b|"
            r"\b(?:buy|sell)\s+now\b|\ball[- ]in\b",
            re.I,
        ),
    ),
    (
        "Target harga eksplisit",
        re.compile(
            r"\btarget\s+(?:harga\s+)?(?:price\s+)?(?:sebesar\s+|di\s+)?[$€£]?\s?\d",
            re.I,
        ),
    ),
)


def scan_output(report: str) -> list[str]:
    """Flag risky financial-communication patterns in a generated report."""
    flags: list[str] = []
    for label, pattern in _OUTPUT_RULES:
        match = pattern.search(report)
        if match:
            snippet = re.sub(r"\s+", " ", match.group(0))[:40]
            flags.append(f"{label} terdeteksi: '{snippet}'. Tinjau sebelum dibagikan.")
    return flags
