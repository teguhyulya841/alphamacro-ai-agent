"""Shared constants."""

from datetime import timedelta, timezone

WIB = timezone(timedelta(hours=7))

CATEGORIES = ("MACRO", "CRYPTO_MARKET", "TECHNICAL", "FULL_RESEARCH")
DEFAULT_CATEGORY = "FULL_RESEARCH"

MARKET_TOOL_NAME = "get_crypto_market_data"
SEARCH_TOOL_NAME = "tavily_search"
TECHNICAL_TOOL_NAME = "get_technical_indicators"

DISCLAIMER = (
    "*Laporan ini bersifat informasional dan edukatif, dihasilkan oleh sistem AI, "
    "dan bukan merupakan nasihat investasi, keuangan, atau hukum. Aset kripto "
    "berisiko tinggi dan harganya sangat fluktuatif. Verifikasi data pada sumber "
    "primer dan konsultasikan dengan penasihat keuangan berlisensi sebelum "
    "mengambil keputusan.*"
)
