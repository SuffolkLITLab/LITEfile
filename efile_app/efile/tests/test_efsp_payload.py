"""Tests for the shared EFSP payload preparation.

The safety-critical property is that EFSP_TEST_DOCUMENT_URL is inert unless a
developer opts in, so the "off" cases are tested as carefully as the "on" ones.
"""

import logging

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import override_settings

from efile.services.efsp_payload import (
    PayloadValidationError,
    _EfspLookups,
    normalize_efile_payload,
    normalize_optional_services,
    resolve_placeholder_filing_components,
    substitute_test_document_urls,
    validate_document_selections,
    validate_required_party_types,
)

REAL_S3_URL = "https://litefile-staging.s3.us-east-1.amazonaws.com/efile-documents/lead/abc.pdf?X-Amz-Signature=x"
STAND_IN_URL = "https://example.org/fixtures/blank.pdf"


@pytest.fixture
def efile_logs(caplog):
    """Capture records from the ``efile`` logger.

    ``caplog`` only attaches to the root logger, and settings_base.LOGGING sets
    ``propagate: False`` on ``efile``, so its records never reach root.
    """
    logger = logging.getLogger("efile")
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger="efile")
    yield caplog
    logger.removeHandler(caplog.handler)


def _bundle(**overrides):
    bundle = {"data_url": REAL_S3_URL, "filing_type": "27965", "filing_component": "331"}
    bundle.update(overrides)
    return {"al_court_bundle": [bundle]}


# --- EFSP_TEST_DOCUMENT_URL off (the production configuration) ---------------


def test_real_document_url_is_untouched_when_stand_in_is_not_configured():
    efile_data = _bundle()

    with override_settings(EFSP_TEST_DOCUMENT_URL=""):
        substitute_test_document_urls(efile_data)

    assert efile_data["al_court_bundle"][0]["data_url"] == REAL_S3_URL


def test_substitution_is_inert_when_setting_is_absent_entirely(monkeypatch):
    """settings_prod/settings_staging never define the setting at all.

    Deleted rather than left to the ambient value: the suite runs under
    settings_dev, which defaults the stand-in URL to a real PDF so local fee
    quotes work, and `override_settings` cannot express "no such setting".
    """
    efile_data = _bundle()

    # Through the LazySettings wrapper, not settings._wrapped: __getattr__ caches
    # each setting on the wrapper, so deleting only from the inner object leaves
    # the cached value visible to getattr.
    monkeypatch.delattr(settings, "EFSP_TEST_DOCUMENT_URL", raising=False)
    substitute_test_document_urls(efile_data)

    assert efile_data["al_court_bundle"][0]["data_url"] == REAL_S3_URL


# --- EFSP_TEST_DOCUMENT_URL set outside development --------------------------
#
# Django's test environment forces DEBUG=False, so every "on" case below has to
# opt into DEBUG=True explicitly -- which is the precondition the substitution
# actually requires.


def test_stand_in_url_is_refused_when_debug_is_off():
    """A non-development process holding a stand-in URL must not file anything."""
    efile_data = _bundle()

    with override_settings(EFSP_TEST_DOCUMENT_URL=STAND_IN_URL, DEBUG=False):
        with pytest.raises(ImproperlyConfigured, match="DEBUG is False"):
            substitute_test_document_urls(efile_data)


def test_refusal_happens_before_any_url_is_rewritten():
    """The raise must not leave a half-substituted payload behind."""
    efile_data = {"al_court_bundle": [{"data_url": REAL_S3_URL}, {"data_url": REAL_S3_URL}]}

    with override_settings(EFSP_TEST_DOCUMENT_URL=STAND_IN_URL, DEBUG=False):
        with pytest.raises(ImproperlyConfigured):
            substitute_test_document_urls(efile_data)

    assert [b["data_url"] for b in efile_data["al_court_bundle"]] == [REAL_S3_URL, REAL_S3_URL]


def test_full_payload_preparation_refuses_too():
    """The guard holds on the path both the fee quote and the submission call."""
    efile_data = _bundle()

    with override_settings(EFSP_TEST_DOCUMENT_URL=STAND_IN_URL, DEBUG=False):
        with pytest.raises(ImproperlyConfigured):
            normalize_efile_payload(efile_data, "illinois", "adams")


# --- EFSP_TEST_DOCUMENT_URL on in development --------------------------------


def test_stand_in_url_replaces_every_document_url():
    efile_data = {
        "al_court_bundle": [
            {"data_url": REAL_S3_URL},
            {"data_url": "https://litefile-staging.s3.us-east-1.amazonaws.com/efile-documents/supporting/d.pdf"},
        ]
    }

    with override_settings(EFSP_TEST_DOCUMENT_URL=STAND_IN_URL, DEBUG=True):
        substitute_test_document_urls(efile_data)

    assert [b["data_url"] for b in efile_data["al_court_bundle"]] == [STAND_IN_URL, STAND_IN_URL]


def test_substitution_warns_so_the_filing_is_not_mistaken_for_a_real_one(efile_logs):
    efile_data = _bundle()

    with override_settings(EFSP_TEST_DOCUMENT_URL=STAND_IN_URL, DEBUG=True):
        substitute_test_document_urls(efile_data)

    assert any("stand-in document" in record.getMessage() for record in efile_logs.records)


def test_substitution_leaves_other_bundle_fields_alone():
    efile_data = _bundle(filing_component="331", filing_type="27965")

    with override_settings(EFSP_TEST_DOCUMENT_URL=STAND_IN_URL, DEBUG=True):
        substitute_test_document_urls(efile_data)

    bundle = efile_data["al_court_bundle"][0]
    assert bundle["filing_component"] == "331"
    assert bundle["filing_type"] == "27965"


# --- cross_references --------------------------------------------------------


@pytest.mark.parametrize("empty_value", ["", None, [], {}])
def test_empty_cross_references_is_dropped_rather_than_sent(empty_value):
    efile_data = {"al_court_bundle": [], "cross_references": empty_value}

    with override_settings(EFSP_TEST_DOCUMENT_URL=""):
        normalize_efile_payload(efile_data, "illinois", "adams")

    assert "cross_references" not in efile_data


def test_populated_cross_references_is_preserved():
    efile_data = {"al_court_bundle": [], "cross_references": [{"code": "1", "value": "abc"}]}

    with override_settings(EFSP_TEST_DOCUMENT_URL=""):
        normalize_efile_payload(efile_data, "illinois", "adams")

    assert efile_data["cross_references"] == [{"code": "1", "value": "abc"}]


# --- missing filing type ------------------------------------------------------
#
# A draft can reach the fee quote with no filing type on a document. The EFSP
# answers with a wrong_vars entry and a list of bare code numbers, so the blank
# is caught here while the document can still be named.


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_document_without_a_filing_type_is_rejected(blank):
    efile_data = _bundle(filing_type=blank, filename="petition.pdf")

    with pytest.raises(PayloadValidationError, match="petition.pdf"):
        validate_document_selections(efile_data)


def test_missing_filing_type_names_every_affected_document():
    efile_data = {
        "al_court_bundle": [
            {"filing_type": "27965", "filename": "petition.pdf"},
            {"filing_type": "", "filename": "exhibit-a.pdf"},
            {"filing_type": "", "filename": "exhibit-b.pdf"},
        ]
    }

    with pytest.raises(PayloadValidationError) as rejection:
        validate_document_selections(efile_data)

    assert "exhibit-a.pdf" in str(rejection.value)
    assert "exhibit-b.pdf" in str(rejection.value)
    assert "petition.pdf" not in str(rejection.value)


def test_unnamed_document_is_identified_by_position():
    with pytest.raises(PayloadValidationError, match="document 2"):
        validate_document_selections({"al_court_bundle": [{"filing_type": "27965"}, {"filing_type": ""}]})


def test_document_type_is_left_to_the_court():
    """Only the filing type is universally required; the rest varies by court."""
    validate_document_selections({"al_court_bundle": [{"filing_type": "27965", "document_type": ""}]})


def test_preparation_rejects_a_blank_filing_type_end_to_end():
    """The exact payload the payment screen sent when its filing type was blank."""
    efile_data = _bundle(filing_type="", filename="petition.pdf")

    with override_settings(EFSP_TEST_DOCUMENT_URL=""):
        with pytest.raises(PayloadValidationError, match="filing type"):
            normalize_efile_payload(efile_data, "illinois", "edgar")


# --- placeholder filing components -------------------------------------------


class _ComponentsResponse:
    """The shape Illinois courts publish: an optional attachment slot, and one
    required lead document that every filing of this type has to carry."""

    status_code = 200

    @staticmethod
    def json():
        return [
            {"code": "331", "name": "Attachments", "required": False, "efspcode": "ATTACH"},
            {"code": "332", "name": "Lead Document", "required": True, "efspcode": "LEAD"},
        ]


def test_placeholder_component_label_is_resolved_to_the_court_code(monkeypatch, settings):
    monkeypatch.setattr("efile.services.efsp_payload.requests.get", lambda *args, **kwargs: _ComponentsResponse())
    settings.EFSP_URL = "https://efile-test.example"
    efile_data = _bundle(filing_component="supporting")

    resolve_placeholder_filing_components(efile_data, "illinois", "adams")

    assert efile_data["al_court_bundle"][0]["filing_component"] == "332"


def test_real_component_code_is_not_looked_up(monkeypatch, settings):
    def fail(*args, **kwargs):
        raise AssertionError("should not call the EFSP when the code is already real")

    monkeypatch.setattr("efile.services.efsp_payload.requests.get", fail)
    settings.EFSP_URL = "https://efile-test.example"
    efile_data = _bundle(filing_component="331")

    resolve_placeholder_filing_components(efile_data, "illinois", "adams")

    assert efile_data["al_court_bundle"][0]["filing_component"] == "331"


def test_repeated_filing_type_is_looked_up_once(monkeypatch, settings):
    calls = []

    def record(*args, **kwargs):
        calls.append(args)
        return _ComponentsResponse()

    monkeypatch.setattr("efile.services.efsp_payload.requests.get", record)
    settings.EFSP_URL = "https://efile-test.example"
    efile_data = {
        "al_court_bundle": [
            {"filing_type": "27965", "filing_component": "supporting"},
            {"filing_type": "27965", "filing_component": "attachment"},
        ]
    }

    resolve_placeholder_filing_components(efile_data, "illinois", "adams")

    assert len(calls) == 1
    assert [b["filing_component"] for b in efile_data["al_court_bundle"]] == ["332", "332"]


def test_a_bundle_without_a_component_gets_the_one_its_filing_type_requires(monkeypatch, settings):
    """Every bundle is its own filing, so every bundle needs a lead document.

    Filling the blank with the attachment slot instead leaves that filing with
    no lead document, and the court answers "Required filing component '332'
    not found" -- long after the filer has left the screen where they could
    have fixed it.
    """

    monkeypatch.setattr("efile.services.efsp_payload.requests.get", lambda *args, **kwargs: _ComponentsResponse())
    settings.EFSP_URL = "https://efile-test.example"
    efile_data = _bundle(filing_component="attachment")

    resolve_placeholder_filing_components(efile_data, "illinois", "kane")

    assert efile_data["al_court_bundle"][0]["filing_component"] == "332"


def test_the_lead_component_is_found_by_code_when_nothing_is_flagged_required(monkeypatch, settings):
    class _Unflagged:
        status_code = 200

        @staticmethod
        def json():
            return [
                {"code": "331", "name": "Attachments", "efspcode": "ATTACH"},
                {"code": "332", "name": "Lead Document", "efspcode": "LEAD"},
            ]

    monkeypatch.setattr("efile.services.efsp_payload.requests.get", lambda *args, **kwargs: _Unflagged())
    settings.EFSP_URL = "https://efile-test.example"
    efile_data = _bundle(filing_component="")

    resolve_placeholder_filing_components(efile_data, "illinois", "kane")

    assert efile_data["al_court_bundle"][0]["filing_component"] == "332"


def test_unresolvable_component_is_left_for_the_efsp_to_reject(monkeypatch, settings):
    import requests

    def boom(*args, **kwargs):
        raise requests.RequestException("EFSP unreachable")

    monkeypatch.setattr("efile.services.efsp_payload.requests.get", boom)
    settings.EFSP_URL = "https://efile-test.example"
    efile_data = _bundle(filing_component="supporting")

    resolve_placeholder_filing_components(efile_data, "illinois", "adams")

    # Guessing a code would file under the wrong component; leave it visibly wrong.
    assert efile_data["al_court_bundle"][0]["filing_component"] == "supporting"


# --- required party types ----------------------------------------------------
#
# A case type names the party types a filing must include. Nothing in the UI stops
# a filer from choosing the same type for themselves and the other side, and the
# EFSP answers that with a code-list error ("Missing [173180]") that means nothing
# to a filer, so the combination is caught here while it can still be explained.

PLAINTIFF = {"code": "173180", "name": "Plaintiff", "isrequired": True}
DEFENDANT = {"code": "173174", "name": "Defendant", "isrequired": True}
OPTIONAL_GUARDIAN = {"code": "173999", "name": "Guardian", "isrequired": False}


class _PartyTypesResponse:
    status_code = 200

    def __init__(self, party_types):
        self._party_types = party_types

    def json(self):
        return self._party_types


def _party_type_api(monkeypatch, settings, party_types):
    settings.EFSP_URL = "https://efile-test.example"
    monkeypatch.setattr(
        "efile.services.efsp_payload.requests.get",
        lambda *args, **kwargs: _PartyTypesResponse(party_types),
    )


def _payload(user_types, other_types=()):
    return {
        "efile_case_type": "186542",
        "al_court_bundle": [],
        "users": [{"party_type": code} for code in user_types],
        "other_parties": [{"party_type": code} for code in other_types],
    }


def test_missing_required_party_type_is_rejected_with_the_missing_name(monkeypatch, settings):
    """The exact case a filer hits by picking Defendant for both sides."""
    _party_type_api(monkeypatch, settings, [PLAINTIFF, DEFENDANT])

    with pytest.raises(PayloadValidationError, match="Plaintiff"):
        validate_required_party_types(_payload(["173174"], ["173174"]), "illinois", "cook:chd1")


def test_every_required_party_type_present_passes(monkeypatch, settings):
    _party_type_api(monkeypatch, settings, [PLAINTIFF, DEFENDANT])

    validate_required_party_types(_payload(["173180"], ["173174"]), "illinois", "cook:chd1")


def test_other_parties_count_toward_coverage(monkeypatch, settings):
    """The filer is one side; the other side is only ever in other_parties."""
    _party_type_api(monkeypatch, settings, [PLAINTIFF, DEFENDANT])

    validate_required_party_types(_payload(["173174"], ["173180"]), "illinois", "cook:chd1")


def test_optional_party_types_are_not_required(monkeypatch, settings):
    _party_type_api(monkeypatch, settings, [PLAINTIFF, DEFENDANT, OPTIONAL_GUARDIAN])

    validate_required_party_types(_payload(["173180"], ["173174"]), "illinois", "cook:chd1")


def test_string_valued_isrequired_is_honoured(monkeypatch, settings):
    """Tyler's code lists are inconsistent about JSON booleans vs "true"."""
    _party_type_api(monkeypatch, settings, [{"code": "173180", "name": "Plaintiff", "isrequired": "true"}])

    with pytest.raises(PayloadValidationError, match="Plaintiff"):
        validate_required_party_types(_payload(["173174"]), "illinois", "cook:chd1")


def test_unreachable_party_type_list_does_not_block_the_filing(monkeypatch, settings):
    """Fails open: the EFSP stays the authority, this check only improves the message."""
    import requests

    settings.EFSP_URL = "https://efile-test.example"

    def boom(*args, **kwargs):
        raise requests.RequestException("EFSP unreachable")

    monkeypatch.setattr("efile.services.efsp_payload.requests.get", boom)

    validate_required_party_types(_payload(["173174"], ["173174"]), "illinois", "cook:chd1")


def test_payload_without_a_case_type_is_not_checked(monkeypatch, settings):
    """No case type means no code list to check against -- and no API call."""
    settings.EFSP_URL = "https://efile-test.example"

    def fail(*args, **kwargs):
        raise AssertionError("should not call the EFSP without a case type")

    monkeypatch.setattr("efile.services.efsp_payload.requests.get", fail)

    validate_required_party_types({"al_court_bundle": [], "users": []}, "illinois", "cook:chd1")


def test_preparation_rejects_the_payload_end_to_end(monkeypatch, settings):
    """normalize_efile_payload is what both the fee quote and the submission call."""
    _party_type_api(monkeypatch, settings, [PLAINTIFF, DEFENDANT])

    with override_settings(EFSP_TEST_DOCUMENT_URL=""):
        with pytest.raises(PayloadValidationError):
            normalize_efile_payload(_payload(["173174"], ["173174"]), "illinois", "cook:chd1")


# --- optional services -------------------------------------------------------
#
# A court decides per service whether a fee multiplier belongs in the payload,
# and rejects the payload either way round: 400 when a required multiplier is
# missing, 422 when an unsupported one is present.

MULTIPLIED_SERVICE = {"code": "143487", "name": "Certified copy", "multiplier": True}
FLAT_SERVICE = {"code": "143488", "name": "Courtesy copy", "multiplier": False}


class _OptionalServicesResponse:
    def __init__(self, services, status_code=200):
        self.status_code = status_code
        self._services = services

    def json(self):
        return self._services


def _services_payload(services):
    return {"al_court_bundle": [{"filing_type": "27965", "optional_services": services}]}


def test_a_service_the_court_multiplies_is_sent_with_a_multiplier(monkeypatch, settings):
    settings.EFSP_URL = "https://efile-test.example"
    monkeypatch.setattr(
        "efile.services.efsp_payload.requests.get",
        lambda *args, **kwargs: _OptionalServicesResponse([MULTIPLIED_SERVICE, FLAT_SERVICE]),
    )
    efile_data = _services_payload(["143487", "143488"])

    normalize_optional_services(efile_data, "illinois", "adams")

    assert efile_data["al_court_bundle"][0]["optional_services"] == [
        {"code": "143487", "multiplier": 1},
        {"code": "143488"},
    ]


def test_an_unfetchable_service_list_keeps_the_multiplier_the_client_sent(monkeypatch, settings):
    """Unknown is not the same as "the court multiplies nothing".

    Treating a failed lookup as "no multiplier anywhere" strips the field from
    exactly the services that require it, and the filing dies at the EFSP with
    the malformed-interview error this normalization exists to prevent. The
    picker that offered the service read the same court list, so its answer is
    the best evidence left.
    """

    settings.EFSP_URL = "https://efile-test.example"
    monkeypatch.setattr(
        "efile.services.efsp_payload.requests.get",
        lambda *args, **kwargs: _OptionalServicesResponse(None, status_code=503),
    )
    efile_data = _services_payload([{"code": "143487", "multiplier": 2}, {"code": "143488"}])

    normalize_optional_services(efile_data, "illinois", "adams")

    assert efile_data["al_court_bundle"][0]["optional_services"] == [
        {"code": "143487", "multiplier": 2},
        {"code": "143488"},
    ]


def test_a_service_the_court_does_not_multiply_loses_a_supplied_multiplier(monkeypatch, settings):
    settings.EFSP_URL = "https://efile-test.example"
    monkeypatch.setattr(
        "efile.services.efsp_payload.requests.get",
        lambda *args, **kwargs: _OptionalServicesResponse([FLAT_SERVICE]),
    )
    efile_data = _services_payload([{"code": "143488", "multiplier": 3}])

    normalize_optional_services(efile_data, "illinois", "adams")

    assert efile_data["al_court_bundle"][0]["optional_services"] == [{"code": "143488"}]


def test_string_valued_multiplier_flag_is_honoured(monkeypatch, settings):
    """Tyler's code lists are inconsistent about JSON booleans vs "true"."""
    settings.EFSP_URL = "https://efile-test.example"
    monkeypatch.setattr(
        "efile.services.efsp_payload.requests.get",
        lambda *args, **kwargs: _OptionalServicesResponse([{"code": "143487", "multiplier": "true"}]),
    )
    efile_data = _services_payload(["143487"])

    normalize_optional_services(efile_data, "illinois", "adams")

    assert efile_data["al_court_bundle"][0]["optional_services"] == [{"code": "143487", "multiplier": 1}]


# --- code-list lookup budget -------------------------------------------------


def test_one_code_list_is_fetched_once_across_every_phase(monkeypatch, settings):
    """Each phase asks its own questions, but no URL is paid for twice."""
    settings.EFSP_URL = "https://efile-test.example"
    calls = []

    def record(url, **kwargs):
        calls.append(url)
        return _OptionalServicesResponse([])

    monkeypatch.setattr("efile.services.efsp_payload.requests.get", record)
    efile_data = {
        "al_court_bundle": [
            {"filing_type": "27965", "filing_component": "supporting", "optional_services": ["143487"]},
            {"filing_type": "27965", "filing_component": "supporting", "optional_services": ["143487"]},
        ],
        "efile_case_type": "186542",
        "users": [],
        "other_parties": [],
    }

    with override_settings(EFSP_TEST_DOCUMENT_URL=""):
        normalize_efile_payload(efile_data, "illinois", "adams")

    assert len(calls) == len(set(calls))


def test_a_spent_time_budget_stops_further_lookups(monkeypatch, settings):
    """A slow EFSP must not stack one timeout per filing type onto a request.

    Every lookup fails open, so running out of budget costs a better message,
    never the filing itself.
    """

    settings.EFSP_URL = "https://efile-test.example"

    def fail(*args, **kwargs):
        raise AssertionError("no request should be made once the budget is spent")

    monkeypatch.setattr("efile.services.efsp_payload.requests.get", fail)
    spent = _EfspLookups(budget=0)

    assert spent.get("https://efile-test.example/anything") is None
