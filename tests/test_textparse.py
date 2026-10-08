from __future__ import annotations

from bagley.llm.textparse import StreamParser, parse_tool_call, visible_text


def feed_all(parser: StreamParser, chunks: list[str]):
    text, reasoning, calls = "", "", []
    for part in [*(parser.feed(c) for c in chunks), parser.finish()]:
        if part.retract:
            text, reasoning = "", reasoning + text
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


def test_lone_closing_tag_turns_earlier_text_into_reasoning():
    # Qwen3 thinking models: the template opens <think>, so only the closing tag is printed.
    parser = StreamParser()
    first = parser.feed("Okay, the user wants their specs. ")
    assert first.text == "Okay, the user wants their specs. "
    second = parser.feed("I should call system_info.</th")
    assert second.text == "I should call system_info." and not second.retract
    third = parser.feed("ink>\n\nHere you go.")
    assert third.retract
    assert third.reasoning == ""
    assert third.text == "Here you go."


def test_lone_closing_tag_in_one_chunk():
    text, reasoning, _ = feed_all(StreamParser(), ["Hmm, let me think.</think>\n\nAnswer."])
    assert reasoning == "Hmm, let me think."
    assert text == "Answer."


def test_second_lone_closing_tag_retracts_again():
    text, reasoning, _ = feed_all(StreamParser(), ["a ", "</think>", "b ", "</think>", "c"])
    assert text == "c"
    assert reasoning == "a b "


def test_visible_text_drops_reasoning():
    assert visible_text("<think>x</think>\nTitle") == "Title"
    assert visible_text("pondering...</think>\n\nTitle") == "Title"
    assert visible_text("Plain answer") == "Plain answer"
    assert visible_text("<think>never closed") == ""


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
