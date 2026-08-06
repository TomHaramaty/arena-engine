"""Reading a model's reply when the model added something to it.

The reflection half of this is a real loss, not a hypothetical: on 2026-07-31
`runner/reflect.call_pro` was still parsing with a bare `json.loads`, the model
returned a complete valid reflection followed by trailing content, and
`Extra data: line 40 column 1 (char 5379)` dropped ballast's weekly reflection.
Nothing retries a reflection. The letters had solved this two days earlier and
`reflect` never got the fix — so these tests exist as much to keep the two
callers on one implementation as to defend the parsing.
"""
import json

import pytest

from engine.modelreply import first_json_object

REFLECTION = {"verdicts": [{"decision": "sold V", "quality": "good"}],
              "journal_title": "the day the beat came"}


def test_a_plain_json_reply_parses():
    assert first_json_object(json.dumps(REFLECTION)) == REFLECTION


def test_the_production_failure_trailing_content_after_a_complete_object():
    """The exact shape that cost ballast a week: valid JSON, then more text."""
    reply = json.dumps(REFLECTION) + "\n\nI hope this reflection is useful."
    assert first_json_object(reply) == REFLECTION


def test_the_old_parser_really_did_fail_on_it():
    """Teeth. If json.loads handled this, the fix would be theatre."""
    with pytest.raises(json.JSONDecodeError):
        json.loads(json.dumps(REFLECTION) + "\n\ntrailing")


def test_a_fenced_reply_parses():
    assert first_json_object("```json\n" + json.dumps(REFLECTION) + "\n```") == REFLECTION


def test_prose_before_the_object_does_not_stop_it():
    assert first_json_object("Here is my reflection:\n" + json.dumps(REFLECTION)) == REFLECTION


def test_a_brace_inside_a_string_does_not_end_the_object():
    """The scanner counts braces, so it has to know what a string is."""
    got = first_json_object('{"note": "a } inside a string", "n": {"deep": 1}} tail')
    assert got == {"note": "a } inside a string", "n": {"deep": 1}}


def test_an_escaped_quote_does_not_end_the_string():
    got = first_json_object(r'{"note": "she said \"no\" }"} trailing')
    assert got["note"] == 'she said "no" }'


def test_a_truncated_object_still_raises():
    """The line this tolerance must not cross. Recovering a COMPLETE object
    that arrived with a tail is not the same act as repairing an incomplete
    one, and repairing one is forbidden — a truncated reply is a failed
    reflection and must stay one."""
    with pytest.raises(ValueError):
        first_json_object('{"verdicts": [{"decision": "sold V", "qual')


def test_a_reply_with_no_object_raises():
    with pytest.raises(ValueError):
        first_json_object("I would rather not.")


def test_an_empty_reply_raises():
    with pytest.raises(ValueError):
        first_json_object("")


def test_both_callers_use_this_one_implementation():
    """The bug was not that the parsing was hard. It was that reflect had its
    own answer and letter had another, and only one of them was right."""
    from jobs import letter
    from runner import reflect
    assert letter.first_json_object is first_json_object
    assert reflect.first_json_object is first_json_object
