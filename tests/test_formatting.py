import re

import pytest

from alphamacro.telegram_bot.formatting import markdown_to_telegram_html as to_html
from alphamacro.telegram_bot.formatting import split_message


def test_headings_bold_italic_code():
    out = to_html("## Judul\n**tebal** dan *miring* dan `kode`")
    assert "<b>Judul</b>" in out
    assert "<b>tebal</b>" in out
    assert "<i>miring</i>" in out
    assert "<code>kode</code>" in out


def test_html_is_escaped():
    out = to_html("<script>alert(1)</script> & 5 < 6")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out


def test_code_span_content_not_formatted():
    assert to_html("`**x**`") == "<code>**x**</code>"


def test_links_preserved_and_safe():
    out = to_html("[Sumber](https://a.com/x?a=1&b=2)")
    assert out == '<a href="https://a.com/x?a=1&amp;b=2">Sumber</a>'
    assert "javascript" not in to_html("[x](javascript:alert(1))").replace(
        "[x](javascript:alert(1))", ""
    )


def test_bullets_nested_and_hr():
    out = to_html("- satu\n  - dua\n---")
    assert out.startswith("\u2022 satu")
    assert "\u25e6 dua" in out
    assert "\u2500" in out


def test_table_becomes_pre_without_separator():
    out = to_html("| A | B |\n|---|---|\n| 1 | 2 |")
    assert out == "<pre>A | B\n1 | 2</pre>"


def test_fenced_code():
    assert to_html("```\nx < y\n```") == "<pre>x &lt; y</pre>"


def test_math_asterisks_not_italic():
    assert "<i>" not in to_html("2 * 3 * 4")


def test_no_nul_leaks():
    assert "\x00" not in to_html("a\x00b [l](https://x.com)")


# ---- splitting -------------------------------------------------------
def test_split_short_text_is_single_chunk():
    assert split_message("halo") == ["halo"]


def test_split_empty():
    assert split_message("  \n ") == []


def test_split_respects_limit_and_loses_no_words():
    text = "\n".join(f"baris {i} " + "x" * 50 for i in range(200))
    chunks = split_message(text, limit=500)
    assert all(len(c) <= 500 for c in chunks)
    assert len(chunks) > 1
    assert re.findall(r"baris \d+", "\n".join(chunks)) == re.findall(r"baris \d+", text)


def test_split_hard_wraps_long_line():
    chunks = split_message("kata " * 1000, limit=300)
    assert all(len(c) <= 300 for c in chunks)


def test_split_keeps_code_fences_balanced():
    text = "```\n" + "\n".join(f"line {i}" for i in range(300)) + "\n```"
    chunks = split_message(text, limit=400)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0


def test_split_limit_guard():
    with pytest.raises(ValueError):
        split_message("x", limit=10)
