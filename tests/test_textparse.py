from __future__ import annotations

from bagley.llm.textparse import StreamParser, parse_tool_call


def feed_all(parser: StreamParser, chunks: list[str]):
    text, reasoning, calls = "", "", []
    for part in [*(parser.feed(c) for c in chunks), parser.finish()]:
        text += part.text
        reasoning += part.reasoning
        calls += part.tool_calls
    return text, reasoning, calls


def test_think_tags_split_across_chunks():
    text, reasoning, _ = feed_all(
        StreamParser(), ["<thi", "nk>pondering", "</th", "ink>\nHello", " world"]
    )
    assert reasoning == "pondering"
    assert text == "Hello world"


def test_tool_call_split_across_chunks():
    chunks = [
        "Sure. <tool",
        '_call>{"name": "calculate", ',
        '"arguments": {"expression": "2+2"}}</tool_',
        "call>",
    ]
    text, _, calls = feed_all(StreamParser(known_tools={"calculate"}), chunks)
    assert text == "Sure. "
    assert calls[0].name == "calculate"
    assert calls[0].arguments == {"expression": "2+2"}


def test_unknown_tool_is_left_as_text():
    raw = '<tool_call>{"name": "nope", "arguments": {}}</tool_call>'
    text, _, calls = feed_all(StreamParser(known_tools={"calculate"}), [raw])
    assert calls == []
    assert text == raw


def test_unterminated_tool_call_is_recovered():
    _, _, calls = feed_all(
        StreamParser(), ['<tool_call>{"name": "x", "arguments": "{\\"a\\": 1}"}']
    )
    assert calls[0].arguments == {"a": 1}


def test_lone_angle_bracket_is_released():
    text, _, _ = feed_all(StreamParser(), ["a <", "b and 3 < 4"])
    assert text == "a <b and 3 < 4"


def test_tool_parsing_can_be_disabled():
    raw = '<tool_call>{"name": "x", "arguments": {}}</tool_call>'
    text, _, calls = feed_all(StreamParser(parse_tools=False), [raw])
    assert text == raw and calls == []


def test_parse_tool_call_variants():
    assert parse_tool_call('```json\n{"name": "a", "parameters": {"b": 1}}\n```').arguments == {
        "b": 1
    }
    assert parse_tool_call('{"function": {"name": "a", "arguments": {}}}').name == "a"
    assert parse_tool_call("not json") is None
    assert parse_tool_call('["list"]') is None
