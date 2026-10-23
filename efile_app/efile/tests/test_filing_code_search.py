"""Search preserves real paths, jurisdiction boundaries and changing catalogs."""

from copy import deepcopy
from importlib import import_module
from unittest.mock import Mock, patch

import pytest
import requests
from django.core.cache import cache, caches
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from efile.models import FilingCodeIndex, FilingDocument, FilingDraft, FilingParty
from efile.services import court_selection
from efile.services.filing_code_search import (
    CodeCatalog,
    concept_definitions,
    concept_meaning,
    current_index,
    explanation_for,
    filing_facets,
    filing_group_key,
    filing_groups,
    matching_paths,
    refresh_index,
    search_context,
    search_grouped_paths,
    search_paths,
    search_tokens,
    validate_path,
)
from efile.services.filing_path import clear_changed_classification

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def legacy_catalog_transport(settings):
    # These tests exercise the legacy reader and shared search/validation behavior.
    # The bulk transport and incremental imports have separate contract tests.
    settings.FILING_CODE_SYNC_MODE = "legacy"


def option(code, name):
    return {"code": code, "name": name}


@pytest.fixture
def catalog():
    with patch("efile.services.filing_code_search.CodeCatalog") as factory:
        api = factory.return_value
        api.courts.return_value = [
            option("housing", "Boston Housing Court"),
            option("district", "Boston District Court"),
        ]
        api.categories.return_value = [option("civil", "Civil")]
        api.types.return_value = [option("sp", "Summary Process"), option("debt", "Debt collection")]

        def filings(court, category, case_type, initial):
            assert category == "civil"
            if case_type == "debt":
                return [option("complaint", "Complaint")] if initial else [option("motion", "Motion")]
            return (
                [option("spcomplaint", "Summary Process Complaint")]
                if initial
                else [
                    option("answer", "Answer to Summary Process Complaint"),
                    option("dismiss", "Motion to Dismiss"),
                ]
            )

        api.filings.side_effect = filings
        yield api


@pytest.fixture
def index(catalog):
    refresh_index("massachusetts")
    return current_index("massachusetts")


@pytest.mark.parametrize("jurisdiction", ["illinois", "massachusetts", "vermont"])
def test_synonyms_search_other_courts_and_all_facets(catalog, jurisdiction):
    refresh_index(jurisdiction)
    index = current_index(jurisdiction)
    for query in ("eviction", "evicted", "unlawful detainer", "summary process", "forcible entry and detainer"):
        data = search_paths(index, query)
        assert data["total"] == 2
        assert {row["court"]["code"] for row in data["results"]} == {"housing", "district"}
    data = search_paths(index, "district civil eviction complaint")
    assert data["total"] == 1
    row = data["results"][0]
    assert row["court"]["code"] == "district"
    assert row["case_category"]["code"] == "civil"
    assert row["case_type"]["code"] == "sp"
    assert row["filing_type"]["code"] == "spcomplaint"
    assert search_paths(index, "debt eviction")["total"] == 0


def test_stems_typos_ranking_and_pagination(index):
    assert search_paths(index, "motions to dismiss", initial=False)["total"] == 2
    assert search_paths(index, "complants")["corrected_terms"]
    assert search_paths(index, "complants")["total"] == 4
    assert search_paths(index, "eviciton")["total"] == 2
    assert search_paths(index, "eviciton")["corrected_terms"] == ["eviction"]
    assert search_paths(index, "unlawfull detainer")["total"] == 2
    assert search_paths(index, "notice", initial=False)["total"] == 0
    assert search_paths(index, "eviction", initial=False)["total"] == 4
    first = search_paths(index, "complaint", limit=1)
    second = search_paths(index, "complaint", offset=1, limit=1)
    assert first["total"] == 4
    assert first["results"][0]["id"] != second["results"][0]["id"]
    assert search_paths(index, "the and")["total"] == 0
    assert search_paths(index, "<script>alert(1)</script>")["total"] == 0


def test_phrase_concepts_do_not_expand_individual_words():
    assert "concepteviction" not in search_tokens("summary judgment", "massachusetts")
    assert "conceptprotection" not in search_tokens("order", "vermont")
    assert "conceptanswer" not in search_tokens("complaint", "illinois")


def test_synonym_results_explain_concepts_without_changing_matches(index):
    groups = search_grouped_paths(index, "unlawful detainer")["groups"]
    assert len(groups) == 1
    group = groups[0]
    assert group["match_reason"]["kind"] == "related_concept"
    assert group["match_reason"]["concepts"] == ["eviction"]
    meaning = group["concepts"][0]
    assert meaning["scope"] == "state" and meaning["scope_name"] == "Massachusetts"
    assert "summary process" in meaning["text"]
    assert search_grouped_paths(index, "summary process")["groups"][0]["match_reason"] is None
    path = search_paths(index, "unlawful detainer")["results"][0]
    assert path["match_reason"]["kind"] == "related_concept"
    assert path["concepts"][0]["text"] == meaning["text"]
    typo = search_grouped_paths(index, "unlawfull detainer")["groups"][0]
    assert typo["match_reason"]["concepts"] == ["eviction"]
    assert typo["match_reason"]["corrected_terms"] == ["unlawful"]


def test_case_topic_is_not_a_definition_of_the_document():
    context = search_context(
        "massachusetts", "unlawful detainer", [("filing_type", "Affidavit"), ("case_type", "Summary Process")]
    )
    assert context["match_reason"]["kind"] == "related_concept"
    assert context["concepts"][0]["key"] == "eviction"
    assert "affidavit" not in context["concepts"][0]["text"].lower()
    assert search_context("massachusetts", "summary judgment", [("filing_type", "Motion")])["concepts"] == []
    alternate = search_context(
        "massachusetts",
        "summary process",
        [("filing_type", "Eviction complaint"), ("filing_type", "Summary process complaint")],
    )
    assert alternate["match_reason"]["kind"] == "alternate_name"


def test_concept_definition_precedence_and_local_scope(monkeypatch):
    config = deepcopy(concept_definitions())
    state = config["concepts"]["eviction"]["jurisdictions"]["illinois"]
    state["counties"]["Cook"] = {"text": "County-specific explanation.", "source": "https://example.org/county"}
    court = "Cook County - Municipal Civil - District 1 - Chicago"
    state["courts"][court] = {"text": "Court-specific explanation."}
    monkeypatch.setattr("efile.services.filing_code_search.concept_definitions", lambda: config)
    assert concept_meaning("eviction", "illinois")["scope"] == "state"
    assert concept_meaning("eviction", "illinois", "Cook County - Chancery")["text"] == "County-specific explanation."
    assert concept_meaning("eviction", "illinois", "Cook County")["scope"] == "county"
    specific = concept_meaning("eviction", "illinois", court)
    assert specific["text"] == "Court-specific explanation."
    assert specific["scope"] == "court" and specific["source"] == ""
    assert concept_meaning("eviction", "illinois", "Cooksville County")["scope"] == "state"
    assert concept_meaning("eviction", "vermont", court)["scope_name"] == "Vermont"
    assert concept_meaning("answer", "illinois")["scope"] == "general"


def test_local_definition_is_applied_after_court_selection_and_without_reindex(index, monkeypatch):
    before = search_grouped_paths(index, "unlawful detainer")
    config = deepcopy(concept_definitions())
    config["concepts"]["eviction"]["jurisdictions"]["massachusetts"]["courts"] = {
        "Boston Housing Court": {"text": "Local explanation.", "source": "https://example.org/local"}
    }
    monkeypatch.setattr("efile.services.filing_code_search.concept_definitions", lambda: config)
    after = search_grouped_paths(index, "unlawful detainer")
    assert after == before  # No court has been chosen: keep the statewide meaning.
    paths = search_grouped_paths(index, "unlawful detainer", group_key=after["groups"][0]["key"], court="housing")
    assert paths["results"][0]["concepts"][0]["text"] == "Local explanation."
    config["concepts"]["eviction"]["jurisdictions"]["massachusetts"]["text"] = "Updated statewide explanation."
    assert (
        search_grouped_paths(index, "unlawful detainer")["groups"][0]["concepts"][0]["text"]
        == "Updated statewide explanation."
    )
    assert current_index("massachusetts").pk == index.pk


def test_every_search_concept_has_a_short_sourced_definition():
    from efile.services.filing_code_search import rules

    for concept, config in rules()[0]["concepts"].items():
        for state in config.get("jurisdictions", rules()[0]["jurisdictions"]):
            meaning = concept_meaning(concept, state)
            assert meaning["text"] and meaning["source"].startswith("https://")


@pytest.mark.parametrize(
    ("name", "purpose", "document"),
    [
        ("Eviction Complaint", "starting", "main"),
        ("Answer to Complaint", "responding", "main"),
        ("Motion to Dismiss Complaint", "either", "main"),
        ("Exhibit - Complaint", "unknown", "attachment"),
        ("Exhibits in support of motion", "unknown", "attachment"),
        ("Affidavit supporting complaint", "either", "unknown"),
        ("Order on complaint", "unknown", "unknown"),
        ("Unfamiliar local code", "unknown", "unknown"),
    ],
)
def test_facets_preserve_document_role_and_leave_ambiguous_types_unknown(name, purpose, document):
    assert filing_facets(name, "illinois") == {"purpose": purpose, "document": document}


def test_facets_filter_groups_before_pagination_and_preserve_exact_paths(catalog):
    catalog.filings.return_value = [
        option("complaint", "Complaint"),
        option("answer", "Answer to Complaint"),
        option("motion", "Motion to Dismiss Complaint"),
        option("exhibit", "Exhibit"),
        option("unknown", "Unfamiliar local code"),
    ]
    catalog.filings.side_effect = None
    refresh_index("massachusetts")
    index = current_index("massachusetts")
    all_groups = search_grouped_paths(index, "eviction")
    assert all_groups["total"] == 5
    attachments = search_grouped_paths(index, "eviction", document="attachment", limit=1)
    assert attachments["total"] == 1
    assert attachments["groups"][0]["name"] == "Exhibit"
    assert search_grouped_paths(index, "eviction", document="attachment", offset=1)["groups"] == []
    responses = search_grouped_paths(index, "eviction", initial=False, purpose="responding", document="main")
    assert responses["total"] == 1
    group = responses["groups"][0]
    assert group["facets"] == {"purpose": "responding", "document": "main"}
    paths = search_grouped_paths(
        index, "eviction", initial=False, purpose="responding", document="main", group_key=group["key"], court="housing"
    )
    assert paths["total"] == 1 and paths["results"][0]["filing_type"]["code"] == "answer"
    assert paths["results"][0]["initial"] is False
    assert search_grouped_paths(index, "eviction", document="unknown")["groups"][0]["name"] == "Unfamiliar local code"


def test_endpoint_validates_and_applies_facets(signed_in, index):
    params = {
        "jurisdiction": "massachusetts",
        "q": "eviction",
        "grouped": "true",
        "existing_case": "yes",
        "purpose": "responding",
        "document": "main",
    }
    result = signed_in.get(URL, params).json()
    assert result["total"] == 1
    assert result["groups"][0]["facets"]["purpose"] == "responding"
    assert signed_in.get(URL, {**params, "purpose": "unexpected"}).status_code == 400
    assert signed_in.get(URL, {**params, "document": "unexpected"}).status_code == 400


def test_generic_filing_names_include_the_matching_case_context(catalog):
    catalog.categories.return_value = [option("small", "Small claims"), option("civil", "Civil")]
    catalog.types.side_effect = lambda court, category, initial: (
        [option("small", "Small claims")]
        if category == "small"
        else [option("eviction", "Eviction"), option("debt", "Debt collection")]
    )
    catalog.filings.side_effect = None
    catalog.filings.return_value = [option("petition", "Petition")]
    refresh_index("massachusetts")
    index = current_index("massachusetts")
    group = search_grouped_paths(index, "petition")["groups"][0]
    assert group["case_context_count"] == 3
    assert set(group["case_contexts"]) == {"Small claims", "Civil › Eviction", "Civil › Debt collection"}
    assert group["path_count"] == 6
    matched = search_grouped_paths(index, "eviction")["groups"][0]
    assert matched["case_contexts"] == ["Civil › Eviction"]
    assert matched["path_count"] == 2
    detail = search_grouped_paths(index, "petition", group_key=group["key"], court="housing")
    assert {path["case_context"] for path in detail["results"]} == set(group["case_contexts"])
    small = next(item for item in group["case_context_options"] if item["label"] == "Small claims")
    narrowed = search_grouped_paths(index, "petition", group_key=group["key"], court="housing", context=small["key"])
    assert narrowed["total"] == 1
    assert narrowed["results"][0]["case_category"]["code"] == "small"
    assert narrowed["results"][0]["case_type"]["code"] == "small"
    courts = search_grouped_paths(index, "petition", group_key=group["key"], context=small["key"])
    assert {item["code"] for item in courts["courts"]} == {"housing", "district"}
    with pytest.raises(ValueError, match="case type"):
        search_grouped_paths(index, "eviction", group_key=group["key"], context=small["key"])


def test_context_preview_is_bounded_without_hiding_the_number_of_case_types(catalog):
    catalog.types.return_value = [option(str(i), f"Case type {i}") for i in range(12)]
    catalog.filings.side_effect = None
    catalog.filings.return_value = [option("petition", "Petition")]
    refresh_index("massachusetts")
    group = search_grouped_paths(current_index("massachusetts"), "petition")["groups"][0]
    assert len(group["case_contexts"]) == 8
    assert group["case_context_count"] == 12
    assert group["path_count"] == 24


def test_sqlite_search_uses_full_text_index(index):
    if connection.vendor != "sqlite":
        pytest.skip("SQLite query plan")
    paths, _ = matching_paths(index, "eviction", True)
    plan = paths.explain()
    assert "filing_code_search_fts VIRTUAL TABLE INDEX" in plan
    assert "rowid=?" in plan
    assert 'search_text" LIKE' not in str(paths.query)
    # Neither SQL nor FTS query operators in user input can bypass scoping.
    assert search_paths(index, 'eviction OR "debt"')["total"] == 0


def test_court_filters_use_the_indexed_court_code(catalog):
    if connection.vendor != "sqlite":
        pytest.skip("SQLite query text")
    catalog.courts.return_value = [option("court-1", "County 1"), option("court-2", "County 2")]
    refresh_index("massachusetts")
    index = current_index("massachusetts")
    group = search_grouped_paths(index, "eviction")["groups"][0]
    with CaptureQueriesContext(connection) as queries:
        detail = search_grouped_paths(index, "eviction", group_key=group["key"], court="court-1")
    assert {path["court"]["code"] for path in detail["results"]} == {"court-1"}
    sql = " ".join(query["sql"] for query in queries.captured_queries)
    # A JSON key lookup (court__code) cannot use the (index, court code) index;
    # on Postgres that read every path matching the words, in every jurisdiction.
    assert "'$.code'" in sql
    assert '$."code"' not in sql


def test_sqlite_full_text_index_tracks_edits_deletes_and_rollback(index):
    if connection.vendor != "sqlite":
        pytest.skip("SQLite index triggers")
    path = index.paths.filter(initial=True, case_type__code="sp").first()
    with pytest.raises(RuntimeError), transaction.atomic():
        index.paths.filter(pk=path.pk).update(search_text=" debt ")
        assert search_paths(index, "eviction")["total"] == 1
        assert search_paths(index, "debt")["total"] == 3
        raise RuntimeError("Abort catalog publication")
    assert search_paths(index, "eviction")["total"] == 2
    index.paths.filter(pk=path.pk).update(initial=False)
    assert search_paths(index, "eviction")["total"] == 1
    assert search_paths(index, "eviction", initial=False)["total"] == 5
    path.delete()
    assert search_paths(index, "eviction")["total"] == 1
    assert search_paths(index, "eviction", initial=False)["total"] == 4
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO filing_code_search_fts(filing_code_search_fts, rank) VALUES ('integrity-check', 1)")


def test_sqlite_migration_indexes_existing_catalog(index):
    if connection.vendor != "sqlite":
        pytest.skip("SQLite migration backfill")
    migration = import_module("efile.migrations.0034_sqlite_filing_search_index")
    editor = connection.schema_editor()
    vars(migration)["remove_sqlite_search_index"](None, editor)
    vars(migration)["add_sqlite_search_index"](None, editor)
    assert search_paths(index, "eviction")["total"] == 2
    assert search_paths(index, "district civil eviction complaint")["total"] == 1


def test_subsequent_only_hierarchy_is_indexed_and_validated(catalog):
    catalog.categories.side_effect = lambda court, initial: [option("civil", "Civil")] if not initial else []
    catalog.types.side_effect = (
        lambda court, category, initial: [option("sp", "Summary Process")] if not initial else []
    )
    refresh_index("massachusetts")
    index = current_index("massachusetts")
    assert search_paths(index, "eviction", initial=True)["total"] == 0
    assert search_paths(index, "eviction", initial=False)["total"] == 4
    path = index.paths.first()
    assert validate_path(path)["initial"] is False
    catalog.categories.assert_called_with(path.court["code"], False)
    catalog.types.assert_called_with(path.court["code"], "civil", False)


def test_explanations_are_exact_optional_and_regenerated(index, catalog):
    path = index.paths.filter(initial=True, case_type__code="sp").first()
    assert path.explanation
    raw = {facet: getattr(path, facet) for facet in ("court", "case_category", "case_type", "filing_type")}
    raw["filing_type"] = option("spcomplaint", "Motion to dismiss Summary Process Complaint")
    assert explanation_for("massachusetts", raw, True) == ("", "")
    assert explanation_for("vermont", raw, True) == ("", "")
    catalog.filings.side_effect = None
    catalog.filings.return_value = [option("spcomplaint", "Renamed code")]
    validated = validate_path(path)
    assert validated["explanation"] == ""
    assert validated["filing_type"]["name"] == "Renamed code"
    assert validated["options"]["court"]


@pytest.mark.parametrize("facet", ["court", "case_category", "case_type"])
def test_cached_explanations_preserve_context_and_filing_timing(facet):
    from efile.services.filing_code_search import _explanation_for_names, explanation_rules

    config = {
        "explanations": [
            {
                "jurisdictions": ["illinois"],
                "initial": True,
                "filing_type": ["Motion"],
                facet: ["Specific context"],
                "text": "Context-specific explanation",
                "source": "https://example.com/court",
            }
        ]
    }
    explanation_rules.cache_clear()
    _explanation_for_names.cache_clear()
    try:
        with patch("efile.services.filing_code_search.rules", return_value=(config, "test")):
            path = {"filing_type": option("motion", "Motion"), facet: option("context", "Specific context")}
            assert explanation_for("illinois", path, True)[0] == "Context-specific explanation"
            assert explanation_for("illinois", path, False) == ("", "")
            assert explanation_for("vermont", path, True) == ("", "")
            path[facet] = option("other", "Other context")
            assert explanation_for("illinois", path, True) == ("", "")
    finally:
        explanation_rules.cache_clear()
        _explanation_for_names.cache_clear()


def test_failed_refresh_keeps_snapshot_then_success_removes_retired_paths(index, catalog):
    ids = list(index.paths.values_list("pk", flat=True))
    stamp = index.refreshed_at
    catalog.types.side_effect = requests.Timeout("unavailable")
    with pytest.raises(requests.Timeout):
        refresh_index("massachusetts")
    index.refresh_from_db()
    assert index.refreshed_at == stamp
    assert list(index.paths.values_list("pk", flat=True)) == ids
    catalog.types.side_effect = None
    catalog.courts.return_value = [option("district", "Boston District Court")]
    refresh_index("massachusetts")
    assert not index.paths.filter(court__code="housing").exists()
    assert not index.paths.filter(pk__in=ids).exists()


def test_empty_refresh_preserves_previous_index(index, catalog):
    catalog.courts.return_value = []
    with pytest.raises(ValueError, match="No filing paths"):
        refresh_index("massachusetts")
    assert index.paths.exists()


def test_parallel_filing_request_failure_does_not_publish_partial_index(index, catalog):
    ids = list(index.paths.values_list("pk", flat=True))
    catalog.filings.side_effect = requests.Timeout("filing branch unavailable")
    with pytest.raises(requests.Timeout):
        refresh_index("massachusetts")
    assert list(index.paths.values_list("pk", flat=True)) == ids


def test_wrong_efsp_or_rules_revision_not_served(index, settings):
    settings.EFSP_URL = "https://other.example/api"
    assert current_index("massachusetts") is None
    settings.EFSP_URL = index.source_url
    index.rules_digest = "old"
    index.save()
    assert current_index("massachusetts") is None


def test_live_validation_rejects_removed_or_restricted_path(index, catalog):
    path = index.paths.first()
    catalog.courts.return_value = []
    with pytest.raises(ValueError, match="no longer offered"):
        validate_path(path)
    catalog.courts.return_value = [path.court]
    with patch("efile.services.filing_code_search.filing_unavailable_message", return_value="Not available here"):
        with pytest.raises(ValueError, match="Not available here"):
            validate_path(path)


def test_search_exposes_availability_without_hiding_other_courts(index):
    with patch("efile.services.filing_code_search.filing_unavailable_message", return_value="Temporarily unavailable"):
        data = search_paths(index, "eviction")
    assert data["total"] == 2
    assert all(row["unavailable"] == "Temporarily unavailable" for row in data["results"])


def test_a_hundred_courts_and_synonyms_become_one_result(catalog):
    catalog.courts.return_value = [option(f"court-{i}", f"County {i}") for i in range(100)]
    catalog.types.return_value = [option("sp", "Summary Process")]
    catalog.filings.side_effect = lambda court, category, case_type, initial: [
        option(f"filing-{court}", "Eviction Complaint" if int(court.split("-")[1]) % 2 else "Summary Process Complaint")
    ]
    refresh_index("massachusetts")
    index = current_index("massachusetts")
    grouped = search_grouped_paths(index, "eviction")
    assert grouped["total"] == 1
    group = grouped["groups"][0]
    assert group["path_count"] == 100
    assert set(group["variants"]) == {"Eviction Complaint", "Summary Process Complaint"}
    courts = search_grouped_paths(index, "eviction", group_key=group["key"])["courts"]
    assert len(courts) == 100
    detail = search_grouped_paths(index, "eviction", group_key=group["key"], court="court-7")
    assert detail["total"] == 1
    assert detail["results"][0]["court"]["code"] == "court-7"
    assert detail["results"][0]["filing_type"]["code"] == "filing-court-7"
    assert search_grouped_paths(index, "County 7 eviction")["groups"][0]["path_count"] == 1
    assert search_grouped_paths(index, "eviction", group_key=group["key"], court="unknown")["total"] == 0


def test_grouping_keeps_different_documents_separate():
    def key(name):
        return filing_group_key(name, "massachusetts")

    assert key("Answer") == key("Answer to Complaint")
    assert key("Affidavit") == key("Affidavit filed.")
    assert key("Eviction Complaint") == key("E-filed - Eviction Complaint filed.")
    assert key("Eviction Complaint") == key("Complaint for Summary Process")
    assert key("Eviction") == key("Summary Process")
    assert key("Fee waiver") == key("In forma pauperis")
    assert key("Divorce") == key("Dissolution of marriage")
    assert key("Complaint") != key("Answer to Complaint")
    assert key("Motion to Dismiss") != key("Complaint")
    assert key("Motion to Dismiss") != key("Dismissal of Motion")
    assert key("Verified Complaint") != key("Complaint")
    assert key("Proposed Order") != key("Order")
    assert key("Answer and Counterclaim") != key("Answer")
    assert key("Motion for Summary Judgment") != key("Summary Process Complaint")


def test_grouped_endpoint_keeps_court_selection_separate(signed_in, index):
    params = {"jurisdiction": "massachusetts", "q": "eviction", "grouped": "true"}
    result = signed_in.get(URL, params).json()
    assert result["total"] == 1
    assert "results" not in result
    params["group"] = result["groups"][0]["key"]
    assert len(signed_in.get(URL, params).json()["courts"]) == 2
    params["court"] = "housing"
    paths = signed_in.get(URL, params).json()["results"]
    assert len(paths) == 1 and paths[0]["court"]["code"] == "housing"
    params["group"] = "retired-group"
    assert signed_in.get(URL, params).status_code == 409


def test_numeric_code_names_stay_text_in_grouped_search(catalog):
    # Illinois has a case type named "97"; SQLite's JSON key lookup decodes it as a number.
    catalog.types.return_value = [option("97", "97")]
    catalog.courts.return_value = [option("42", "42")]
    refresh_index("massachusetts")
    result = search_grouped_paths(current_index("massachusetts"), "complaint", initial=True)
    assert [group["name"] for group in result["groups"]] == ["Summary Process Complaint"]
    courts = search_grouped_paths(
        current_index("massachusetts"), "complaint", initial=True, group_key=result["groups"][0]["key"]
    )["courts"]
    assert courts == [{"code": "42", "name": "42"}]


def test_refresh_resumes_completed_courts_after_late_failure(catalog, tmp_path):
    def fail_district(court, category, initial):
        if court == "district":
            raise requests.Timeout("late failure")
        return [option("sp", "Summary Process")]

    catalog.types.side_effect = fail_district
    with pytest.raises(requests.Timeout):
        refresh_index("massachusetts", cache_dir=tmp_path)
    assert current_index("massachusetts") is None
    assert len(list(tmp_path.rglob("*.json.gz"))) == 1
    catalog.types.reset_mock()
    catalog.types.side_effect = None
    catalog.types.return_value = [option("sp", "Summary Process")]
    refresh_index("massachusetts", cache_dir=tmp_path)
    assert current_index("massachusetts").paths.count() == 6
    assert {call.args[0] for call in catalog.types.call_args_list} == {"district"}


def test_catalog_retries_failures_while_reading_response_body():
    with (
        patch("efile.services.filing_code_search.requests.Session") as session,
        patch("efile.services.filing_code_search.time.sleep") as sleep,
    ):
        response = session.return_value.get.return_value
        response.json.side_effect = [requests.ConnectionError("body timed out"), [{"code": "civil", "name": "Civil"}]]
        api = CodeCatalog("illinois")
        assert api.categories("court") == [option("civil", "Civil")]
        assert session.return_value.get.call_count == 2
        sleep.assert_called_once_with(1)
        api.close()


def test_catalog_encodes_codes_and_rejects_malformed_data():
    with patch("efile.services.filing_code_search.requests.Session") as session:
        response = session.return_value.get.return_value
        response.json.return_value = [{"code": 123, "name": "Civil"}]
        api = CodeCatalog("illinois")
        assert api.categories("cook:cvd1") == [option("123", "Civil")]
        assert "cook%3Acvd1/categories" in session.return_value.get.call_args.args[0]
        response.json.return_value = {"error": "oops"}
        with pytest.raises(ValueError):
            api.courts()
        response.json.return_value = [{"name": "No code"}]
        with pytest.raises(ValueError):
            api.courts()
        response.raise_for_status.side_effect = requests.HTTPError()
        with pytest.raises(requests.HTTPError):
            api.courts()


@pytest.fixture
def signed_in(client, django_user_model):
    user = django_user_model.objects.create_user(username="searcher", tyler_jurisdiction="massachusetts")
    client.force_login(user)
    session = client.session
    session["auth_tokens"] = {"TYLER-TOKEN-MASSACHUSETTS": "test-token"}
    session.save()
    return client


URL = reverse("api:filing_code_search")


def test_endpoint_auth_input_missing_index_and_jurisdiction_isolation(signed_in, index, client):
    for query in ("", "x" * 161):
        assert signed_in.get(URL, {"jurisdiction": "massachusetts", "q": query}).status_code == 400
    assert signed_in.get(URL, {"jurisdiction": "massachusetts", "q": "m"}).status_code == 200
    assert signed_in.get(URL, {"jurisdiction": "massachusetts", "q": "eviction", "offset": -1}).status_code == 400
    assert signed_in.post(URL).status_code == 405
    index.delete()
    assert signed_in.get(URL, {"jurisdiction": "massachusetts", "q": "eviction"}).status_code == 503
    assert signed_in.get(URL, {"jurisdiction": "vermont", "q": "eviction"}).status_code == 403
    assert signed_in.get(URL, {"jurisdiction": "unsupported", "q": "eviction"}).status_code == 400
    client.logout()
    assert client.get(URL, {"jurisdiction": "massachusetts", "q": "eviction"}).status_code == 403


def test_endpoint_search_ignores_current_facets_and_validates_without_saving(signed_in, index, catalog):
    params = {"jurisdiction": "massachusetts", "q": "eviction", "court": "wrong-court", "case_type": "wrong"}
    response = signed_in.get(URL, params)
    assert response.status_code == 200
    assert response.json()["total"] == 2
    assert response["Cache-Control"] == "no-store"
    path_id = response.json()["results"][0]["id"]
    response = signed_in.get(URL, {"jurisdiction": "massachusetts", "path_id": path_id})
    assert response.status_code == 200
    assert response.json()["path"]["options"]
    assert not FilingDraft.objects.exists()
    assert (
        signed_in.get(URL, {"jurisdiction": "massachusetts", "path_id": path_id, "existing_case": "yes"}).status_code
        == 409
    )
    catalog.courts.side_effect = requests.Timeout()
    assert signed_in.get(URL, {"jurisdiction": "massachusetts", "path_id": path_id}).status_code == 503
    assert signed_in.get(URL, {"jurisdiction": "massachusetts", "path_id": "bad"}).status_code == 400


def test_using_a_path_remembers_its_zip_newest_first(signed_in, index, catalog, django_user_model):
    path_id = signed_in.get(URL, {"jurisdiction": "massachusetts", "q": "eviction"}).json()["results"][0]["id"]
    user = django_user_model.objects.get(username="searcher")
    with patch("efile.api.filing_code_search.validate_path", return_value={}):
        for postal_code in ("02108", "02139", "", "not-a-zip", "02108", "01060-1234", "02445"):
            signed_in.get(URL, {"jurisdiction": "massachusetts", "path_id": path_id, "zip": postal_code})
    user.refresh_from_db()
    assert user.recent_case_zips == ["02445", "01060", "02108"]
    with patch("efile.api.filing_code_search.validate_path", side_effect=ValueError("gone")):
        signed_in.get(URL, {"jurisdiction": "massachusetts", "path_id": path_id, "zip": "02210"})
    user.refresh_from_db()
    assert user.recent_case_zips[0] == "02445"


def test_zip_shortcuts_put_the_filers_own_zip_before_recent_ones(rf, django_user_model):
    from efile.views.extraction_review import _zip_shortcuts

    user = django_user_model.objects.create_user(
        username="filer", tyler_jurisdiction="illinois", recent_case_zips=["60187", "60601"]
    )
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois")
    FilingParty.objects.create(draft=draft, role="filer", zip_code="60601-1234")
    request = rf.post("/")
    request.user = user
    request.session = {}
    assert _zip_shortcuts(request, draft, "illinois") == [
        {"zip": "60601", "kind": "address"},
        {"zip": "60187", "kind": "recent"},
    ]


def test_management_command_attempts_each_jurisdiction_and_reports_failure():
    with patch(
        "efile.management.commands.refresh_filing_code_index.refresh_index", side_effect=[ValueError("bad"), 2, 3]
    ) as refresh:
        with pytest.raises(CommandError, match="illinois"):
            call_command("refresh_filing_code_index", stdout=Mock(), stderr=Mock())
    assert [call.args[0] for call in refresh.call_args_list] == ["illinois", "massachusetts", "vermont"]


@pytest.mark.django_db(transaction=True)
def test_worker_retries_failed_state_soon_and_skips_fresh_states(index):
    from efile.models import FilingCodeIndex

    command = "efile.management.commands.refresh_filing_code_index"

    # Massachusetts is already ready. Vermont succeeds on the first pass;
    # Illinois fails once and must be retried without rebuilding either.
    attempts = 0

    def refresh(jurisdiction, **kwargs):
        nonlocal attempts
        if jurisdiction == "illinois" and attempts == 0:
            attempts += 1
            raise requests.Timeout("temporary failure")
        FilingCodeIndex.objects.create(
            jurisdiction=jurisdiction,
            source_url=index.source_url,
            rules_digest=index.rules_digest,
            refreshed_at=index.refreshed_at,
        )
        return 2

    with (
        patch(f"{command}.refresh_index", side_effect=refresh) as build,
        patch(f"{command}.time.sleep", side_effect=[None, KeyboardInterrupt]) as sleep,
    ):
        with pytest.raises(KeyboardInterrupt):
            call_command("refresh_filing_code_index", interval=86400, stdout=Mock(), stderr=Mock())
    assert [call.args[0] for call in build.call_args_list] == ["illinois", "vermont", "illinois"]
    assert [call.args[0] for call in sleep.call_args_list] == [60, 86400]


def test_catalog_retries_temporary_http_failure():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class Handler(BaseHTTPRequestHandler):
        attempts = 0

        def do_GET(self):
            Handler.attempts += 1
            self.send_response(503 if Handler.attempts == 1 else 200)
            self.end_headers()
            self.wfile.write(b'[{"code": "civil", "name": "Civil"}]')

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever)
    thread.start()
    catalog = CodeCatalog("illinois")
    catalog.base = f"http://127.0.0.1:{server.server_port}/"
    try:
        assert catalog.categories("court") == [option("civil", "Civil")]
        assert Handler.attempts == 2
    finally:
        catalog.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_classification_change_clears_dependent_codes_but_keeps_documents(django_user_model):
    user = django_user_model.objects.create_user(username="draft-owner")
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        court_code="old",
        case_category_code="civil",
        case_type_code="eviction",
        quoted_fee_total="10",
        quoted_fee_fingerprint="old-fees",
        previous_case_id="old-case",
    )
    doc = FilingDocument.objects.create(
        draft=draft,
        role="lead",
        name="keep.pdf",
        s3_key="keep.pdf",
        filing_type_code="old",
        document_type_code="private",
        document_type_confirmed=True,
        filing_component_code="old-component",
        requested_optional_services=["old-service"],
    )
    party = FilingParty.objects.create(draft=draft, party_type="old", first_name="Keep")
    assert not clear_changed_classification(draft, "old", "civil", "eviction")
    assert clear_changed_classification(draft, "new", "civil", "eviction")
    doc.refresh_from_db()
    party.refresh_from_db()
    draft.refresh_from_db()
    assert doc.s3_key == "keep.pdf"
    assert not doc.filing_type_code and not doc.document_type_code and not doc.document_type_confirmed
    assert not doc.filing_component_code and not doc.requested_optional_services
    assert not party.party_type and party.first_name == "Keep"
    assert not draft.quoted_fee_total and not draft.previous_case_id and not draft.filing_type_code


def test_zip_filter_scopes_groups_contexts_courts_and_paths(catalog):
    catalog.courts.return_value = [option("cook:cvd1", "Cook County"), option("adams", "Adams County")]
    refresh_index("illinois")
    index = current_index("illinois")
    all_groups = search_grouped_paths(index, "eviction")
    scoped = search_grouped_paths(index, "eviction", postal_code="60601")
    assert scoped["location_counties"] == ["Cook"]
    group = scoped["groups"][0]
    assert group["path_count"] < all_groups["groups"][0]["path_count"]
    args = {"group_key": group["key"], "postal_code": "60601", "context": group["case_context_options"][0]["key"]}
    courts = search_grouped_paths(index, "eviction", **args)
    assert [court["code"] for court in courts["courts"]] == ["cook:cvd1"]
    assert search_grouped_paths(index, "eviction", court="adams", **args)["total"] == 0
    assert search_grouped_paths(index, "eviction", court="cook:cvd1", **args)["total"] == 1
    assert search_grouped_paths(index, "eviction")["groups"] == all_groups["groups"]


@pytest.mark.parametrize("postal_code", ["606", "00000", "abcde"])
def test_zip_filter_rejects_invalid_or_unknown_zip(catalog, postal_code):
    from efile.services.case_location import locate

    refresh_index("illinois")
    with pytest.raises(ValueError):
        locate(current_index("illinois"), postal_code)


def test_zip_filter_preserves_multiple_counties():
    from efile.services.case_location import zip_counties

    with patch("efile.utils.zip_to_county_il.COUNTY_TO_ZIPS_IL", {"Cook": ["60601"], "Lake": ["60601"]}):
        assert zip_counties("illinois", "60601-1234") == ["Cook", "Lake"]


def test_census_table_places_zips_in_every_state():
    from efile.services.case_location import zip_counties

    assert zip_counties("massachusetts", "02139") == ["Middlesex"]
    assert zip_counties("vermont", "05602") == ["Washington"]
    assert zip_counties("vermont", "02139") == []


@pytest.mark.parametrize(
    ("jurisdiction", "code", "name", "counties"),
    [
        ("illinois", "cook:cvd1", "Cook County - Civil", {"Cook"}),
        ("illinois", "jodaviess", "Jo Daviess County", {"Jo Daviess"}),
        ("vermont", "sc:grandisle", "Grand Isle Unit", {"Grand Isle"}),
        (
            "massachusetts",
            "0965:BE",
            "Juvenile Court -- Franklin-Hampshire County -- Belchertown",
            {"Franklin", "Hampshire"},
        ),
        ("massachusetts", "490", "District Court - Cambridge", {"Middlesex"}),
        ("illinois", "ilsc", "Illinois Supreme Court", set()),
    ],
)
def test_courts_are_placed_by_county_in_their_code_or_name_or_by_town(jurisdiction, code, name, counties):
    from efile.services.case_location import court_counties

    assert set(court_counties(jurisdiction, code, name)) == counties


def test_county_states_keep_courts_that_name_no_county(catalog):
    from efile.services.case_location import locate

    refresh_index("illinois")
    index = current_index("illinois")
    index.vocabulary["courts"] = {"cook:cvd1": "Cook County", "adams": "Adams County", "ilsc": "Supreme Court"}
    assert locate(index, "60601") == {"counties": ["Cook"], "courts": ["cook:cvd1", "ilsc"]}


def test_matcher_states_narrow_only_courts_the_matcher_knows(index):
    from efile.services import court_location
    from efile.services.case_location import locate

    record = lambda code: Mock(tyler_code=code)  # noqa: E731
    finder = Mock()
    finder.find_by_postal_code.return_value = [Mock(records=[record("0490")])]
    finder.catalog.records = [record("490"), record("500")]
    index.vocabulary["courts"] = {
        "490": "Cambridge",
        "500": "Somewhere else",
        "sjc": "Supreme Judicial Court",
        "new-1": "District Court - Winchendon",
        "new-2": "District Court - Somerville",
    }
    with patch.object(court_location, "_finder", return_value=finder):
        # Courts MACourts doesn't know fall back to their town's county.
        assert locate(index, "02139")["courts"] == ["490", "new-2", "sjc"]
        finder.find_by_postal_code.return_value = []
        with pytest.raises(ValueError):
            locate(index, "02139")
    finder.find_by_postal_code.assert_called_with("02139")


def test_vermont_narrows_units_by_name_and_keeps_statewide_courts():
    from efile.services import court_location

    finder = Mock()
    finder.find_units.return_value = [Mock(unit="Washington")]
    courts = {"sc:washington": "Washington Unit", "sc:orange": "Orange Unit", "vermont:supreme": "Supreme Court"}
    with patch.object(court_location, "_vermont_finder", return_value=finder):
        assert court_location.courts_for_postal_code("vtcourts", "05602", courts) == {
            "matched": {"sc:washington"},
            "unknown": {"vermont:supreme"},
        }


def test_refresh_records_each_courts_name(catalog):
    refresh_index("illinois")
    assert current_index("illinois").vocabulary["courts"]


def test_overlapping_words_do_not_need_a_synonym_explanation():
    data = search_context(
        "illinois", "response to complaint", [("filing_type", "Answer/Response to Complaint/Petition")]
    )
    assert data["match_reason"] is None
    data = search_context("illinois", "complaint response", [("filing_type", "Answer/Response to Complaint/Petition")])
    assert data["match_reason"] is None
    data = search_context("illinois", "unlawful detainer", [("filing_type", "Eviction")])
    assert data["match_reason"]["kind"] == "related_concept"


def test_path_description_uses_case_meaning_not_document_definition(index):
    path = search_paths(index, "eviction")["results"][0]
    assert path["case_description"] == concept_meaning("eviction", "massachusetts", path["court"]["name"])["text"]
    debt = search_paths(index, "debt")["results"][0]
    assert "creditor" in debt["case_description"]


@pytest.mark.parametrize(
    ("name", "property_kind", "relief", "text"),
    [
        ("Commercial - Possession Only", "commercial", "possession", "business"),
        ("Residential - Eviction", "residential", "unknown", "does not say whether money"),
        ("Residential - Possession and Rent", "residential", "money", "money claim"),
        ("Ejectment", "unknown", "unknown", "legal right"),
    ],
)
def test_eviction_case_type_guidance(name, property_kind, relief, text):
    from efile.services.case_type_guidance import case_guidance

    guidance = case_guidance("illinois", "Eviction", name, "Answer/Response to Complaint/Petition")
    assert guidance["property"] == property_kind
    assert guidance["relief"] == relief
    assert guidance["role"] == "tenant"
    assert text in guidance["text"]
    assert case_guidance("illinois", "Contracts", "Commercial")["topic"] != "eviction"
    assert case_guidance("vermont", "Eviction", name)["topic"] == "eviction"


def test_eviction_facets_filter_before_pagination_and_preserve_unknowns(catalog):
    catalog.categories.return_value = [option("eviction", "Eviction")]
    catalog.types.return_value = [
        option("business", "Commercial"),
        option("home", "Residential - Possession Only"),
        option("other", "Ejectment"),
    ]
    catalog.filings.side_effect = None
    catalog.filings.return_value = [
        option("answer", "Answer"),
        option("complaint", "Complaint"),
        option("motion", "Motion"),
    ]
    refresh_index("illinois")
    index = current_index("illinois")
    data = search_grouped_paths(
        index, "eviction", role="tenant", property_kind="residential", relief="possession", limit=1
    )
    assert data["eviction_facets"]
    assert data["total"] == 2  # Answer and Motion; landlord Complaint excluded.
    group = data["groups"][0]
    assert "Eviction › Commercial" not in group["case_contexts"]
    assert "Eviction › Ejectment" in group["case_contexts"]  # Unspecified property/relief stays visible.
    paths = search_grouped_paths(
        index,
        "eviction",
        group_key=group["key"],
        court="housing",
        role="tenant",
        property_kind="residential",
        relief="possession",
    )
    assert paths["total"] == 2
    assert {p["case_type"]["code"] for p in paths["results"]} == {"home", "other"}
    assert any("home or apartment" in p["case_description"] for p in paths["results"])
    assert search_grouped_paths(index, "eviction")["total"] == 3


def test_small_claim_amount_filters_groups_courts_and_exact_paths(catalog):
    catalog.categories.return_value = [option("sc", "Small Claims")]
    catalog.types.return_value = [
        option("low", "Contract (Up to $2,500)"),
        option("high", "Contract ($2,500.01 to $10K)"),
        option("unknown", "Other"),
    ]
    catalog.filings.side_effect = None
    catalog.filings.return_value = [option("complaint", "Complaint")]
    refresh_index("illinois")
    index = current_index("illinois")
    args = {"case_topic": "small_claims", "case_filters": {"amount": "250001:1000000"}}
    data = search_grouped_paths(index, "small claims", **args)
    assert data["case_topic"] == "small_claims"
    assert data["total"] == 1
    group = data["groups"][0]
    assert group["path_count"] == 4
    assert not any("Up to $2,500" in label for label in group["case_contexts"])
    detail = search_grouped_paths(index, "small claims", group_key=group["key"], court="housing", **args)
    assert {path["case_type"]["code"] for path in detail["results"]} == {"high", "unknown"}
    assert detail["total"] == 2
    assert len(search_grouped_paths(index, "small claims", group_key=group["key"], **args)["courts"]) == 2
    assert search_grouped_paths(index, "small claims")["groups"][0]["path_count"] == 6


@pytest.mark.parametrize(
    ("name", "action"),
    [
        ("Small Claims Complaint", "start"),
        ("Answer/Response to Complaint/Petition", "respond"),
        ("Response to Motion to Dismiss", "respond"),
        ("Motion to Dismiss", "ask"),
        ("Stipulation to Dismiss", "settle"),
        ("Agreed Order", "settle"),
        ("Petition for Dissolution of Marriage", "start"),
        ("Petition to Modify Custody", "ask"),
        ("Certificate of Service", "proof"),
        ("Proposed Order", "order"),
        ("Notice of Hearing", "notice"),
        ("Additional Defendants", ""),
    ],
)
def test_filing_names_map_to_a_plain_language_action(name, action):
    from efile.services.filing_code_search import filing_action

    assert filing_action(name) == action


def test_jury_government_and_fee_variants_share_one_group():
    from efile.services.filing_code_search import filing_group_key, unqualified_label

    names = [
        "Complaint / Petition - Fraud - Fee",
        "Complaint / Petition - Fraud (Jury - 12) - Fee",
        "Complaint / Petition - Fraud (Govn't) - Fee",
        "Complaint / Petition - Fraud (Jury - 6) (Govn't)",
    ]
    assert {unqualified_label(name) for name in names} == {"Complaint / Petition - Fraud"}
    assert len({filing_group_key(name, "illinois") for name in names}) == 1
    assert filing_group_key("Complaint / Petition - Fraud", "illinois") != filing_group_key(
        "Complaint / Petition - Breach Of Contract", "illinois"
    )


def test_name_matches_lead_and_case_only_matches_are_counted_apart():
    from efile.services.filing_code_search import action_options, strong_matches

    groups = [
        {"name_rank": 24, "_labels": [{"name": "Small Claims Complaint"}]},
        {"name_rank": 16, "_labels": [{"name": "Notice of Small Claims"}]},
        {"name_rank": 8, "_labels": [{"name": "Claim Affidavit"}]},
        {"name_rank": 0, "_labels": [{"name": "Affidavit"}, {"name": "Exhibit"}]},
    ]
    assert strong_matches(groups) == 2
    assert strong_matches([{"name_rank": 0}]) == 0
    assert action_options(groups) == [
        {"value": "start", "label": "Start a case", "count": 1},
        {"value": "proof", "label": "Supporting papers", "count": 2},
        {"value": "notice", "label": "Notice", "count": 1},
    ]


def test_endpoint_filters_by_action_and_rejects_unknown_ones(signed_in, index):
    params = {"jurisdiction": "massachusetts", "q": "eviction", "grouped": "true"}
    data = signed_in.get(URL, params).json()
    assert {"actions", "strong_total"} <= set(data)
    assert signed_in.get(URL, {**params, "action": "bogus"}).status_code == 400
    for option in data["actions"]:
        narrowed = signed_in.get(URL, {**params, "action": option["value"]}).json()
        assert narrowed["total"] == option["count"]


@pytest.mark.parametrize(
    ("category", "case_type", "filing", "expected"),
    [
        (
            "Civil",
            "Other Personal Injury Complaint - Jury - Self-Represented Litigant",
            "Complaint / Petition - Personal Injury (Jury - 6) (Govn't) - Fee",
            {"jury": "6", "representation": "self", "government": "yes", "amount": None},
        ),
        (
            "Civil - Amount Claimed Greater Than $10,000",
            "Personal Injury Complaint - Non-Jury",
            "Complaint / Petition - Personal Injury - Fee",
            {
                "jury": "none",
                "representation": "lawyer",
                "government": "no",
                "amount": {"value": "1000001:", "label": "More than $10,000"},
            },
        ),
        (
            "Civil",
            "Personal Injury Complaint - Non-Jury- Small Claims $0 to $10,000- SRL",
            "Complaint / Petition - Personal Injury - Fee",
            {
                "jury": "none",
                "representation": "self",
                "government": "no",
                "amount": {"value": "0:1000000", "label": "$0 to $10,000"},
            },
        ),
        (
            "Personal Injury/Wrongful Death",
            "Other Personal Injury/Wrongful Death - Jury Demand",
            "Complaint / Petition - Personal Injury (Jury - 12) - Fee",
            {"jury": "12", "representation": "lawyer", "government": "no", "amount": None},
        ),
    ],
)
def test_paths_are_tagged_with_the_choices_courts_code_separately(category, case_type, filing, expected):
    from efile.services.filing_code_search import path_qualifiers

    assert path_qualifiers(category, case_type, filing) == expected


def test_glossary_finds_terms_once_with_state_wording():
    from efile.services.glossary import glossary_for

    terms = glossary_for(
        "illinois", ["Complaint / Petition - Intentional Tort", "Personal Injury - Motor Vehicle Subrogation - SRL"]
    )
    assert [term["label"] for term in terms] == ["Intentional tort", "Subrogation"]
    assert glossary_for("illinois", ["Tortious Interference? No: Torte Bakery"]) == []
    assert "Illinois' older name" in glossary_for("illinois", ["Forcible Entry and Detainer"])[0]["text"]
    assert glossary_for("massachusetts", ["Summary Process Complaint"])[0]["label"] == "Summary process"
    assert glossary_for("illinois", ["Summary Process Complaint"]) == []


def test_case_type_variants_are_shown_once_in_previews():
    from efile.services.filing_code_search import case_contexts, unqualified_case_type

    assert unqualified_case_type("Personal Injury Complaint - Non-Jury- Small Claims $0 to $10,000- SRL") == (
        "Personal Injury Complaint"
    )
    assert unqualified_case_type("Other Personal Injury/Wrongful Death - Jury Demand") == (
        "Other Personal Injury/Wrongful Death"
    )
    labels = [
        {"category": "Civil", "case_type": name, "path_count": 1, "rank": 1}
        for name in [
            "Personal Injury Complaint - Jury",
            "Personal Injury Complaint - Non-Jury - Self-Represented Litigant",
        ]
    ]
    assert case_contexts(labels) == ["Civil › Personal Injury Complaint"]


def test_fee_waivers_ask_the_court_rather_than_start_a_case():
    from efile.services.filing_code_search import filing_action

    assert filing_action("Fee Waiver Petition Filed - Petitioner/Plaintiff") == "ask"


def test_endpoint_offers_and_applies_case_categories(signed_in, index):
    params = {"jurisdiction": "massachusetts", "q": "eviction", "grouped": "true"}
    data = signed_in.get(URL, params).json()
    assert data["categories"] and all({"value", "label", "count"} <= set(item) for item in data["categories"])
    first = data["categories"][0]
    narrowed = signed_in.get(URL, {**params, "category": first["value"]}).json()
    assert narrowed["total"] == first["count"]


def test_data_files_have_no_duplicate_keys():
    # YAML lets a repeated key silently replace the first, as a second
    # "sealing:" concept once did.
    from pathlib import Path

    import yaml

    class Strict(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        keys = [loader.construct_object(key, deep=deep) for key, _ in node.value]
        assert len(keys) == len(set(keys)), f"duplicate keys in {node.start_mark}"
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    for path in (Path(__file__).resolve().parents[1] / "data").glob("*.yaml"):
        yaml.load(path.read_text(), Loader=Strict)


def test_seal_searches_find_impound_and_redaction_filings():
    from efile.services.filing_code_search import search_tokens

    for name in ["Impound/Seal", "Filed Under Seal", "Motion for Redaction & Confidential Filing"]:
        assert "conceptsealing" in search_tokens(name, "illinois")
    assert "conceptsealing" in search_tokens("seal eviction", "illinois", query=True)
    assert "conceptsealing" not in search_tokens("Other Document Not Listed (Confidential)", "illinois")


def test_other_stage_count_points_new_case_searches_at_existing_filings(catalog):
    from efile.services.filing_code_search import search_grouped_paths

    refresh_index("massachusetts")
    index = current_index("massachusetts")
    new = search_grouped_paths(index, "eviction", initial=True)
    existing = search_grouped_paths(index, "eviction", initial=False)
    assert new["other_stage_total"] == existing["total"]
    assert existing["other_stage_total"] == new["total"]
    assert search_grouped_paths(index, "eviction", initial=True, offset=20)["other_stage_total"] is None


def test_search_reuses_index_metadata_across_results(index, django_assert_num_queries):
    # Keep real serialization: it accesses path.index for vocabulary and state
    # guidance. Every result must reuse the already loaded index, without a
    # join that decodes its potentially multi-megabyte JSON again per row.
    from efile.models import FilingCodeIndex
    from efile.services.filing_code_search import serialize_path

    def serialize_known_index(path, **kwargs):
        assert path.index is index
        return serialize_path(path, **kwargs)

    with (
        patch.object(FilingCodeIndex, "from_db", side_effect=AssertionError("Refetched index metadata")),
        patch("efile.services.filing_code_search.serialize_path", side_effect=serialize_known_index),
        django_assert_num_queries(2),
    ):
        result = search_paths(index, "eviction", limit=500)
    assert result["total"] == 2
    assert {path["court"]["code"] for path in result["results"]} == {"housing", "district"}


def test_search_index_leaves_import_snapshots_lazy(index, django_assert_num_queries):
    expected_snapshots = index.court_snapshots
    with django_assert_num_queries(1):
        loaded = current_index("massachusetts")
        assert loaded is not None
        assert loaded.vocabulary == index.vocabulary
    assert loaded.get_deferred_fields() == {"court_snapshots"}
    with django_assert_num_queries(1):
        assert loaded.court_snapshots == expected_snapshots


def test_search_leaves_out_heading_courts_even_in_an_older_index(index, monkeypatch):
    monkeypatch.setattr("efile.services.filing_code_search.heading_court_codes", lambda jurisdiction: {"district"})
    paths, _ = matching_paths(index, "eviction", True)
    codes = set(paths.values_list("court__code", flat=True))
    assert codes
    assert "district" not in codes


def test_live_validation_rejects_a_heading_court(index, catalog, monkeypatch):
    path = index.paths.filter(initial=True).first()
    monkeypatch.setattr(
        "efile.services.filing_code_search.heading_court_codes", lambda jurisdiction: {path.court["code"]}
    )
    catalog.categories.reset_mock()
    with pytest.raises(ValueError, match="no longer offered"):
        validate_path(path)
    catalog.categories.assert_not_called()


def test_unreadable_court_lists_drop_nothing_and_are_not_refetched_per_search():
    cache.clear()
    with patch("efile.services.court_selection.requests.get", side_effect=requests.Timeout()) as get:
        assert court_selection.heading_court_codes("massachusetts") == frozenset()
        assert court_selection.heading_court_codes("massachusetts") == frozenset()
    assert get.call_count == 1
    cache.clear()


def test_labels_find_what_paths_find(catalog):
    catalog.courts.return_value = [option(f"court-{i}", f"County {i}") for i in range(12)]
    catalog.filings.side_effect = lambda court, category, case_type, initial: [
        option(
            f"filing-{court}", "Eviction Complaint" if int(court.split("-")[1]) % 2 else "Summary Process Complaint"
        ),
        option("motion", "Motion to Dismiss" if initial else "Motion to Vacate"),
    ]
    refresh_index("massachusetts")
    index = current_index("massachusetts")

    def results(labelled):
        FilingCodeIndex.objects.filter(pk=index.pk).update(labels_built_at=timezone.now() if labelled else None)
        cache.clear()
        caches["shared"].clear()
        current = current_index("massachusetts")
        found = {}
        for query in ("eviction", "County 7 eviction", "motion", "debt collection", "county", "zzzz"):
            grouped = search_grouped_paths(current, query)
            found[query] = (
                grouped["groups"],
                grouped["other_stage_total"],
                [search_grouped_paths(current, query, group_key=g["key"])["courts"] for g in grouped["groups"]],
                # A case ZIP limits the search to the courts serving it.
                filing_groups(current, query, True, ("court-3", "court-7")),
            )
        return found

    labelled = results(True)
    assert labelled["County 7 eviction"][0][0]["path_count"] == 1
    assert labelled["motion"][2][0]
    assert results(False) == labelled


def test_shared_cache_serves_another_worker(index):
    search_grouped_paths(index, "eviction")
    # Another worker's local cache is empty; the search is not repeated.
    cache.clear()
    with CaptureQueriesContext(connection) as queries:
        search_grouped_paths(index, "eviction")
    assert not [query for query in queries.captured_queries if "filingcodelabel" in query["sql"].lower()]


@pytest.mark.parametrize("transport", ["legacy", "bulk"])
def test_vermont_initial_complaint_aliases_keep_stage_and_division(catalog, transport):
    """VT Judiciary Civil/Family guides + issue 325 workbook rows 4–5, 24–31."""
    catalog.courts.return_value = [option("vt", "Vermont Superior Court")]
    catalog.categories.return_value = [
        option("family", "Family"),
        option("civil", "Civil"),
        option("env", "Environmental"),
    ]
    catalog.types.side_effect = lambda court, category, initial: {
        "family": [
            option("rfa", "Relief from Abuse"),
            option("foreign", "Registration of a Foreign Order"),
            option("divorce", "Dissolution Contested with No Minor Children"),
        ],
        "civil": [
            option("small", "Small Claims $1,000 or less"),
            option("admin", "Administrative Document - Answer No Complaint Filed"),
        ],
        "env": [option("appeal", "Environmental Appeal")],
    }[category]
    catalog.filings.side_effect = None
    catalog.filings.return_value = [option("initial", "Initial Filing"), option("answer", "Answer to Complaint")]
    if transport == "bulk":
        from efile.services.filing_code_sync import export_entries

        data = {
            "court": catalog.courts.return_value[0],
            "categories": catalog.categories.return_value,
            "case_types": [
                {**row, "case_category": category["code"], "initial": initial}
                for category in catalog.categories.return_value
                for initial in (True, False)
                for row in catalog.types("vt", category["code"], initial)
            ],
            "filing_types": [
                {**row, "case_category": "", "case_type": "", "timing": "Both"} for row in catalog.filings.return_value
            ],
        }
        # Bulk catalogs store one timing flag per type; retain the initial
        # types and add a separate existing type to enable subsequent paths.
        data["case_types"] = [row for row in data["case_types"] if row["initial"]]
        data["case_types"].extend(
            {
                "code": "existing-" + row["code"],
                "name": row["name"],
                "case_category": row["case_category"],
                "initial": False,
            }
            for row in list(data["case_types"])
        )
        with patch("efile.services.filing_code_search.catalog_entries", return_value=export_entries(data, "vermont")):
            refresh_index("vermont")
    else:
        refresh_index("vermont")
    index = current_index("vermont")
    for query in (
        "complaint",
        "file a complaint",
        "small claims complaint",
        "divorce complaint",
        "end marriage",
        "relief from abuse complaint",
        "RFA",
        "start relief from abuse case",
    ):
        result = search_paths(index, query)
        initial = [row for row in result["results"] if row["filing_type"]["code"] == "initial"]
        assert initial, query
        assert initial[0]["case_type"]["code"] in {"rfa", "small", "divorce"}
        assert "complaint that starts" in initial[0]["explanation"]
        grouped = search_grouped_paths(index, query)
        assert "Initial Filing" in [group["name"] for group in grouped["groups"][:5]]
    assert not any(
        row["filing_type"]["code"] == "initial"
        for row in search_paths(index, "relief from abuse complaint", initial=False)["results"]
    )
    admin = index.paths.get(initial=True, case_type__code="admin", filing_type__code="initial")
    assert " complaint " not in admin.filing_terms
    assert admin.explanation == ""
    path = index.paths.get(initial=True, case_type__code="rfa", filing_type__code="initial")
    validated = validate_path(path)
    assert validated["filing_type"] == option("initial", "Initial Filing")


def test_illinois_eviction_sealing_requires_specific_existing_motion_and_case(catalog):
    """Illinois Courts' standardized motion; DuPage snapshot label 309683."""
    catalog.courts.return_value = [option("dupage", "DuPage County")]
    catalog.categories.return_value = [option("civil", "Civil")]
    catalog.types.return_value = [option("eviction", "Residential - Eviction"), option("criminal", "Criminal")]
    title = "Motion to Remove Eviction Court File from Public Record"
    catalog.filings.side_effect = None
    catalog.filings.return_value = [
        option("remove", title),
        option("redact", "Motion for Redaction"),
        option("seal", "Motion to Seal"),
        option("expunge", "Expungement"),
        option("order", "Order to Remove Eviction Court File from Public Record"),
    ]
    refresh_index("illinois")
    index = current_index("illinois")
    for query in (
        "seal eviction",
        "seal my eviction",
        "eviction record sealing",
        "hide eviction case",
        "remove eviction from public record",
    ):
        data = search_paths(index, query, initial=False)
        assert data["total"] == 1, query
        path = data["results"][0]
        assert path["filing_type"] == option("remove", title)
        assert path["case_type"]["code"] == "eviction"
        if query.startswith(("seal", "hide")):
            assert path["match_reason"]["concepts"] == ["eviction_record_sealing"]
        assert search_paths(index, query, initial=True)["total"] == 0
        groups = search_grouped_paths(index, query, initial=False)["groups"]
        assert [group["name"] for group in groups] == [title]
    # Generic language elsewhere and document-finder tokenization stay separate.
    assert "conceptevictionrecordsealing" not in search_tokens(
        "seal eviction", "massachusetts", query=True, path_query=True
    )
    assert "conceptevictionrecordsealing" not in search_tokens("seal eviction", "illinois", query=True)
    assert "conceptevictionrecordsealing" not in search_tokens("public record", "illinois", query=True, path_query=True)


def test_captured_vermont_paths_are_discoverable_in_each_audited_court(catalog):
    """Replay the dated 154-path capture; this does not measure a full live index."""
    import json
    from pathlib import Path

    from efile.services.filing_code_search import indexed_path

    fixture = (
        Path(__file__).resolve().parents[3]
        / "docs/developer-notes/fixtures/vermont-initial-filing-audit-2026-10-13.json"
    )
    audit = json.loads(fixture.read_text())
    entries = []
    county_codes = []
    for court in audit["courts"]:
        if not court["rfa"]:
            continue
        county_codes.append(court["court_code"])
        for family, category in (("rfa", option("210", "Family")), ("small_claims", option("7", "Civil"))):
            for row in court[family]:
                for filing in row["filing_options"]:
                    entries.append(
                        indexed_path(
                            "vermont",
                            {
                                "court": option(court["court_code"], court["court_name"]),
                                "case_category": category,
                                "case_type": option(row["case_type_id"], row["case_type_name"]),
                                "filing_type": filing,
                            },
                            True,
                        )
                    )
    assert len(county_codes) == 14
    assert len(entries) == 154
    with patch("efile.services.filing_code_search.catalog_entries", return_value=iter(entries)):
        refresh_index("vermont")
    index = current_index("vermont")
    for query, expected_count, expected_code in (
        ("relief from abuse complaint", 3, "9564"),
        ("RFA", 3, "9564"),
        ("small claims complaint", 8, "9563"),
    ):
        groups = search_grouped_paths(index, query)["groups"]
        assert groups[0]["name"] == "Initial Filing"
        for court_code in county_codes:
            found = search_paths(index, query, court=court_code)
            assert found["total"] == expected_count
            assert {row["filing_type"]["code"] for row in found["results"]} == {expected_code}
            assert {row["court"]["code"] for row in found["results"]} == {court_code}


def test_replacement_search_filters_all_saved_codes_before_pagination(index):
    result = search_paths(
        index, "complaint", court="housing", category="civil", case_type="sp", filing_type="spcomplaint", limit=1
    )
    assert result["total"] == 1
    assert result["results"][0]["filing_type"]["code"] == "spcomplaint"
    assert search_paths(index, "complaint", court="housing", case_type="sp", filing_type="complaint")["total"] == 0
