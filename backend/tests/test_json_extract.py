"""Unified JSON extractor tests (P1-1, E-05)."""
from app.llm.json_extract import extract_json_object


def test_plain_object():
    assert extract_json_object('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_object_wrapped_in_prose():
    raw = 'Sure, here it is. {"action": "IDLE", "reason": "rest"} Hope that helps!'
    assert extract_json_object(raw) == {"action": "IDLE", "reason": "rest"}


def test_nested_object_is_captured_fully():
    """The old re.search(r'{[^{}]+}') truncated at the first inner brace."""
    raw = '{"goal": {"goal": "x", "motivation": "y"}, "plans": [{"slot": 0}]}'
    assert extract_json_object(raw) == {
        "goal": {"goal": "x", "motivation": "y"},
        "plans": [{"slot": 0}],
    }


def test_code_fence_json():
    raw = "```json\n{\"mood\": \"positive\", \"summary\": \"nice\"}\n```"
    assert extract_json_object(raw) == {"mood": "positive", "summary": "nice"}


def test_braces_inside_strings_do_not_break_balance():
    raw = '{"text": "use {curly} braces }{ here", "n": 2}'
    assert extract_json_object(raw) == {"text": "use {curly} braces }{ here", "n": 2}


def test_escaped_quotes_inside_strings():
    raw = '{"q": "she said \\"hi\\" today", "ok": true}'
    assert extract_json_object(raw) == {"q": 'she said "hi" today', "ok": True}


def test_trailing_commas_are_tolerated():
    raw = '{"a": 1, "b": [1, 2,], "c": {"d": 3,},}'
    assert extract_json_object(raw) == {"a": 1, "b": [1, 2], "c": {"d": 3}}


def test_array_of_objects_yields_first_inner_object():
    # Call sites all expect an object; if a model wraps one in an array we still
    # recover the first balanced object rather than failing.
    assert extract_json_object('[{"a": 1}, {"b": 2}]') == {"a": 1}


def test_no_json_returns_none():
    assert extract_json_object("no json here at all") is None
    assert extract_json_object("") is None
    assert extract_json_object(None) is None


def test_unbalanced_returns_none():
    assert extract_json_object('{"a": 1, "b": ') is None


def test_first_object_only_when_multiple():
    raw = '{"first": 1} then {"second": 2}'
    assert extract_json_object(raw) == {"first": 1}


def test_target_tile_list_stays_intact():
    raw = 'thinking... {"action": "WANDER", "target_tile": [80, 55], "reason": "roam"}'
    assert extract_json_object(raw) == {
        "action": "WANDER",
        "target_tile": [80, 55],
        "reason": "roam",
    }
