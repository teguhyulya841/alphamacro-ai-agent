import json

import pytest

from alphamacro import parsing
from alphamacro.constants import MARKET_TOOL_NAME, SEARCH_TOOL_NAME
from alphamacro.exceptions import InvalidQueryError

from .conftest import make_message as msg


def test_sanitize_rejects_empty():
    with pytest.raises(InvalidQueryError):
        parsing.sanitize_query("   ", 100)


def test_sanitize_rejects_too_long():
    with pytest.raises(InvalidQueryError):
        parsing.sanitize_query("a" * 101, 100)


def test_sanitize_strips_control_chars():
    assert parsing.sanitize_query("harga\x00  BTC\x07", 100) == "harga BTC"


def test_content_to_text_handles_blocks():
    blocks = [{"type": "text", "text": "halo"}, {"type": "image"}, "dunia"]
    assert parsing.content_to_text(blocks) == "halo\ndunia"


def test_extract_final_answer_skips_empty_ai():
    messages = [msg("ai", "jawaban"), msg("tool", "x", "t"), msg("ai", "")]
    assert parsing.extract_final_answer(messages) == "jawaban"


def test_sources_deduplicated_and_validated():
    tavily = json.dumps(
        {
            "results": [
                {"title": "A", "url": "https://a.com/1"},
                {"title": "A dup", "url": "https://a.com/1"},
                {"title": "bad", "url": "javascript:alert(1)"},
            ]
        }
    )
    messages = [
        msg("tool", tavily, SEARCH_TOOL_NAME),
        msg("tool", "CRYPTO MARKET DATA: Bitcoin", MARKET_TOOL_NAME),
    ]
    tools, sources = parsing.inspect_tool_activity(messages)
    assert tools == [SEARCH_TOOL_NAME, MARKET_TOOL_NAME]
    assert [s.url for s in sources] == ["https://a.com/1", "https://www.coingecko.com"]


def test_failed_market_tool_is_not_a_source():
    _, sources = parsing.inspect_tool_activity(
        [msg("tool", "TOOL ERROR: x", MARKET_TOOL_NAME)]
    )
    assert sources == []


def test_malformed_tavily_payload_ignored():
    _, sources = parsing.inspect_tool_activity(
        [msg("tool", "not json", SEARCH_TOOL_NAME)]
    )
    assert sources == []


def test_warnings():
    assert parsing.build_quality_warnings("MACRO", [])
    assert parsing.build_quality_warnings("CRYPTO_MARKET", [SEARCH_TOOL_NAME])
    assert not parsing.build_quality_warnings("CRYPTO_MARKET", [MARKET_TOOL_NAME])
    assert not parsing.build_quality_warnings("MACRO", [SEARCH_TOOL_NAME])
