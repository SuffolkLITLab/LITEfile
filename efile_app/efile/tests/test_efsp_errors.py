"""Tests for turning an EFSP rejection into a message a filer can act on.

The body in ``WRONG_FILING_TYPE`` is the real one the fee endpoint returned for a
document saved with no filing type, which reached the filer as "Filing
submission failed: API returned status 400".
"""

import json

import pytest

from efile.services.efsp_errors import _actionable_hint, describe_efsp_error

WRONG_FILING_TYPE = {
    "required_vars": [],
    "optional_vars": [
        {
            "name": "efile_case_subtype",
            "description": "subtype (not always present)",
            "datatype": "text",
            "currentVal": "",
            "choices": [],
        }
    ],
    "wrong_vars": [
        {
            "name": "al_court_bundle.elements[0].filing_type",
            "description": "What filing type is this??",
            "datatype": "choice",
            "currentVal": "",
            "choices": ["60384", "60409", "123566"],
        }
    ],
}


class FakeResponse:
    """Enough of requests.Response for the describer."""

    def __init__(self, status_code, body=None, text=None):
        self.status_code = status_code
        self._body = body
        self.text = text if text is not None else json.dumps(body)

    def json(self):
        if self._body is None:
            raise json.JSONDecodeError("Expecting value", self.text, 0)
        return self._body


def test_wrong_var_names_the_field_and_the_document():
    message = describe_efsp_error(FakeResponse(400, WRONG_FILING_TYPE))

    assert "filing code" in message
    assert "document 1" in message
    assert "400" not in message


def test_optional_vars_are_not_reported_as_problems():
    """The same body lists an empty optional field; saying so would only confuse."""
    assert "subtype" not in describe_efsp_error(FakeResponse(400, WRONG_FILING_TYPE))


def test_a_wrong_value_is_quoted_rather_than_called_missing():
    body = {"wrong_vars": [{"name": "al_court_bundle.elements[1].filing_type", "currentVal": "99999"}]}

    message = describe_efsp_error(FakeResponse(400, body))

    assert "99999" in message
    assert "document 2" in message


def test_required_var_is_reported_as_missing():
    message = describe_efsp_error(FakeResponse(400, {"required_vars": [{"name": "efile_case_type"}]}))

    assert "no case type was given" in message


def test_live_other_party_address_error_says_which_party_and_field_to_fix():
    body = {
        "wrong_vars": [
            {
                "name": "other_parties[0].address.state",
                "description": ": no match found",
                "currentVal": "",
            }
        ]
    }

    message = describe_efsp_error(FakeResponse(400, body))

    assert "state is required" in message
    assert "other party 1" in message


def test_several_problems_are_all_reported():
    body = {
        "wrong_vars": [
            {"name": "al_court_bundle.elements[0].filing_type", "currentVal": ""},
            {"name": "al_court_bundle.elements[1].filing_component", "currentVal": ""},
        ]
    }

    message = describe_efsp_error(FakeResponse(400, body))

    assert "filing code" in message
    assert "filing component" in message


def test_unmapped_field_name_is_still_readable():
    message = describe_efsp_error(FakeResponse(400, {"required_vars": [{"name": "lead_contact"}]}))

    assert "lead contact" in message


def test_plain_error_message_is_passed_through():
    body = {"error": "All required parties not covered by existing party types."}

    assert describe_efsp_error(FakeResponse(400, body)) == body["error"]


def test_validation_errors_are_appended_to_a_plain_message():
    body = {"error": "Rejected", "validation_errors": ["bad bundle"]}

    message = describe_efsp_error(FakeResponse(400, body))

    assert "Rejected" in message
    assert "bad bundle" in message


def test_malformed_interview_description_is_surfaced():
    """The EFSP's "Malformed Interview" shape has no error/message/detail key,
    only type + description -- without this, the filer only ever saw the bare
    status code even though the body explains exactly what to fix."""
    body = {
        "type": "Malformed Interview",
        "description": (
            "Court adams doesn't allow subsequent filing into non-indexed cases. "
            "If this case is in the court system, provide the Case tracking ID. "
            "If it's not, don't provide the docket number."
        ),
    }

    message = describe_efsp_error(FakeResponse(500, body))

    assert "Malformed Interview" in message
    assert "non-indexed cases" in message
    assert "500" not in message
    # The known-message catalog (lifted from the EFSP source) should recognize
    # this exact rejection and say what to do about it, not just repeat it.
    assert "New case" in message
    assert "Existing case" in message


@pytest.mark.parametrize(
    ("message", "expected_snippet"),
    [
        (
            "Court adams doesn't allow subsequent filing into non-indexed cases. If this case is "
            "in the court system, provide the Case tracking ID. If it's not, don't provide the "
            "docket number.",
            "New case",
        ),
        (
            "Subsequent filing case type (12345) needs docket number, but not present",
            "case number for this existing case",
        ),
        (
            "Document affidavit.pdf is too big! Must be max 10485760, is 20000000",
            "10,485,760-byte limit",
        ),
        (
            "All Documents combined are too big! Must be max10485760, are 15000000",
            "10,485,760-byte combined limit",
        ),
        (
            "Need a filing type! FilingTypes are empty, so CAT and TYPE are restricted",
            "double-check the case category",
        ),
        (
            "ad danum amount, Amount in controversy required",
            "doesn't collect yet",
        ),
    ],
)
def test_known_messages_get_an_actionable_hint(message, expected_snippet):
    hint = _actionable_hint(message)

    assert hint is not None
    assert expected_snippet in hint


def test_unrecognized_messages_get_no_hint():
    assert _actionable_hint("Something entirely new went wrong") is None


def test_plain_error_message_gets_its_hint_appended_too():
    body = {"error": "Subsequent filing case type (12345) needs docket number, but not present"}

    message = describe_efsp_error(FakeResponse(400, body))

    assert message.startswith(body["error"])
    assert "case number for this existing case" in message


def test_hint_still_matches_once_the_type_prefix_is_prepended():
    """describe_efsp_error prepends "{type}: " to description bodies, so a hint
    pattern anchored to the start of the raw message would never fire -- this
    caught that exact bug for the "Document ... too big" pattern."""
    body = {
        "type": "Malformed Interview",
        "description": "Document affidavit.pdf is too big! Must be max 10485760, is 20000000",
    }

    message = describe_efsp_error(FakeResponse(400, body))

    assert "10,485,760-byte limit" in message


def test_non_json_body_falls_back_to_the_status_and_text():
    message = describe_efsp_error(FakeResponse(502, text="<html>Bad Gateway</html>"))

    assert "502" in message
    assert "Bad Gateway" in message


def test_empty_body_reports_only_the_status():
    assert "503" in describe_efsp_error(FakeResponse(503, text=""))


def test_unrecognised_json_shape_reports_the_status():
    assert "418" in describe_efsp_error(FakeResponse(418, {"something": "else"}))


@pytest.mark.parametrize("body", [["a list"], "a string", 42])
def test_non_object_json_does_not_raise(body):
    assert describe_efsp_error(FakeResponse(400, body))


def test_nameless_var_is_ignored_rather_than_described_as_blank():
    """A var with no name says nothing; the status line is more useful."""
    message = describe_efsp_error(FakeResponse(400, {"wrong_vars": [{"currentVal": ""}]}))

    assert "400" in message
