import pytest

from alphamacro.grounding import extract_mentions, verify_numeric_grounding

TOOL = (
    "CRYPTO MARKET DATA: Bitcoin (BTC)\nPrice USD: $98,512.34\n24H Change: -2.46%\n"
    "Market Cap: $1,950,000,000,000.00\nRSI 14: 58.32\nRetrieved At: 2026-11-14 10:00:00 WIB"
)


def check(report, tool=TOOL, **kw):
    return verify_numeric_grounding(report, [tool], **kw)


@pytest.mark.parametrize(
    "report",
    [
        "Harga Bitcoin $98,512.34 saat ini.",  # exact (US format)
        "Harga Bitcoin $98.512,34 saat ini.",  # Indonesian separators
        "Harga sekitar $98.5K.",  # rounded with magnitude suffix
        "Harga sekitar 98,5 ribu dolar.",
        "Turun 2.46% dalam 24 jam.",  # sign dropped, direction is in words
        "Market cap $1.95T.",
        "RSI 14 berada di 58.32.",
        "Harga $98.512.",  # thousands separator, no decimals
    ],
)
def test_grounded_numbers_pass(report):
    result = check(report)
    assert result.total >= 1
    assert result.verified == result.total, result.ungrounded


@pytest.mark.parametrize(
    "report",
    [
        "Harga $105,000.",
        "RSI saat ini di 71.2.",
        "Naik 7.5% hari ini.",
        "Market cap $2.5T.",
    ],
)
def test_fabricated_numbers_flagged(report):
    result = check(report)
    assert result.verified == 0
    assert result.ungrounded


def test_ignored_noise():
    report = (
        "1. Ringkasan\n2. Risiko\nPada 14 November 2026 pukul 21:50 WIB, tahun 2026. "
        "Dalam 30 hari terakhir ada 3 skenario. Lihat [CPI 2025](https://x.com/a/123456). "
        "Yield 10Y dan M2 relevan. 2026-11-14."
    )
    assert check(report).total == 0


def test_score_and_listing():
    result = check("Harga $98,512.34 dan RSI 58.32 tetapi target $120,000 dan 9.9%.")
    assert (result.total, result.verified) == (4, 2)
    assert result.score == 0.5
    assert result.ungrounded == ["$120,000", "9.9%"]


def test_no_numbers_means_no_score():
    assert check("Pasar cenderung berhati-hati.").score is None


def test_no_tool_data_means_everything_unverified():
    result = verify_numeric_grounding("Harga $98,512.34", [])
    assert result.verified == 0 and result.score == 0.0


def test_tolerance_is_configurable():
    assert check("Harga $98.900", rel_tol=0.001).verified == 0  # 0.39% away
    assert check("Harga $98.900", rel_tol=0.01).verified == 1
    assert check("Harga $98.600", rel_tol=0.001).verified == 1  # 0.09% away


def test_ambiguous_separator_reads_both_ways():
    candidates = {round(v, 3) for v, _ in extract_mentions("98.500")[0].candidates}
    assert candidates == {98500.0, 98.5}
