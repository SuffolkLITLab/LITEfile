"""Tests for finding the documents for what you want to file.

The first group builds a small configuration in a temporary directory, so it
describes the contract partners write against. The second checks the shipped
Illinois index, and the last drives the pages.
"""

from unittest.mock import patch
from urllib.parse import quote

import pytest
import yaml
from django.urls import reverse

from efile.models import FilingDraft
from efile.services.document_checklists import resolve_document_checklist
from efile.services.document_finder import (
    configured_links,
    has_document_finder,
    intent_detail,
    search_intents,
)
from efile.utils.config_loader import JurisdictionConfigLoader
from efile.workflow import ExistingCase, WorkflowStepKey

STATE_CONFIG = {
    "jurisdiction": {"display_name": "Testland", "help_name": "Testland Court Help", "help_url": "https://help.test/"},
    "case_types": {
        "name_change": {
            "matches": {"names": ["Change of Name"]},
            "about": {"summary": "A name change makes a new name official."},
            "documents": {
                "petition": {"label": "Request for name change", "requirement": "always", "role": "lead"},
                "fee_waiver": {"label": "Request to waive court fees", "requirement": "sometimes"},
                "minor_consent": {
                    "label": "Consent from the child",
                    "requirement": "sometimes",
                    "when": {"lead_filing_type_names": ["Request for Name Change (Minor Children)"]},
                },
            },
        },
        "eviction": {
            "matches": {"names": ["Residential - Eviction"]},
            "filer_roles": {"landlord": {"label": "The landlord"}, "tenant": {"label": "The tenant"}},
            "documents": {
                "complaint": {"label": "Eviction complaint", "requirement": "always", "for_roles": ["landlord"]},
                "appearance": {"label": "Appearance form", "requirement": "always", "for_roles": ["tenant"]},
            },
        },
    },
    "court_specific_requirements": {
        "big:county": {
            "case_types": {
                "name_change": {
                    "documents": {"cover_sheet": {"label": "County cover sheet", "requirement": "always"}},
                }
            }
        }
    },
    "document_finder": {
        "intents": {
            "change_my_name": {
                "title": "Change my name",
                "summary": "Make a new name official.",
                "keywords": ["new name"],
                "filing_path": "new",
                "checklist": {"case_type": "name_change"},
                "resources": [
                    {"kind": "help", "label": "About name changes", "url": "https://help.test/name"},
                    {"kind": "interview", "label": "Name change interview", "url": "https://forms.test/name"},
                    {"kind": "form", "label": "Not a web link", "url": "javascript:alert(1)"},
                    {"kind": "form", "label": "Plain http", "url": "http://forms.test/plain.pdf"},
                    {"kind": "podcast", "label": "Unknown kind", "url": "https://forms.test/pod"},
                ],
                "document_resources": {
                    "petition": [{"kind": "form", "label": "Blank request", "url": "https://forms.test/request.pdf"}],
                },
            },
            "change_my_childs_name": {
                "title": "Change my child's name",
                "filing_path": "new",
                "checklist": {"case_type": "name_change"},
                "lead_filing_type_name": "Request for Name Change (Minor Children)",
            },
            "evict_a_tenant": {
                "title": "Evict a tenant",
                "keywords": ["eviction"],
                "filing_path": "new",
                "checklist": {"case_type": "eviction"},
                "filer_role": "landlord",
            },
            "respond_to_an_eviction": {
                "title": "Respond to an eviction",
                "keywords": ["eviction", "kicked out"],
                "filing_path": "existing",
                "checklist": {"case_type": "eviction"},
                "filer_role": "tenant",
            },
            "unconfigured": {
                "title": "Adopt a pet",
                "filing_path": "new",
                "checklist": {"case_type": "pet_adoption"},
            },
        }
    },
}


@pytest.fixture
def finder_config(tmp_path):
    (tmp_path / "base-case-types.yaml").write_text(yaml.safe_dump({}))
    (tmp_path / "states").mkdir(exist_ok=True)
    (tmp_path / "states" / "testland.yaml").write_text(yaml.safe_dump(STATE_CONFIG, sort_keys=False))
    (tmp_path / "states" / "emptyland.yaml").write_text(yaml.safe_dump({"jurisdiction": {"display_name": "E"}}))

    loader = JurisdictionConfigLoader(config_dir=tmp_path)
    with (
        patch("efile.services.document_checklists.config_loader", loader),
        patch("efile.services.document_finder.config_loader", loader),
    ):
        yield loader


def _ids(found):
    return [result["id"] for result in found["results"]]


def _detail(jurisdiction, intent_id):
    detail = intent_detail(jurisdiction, intent_id)
    assert detail is not None, intent_id
    return detail


# --- Search -------------------------------------------------------------------


def test_no_query_lists_every_intent_in_config_order(finder_config):
    found = search_intents("testland")

    assert found["searched"] is False
    assert _ids(found) == [
        "change_my_name",
        "change_my_childs_name",
        "evict_a_tenant",
        "respond_to_an_eviction",
        "unconfigured",
    ]


def test_plain_language_search_finds_the_matching_intents(finder_config):
    assert _ids(search_intents("testland", "I want to change my name")) == ["change_my_name", "change_my_childs_name"]


def test_search_matches_keywords_and_word_forms(finder_config):
    assert _ids(search_intents("testland", "my landlord kicked me out")) == ["respond_to_an_eviction"]
    assert set(_ids(search_intents("testland", "evictions"))) == {"evict_a_tenant", "respond_to_an_eviction"}


def test_search_matches_the_courts_own_case_type_name(finder_config):
    assert "change_my_name" in _ids(search_intents("testland", "Change of Name"))


def test_ambiguous_search_lists_every_candidate_rather_than_choosing(finder_config):
    found = search_intents("testland", "eviction")

    assert set(_ids(found)) == {"evict_a_tenant", "respond_to_an_eviction"}
    tenant = next(result for result in found["results"] if result["id"] == "respond_to_an_eviction")
    assert tenant["filing_path_label"] == "For a case that already exists"
    assert tenant["role_label"] == "The tenant"


def test_search_can_be_narrowed_to_starting_or_joining_a_case(finder_config):
    assert _ids(search_intents("testland", "eviction", filing_path="existing")) == ["respond_to_an_eviction"]
    assert _ids(search_intents("testland", "eviction", filing_path="new")) == ["evict_a_tenant"]


def test_a_document_label_alone_does_not_match_every_intent(finder_config):
    # "waive" appears only on checklists, never in a title or keyword, so a
    # partial match on it is not enough to list a guide.
    assert _ids(search_intents("testland", "waive my parking ticket")) == []


def test_every_query_word_found_in_the_checklist_is_a_match(finder_config):
    assert _ids(search_intents("testland", "waive court fees")) == ["change_my_name", "change_my_childs_name"]


def test_nonsense_finds_nothing(finder_config):
    found = search_intents("testland", "xyzzy plugh")

    assert found == {"results": [], "searched": True}


def test_generic_words_alone_browse_everything(finder_config):
    assert len(search_intents("testland", "court documents")["results"]) == 5


def test_a_jurisdiction_without_a_finder_has_no_results(finder_config):
    assert has_document_finder("emptyland") is False
    assert search_intents("emptyland", "change my name")["results"] == []


# --- One intent ---------------------------------------------------------------


def test_intent_shows_the_same_checklist_check_documents_would(finder_config):
    detail = _detail("testland", "change_my_name")

    shown = {item["id"]: item["label"] for group in detail["groups"] for item in group["items"]}
    expected = resolve_document_checklist("testland", case_type_name="Change of Name")
    assert shown == {item_id: item["label"] for item_id, item in expected.items()}
    assert [group["label"] for group in detail["groups"]] == ["Commonly included", "May apply"]


def test_intent_can_choose_the_lead_document_variant(finder_config):
    detail = _detail("testland", "change_my_childs_name")

    labels = [item["label"] for group in detail["groups"] for item in group["items"]]
    assert "Consent from the child" in labels


def test_intent_for_one_side_shows_only_that_sides_documents(finder_config):
    landlord = _detail("testland", "evict_a_tenant")
    tenant = _detail("testland", "respond_to_an_eviction")

    assert [item["label"] for group in landlord["groups"] for item in group["items"]] == ["Eviction complaint"]
    assert [item["label"] for group in tenant["groups"] for item in group["items"]] == ["Appearance form"]


def test_intent_without_a_configured_checklist_shows_no_list(finder_config):
    detail = _detail("testland", "unconfigured")

    assert detail["groups"] == []
    assert detail["resources"] == []


def test_unknown_intent_is_none(finder_config):
    assert intent_detail("testland", "nope") is None


def test_only_known_kinds_of_https_link_are_shown_interviews_first(finder_config):
    detail = _detail("testland", "change_my_name")

    assert [(resource["kind"], resource["url"]) for resource in detail["resources"]] == [
        ("interview", "https://forms.test/name"),
        ("help", "https://help.test/name"),
    ]
    assert detail["resources"][0]["kind_label"] == "Guided interview"


def test_links_can_be_attached_to_one_checklist_document(finder_config):
    detail = _detail("testland", "change_my_name")

    petition = next(item for group in detail["groups"] for item in group["items"] if item["id"] == "petition")
    assert [resource["url"] for resource in petition["resources"]] == ["https://forms.test/request.pdf"]


def test_documents_some_courts_add_are_listed_separately(finder_config):
    detail = _detail("testland", "change_my_name")

    assert detail["court_variants"] == [{"label": "County cover sheet", "description": ""}]


def test_intent_carries_the_partners_about_text(finder_config):
    assert _detail("testland", "change_my_name")["guidance"] == {"summary": "A name change makes a new name official."}


def test_configured_links_lists_every_shown_link(finder_config):
    assert [url for _where, resource in configured_links("testland") for url in [resource["url"]]] == [
        "https://forms.test/name",
        "https://help.test/name",
        "https://forms.test/request.pdf",
    ]


# --- The shipped Illinois index -------------------------------------------------


def test_every_illinois_intent_resolves_to_a_checklist():
    found = search_intents("illinois")

    assert found["results"]
    for result in found["results"]:
        detail = _detail("illinois", result["id"])
        assert detail["groups"], result["id"]
        assert detail["filing_path"] in {"new", "existing"}, result["id"]


def test_illinois_document_links_name_real_checklist_items():
    config = yaml.safe_load((JurisdictionConfigLoader().states_dir / "illinois.yaml").read_text(encoding="utf-8"))
    for intent_id, intent in config["document_finder"]["intents"].items():
        detail = _detail("illinois", intent_id)
        item_ids = {item["id"] for group in detail["groups"] for item in group["items"]}
        unknown = set(intent.get("document_resources") or {}) - item_ids
        assert not unknown, f"{intent_id} links documents that are not on its list: {unknown}"


def test_illinois_links_are_all_https_and_survive_cleaning():
    config = yaml.safe_load((JurisdictionConfigLoader().states_dir / "illinois.yaml").read_text(encoding="utf-8"))
    configured = 0
    for intent in config["document_finder"]["intents"].values():
        configured += len(intent.get("resources") or [])
        configured += sum(len(links) for links in (intent.get("document_resources") or {}).values())
    links = configured_links("illinois")

    assert len(links) == configured
    assert all(resource["url"].startswith("https://") and " " not in resource["url"] for _where, resource in links)


@pytest.mark.parametrize(
    ("query", "expected_first"),
    [
        ("change my name", "adult_name_change"),
        ("change my daughter's name", "child_name_change"),
        ("respond to a small claims case", "respond_to_small_claims"),
        ("someone is suing me", "respond_to_small_claims"),
        ("my landlord is kicking me out", "respond_to_an_eviction"),
        ("divorce", "divorce"),
        ("evict my tenant", "evict_a_tenant"),
    ],
)
def test_illinois_plain_language_searches(query, expected_first):
    assert _ids(search_intents("illinois", query))[0] == expected_first


def test_massachusetts_and_vermont_have_no_finder_yet():
    assert has_document_finder("massachusetts") is False
    assert has_document_finder("vermont") is False


# --- Pages ----------------------------------------------------------------------


FIND_URL = reverse("find_documents", kwargs={"jurisdiction": "illinois"})


def _intent_url(intent_id, jurisdiction="illinois"):
    return reverse("find_documents_intent", kwargs={"jurisdiction": jurisdiction, "intent_id": intent_id})


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="finder-user", tyler_jurisdiction="illinois")


@pytest.fixture
def signed_in(client, user):
    client.force_login(user)
    session = client.session
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()
    return client


@pytest.mark.django_db
def test_finder_works_without_signing_in(client):
    response = client.get(FIND_URL, {"q": "change my name"})

    content = response.content.decode()
    assert response.status_code == 200
    assert "Find the documents you need" in content
    assert "2 guides match" in content
    assert _intent_url("adult_name_change") in content


@pytest.mark.django_db
def test_empty_search_says_so_and_offers_court_help(client):
    content = client.get(FIND_URL, {"q": "xyzzy"}).content.decode()

    assert "We do not have a guide for that yet." in content
    assert "https://www.ilcourthelp.gov/" in content
    assert "833-411-1121" in content


@pytest.mark.django_db
def test_unconfigured_state_says_there_is_no_guide(client):
    response = client.get(reverse("find_documents", kwargs={"jurisdiction": "vermont"}))

    content = response.content.decode()
    assert "We do not have document guides for Vermont yet." in content
    assert "https://www.vtcourts.gov/self-help" in content
    assert 'name="q"' not in content


@pytest.mark.django_db
def test_unknown_jurisdiction_goes_to_the_jurisdiction_chooser(client):
    response = client.get("/jurisdiction/atlantis/find-documents/")

    assert response.status_code == 302
    assert response.url == reverse("efile_choose_jurisdiction")


@pytest.mark.django_db
def test_unknown_intent_is_not_found(client):
    assert client.get(_intent_url("adopt_a_dragon")).status_code == 404


@pytest.mark.django_db
@pytest.mark.usefixtures("enabled_document_checklists")
def test_intent_page_shows_checklist_and_labelled_external_links(client):
    content = client.get(_intent_url("respond_to_an_eviction")).content.decode()

    assert "Appearance form" in content
    assert "Eviction complaint" not in content
    assert "Guided interview" in content
    assert "Blank form" in content
    assert "https://www.illinoislegalaid.org/legal-information/respond-eviction" in content
    assert 'target="_blank" rel="noopener noreferrer"' in content
    assert "(opens in a new tab)" in content
    assert "These sites do not file anything for you." in content
    assert "This list is a guide, not legal advice." in content
    assert "Sign in to file" in content


@pytest.mark.django_db
def test_sign_in_returns_to_selected_guide(client, user):
    guide_url = _intent_url("respond_to_an_eviction") + "?q=eviction&source=finder"
    login_url = reverse("efile_login", kwargs={"jurisdiction": "illinois"})
    content = client.get(guide_url).content.decode()
    sign_in_url = f"{login_url}?next={quote(guide_url, safe='/')}"
    assert f'href="{sign_in_url}"' in content

    login_page = client.get(sign_in_url)
    assert login_page.context["next"] == guide_url

    def authenticate(request, **kwargs):
        user.backend = "efile.authentication.SuffolkEFileBackend"
        request.session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
        return user

    with patch("efile.views.login.authenticate", side_effect=authenticate):
        response = client.post(
            login_url,
            {"login_submit": "1", "email": "finder@example.com", "password": "password", "next": guide_url},
        )

    assert response.status_code == 302
    assert response.url == guide_url
    content = client.get(response.url).content.decode()
    assert 'name="existing_case" value="existing"' in content
    assert "File into my case" in content


@pytest.mark.django_db
@pytest.mark.usefixtures("enabled_document_checklists")
def test_intent_page_lists_what_particular_courts_add(client):
    content = client.get(_intent_url("evict_a_tenant")).content.decode()

    assert "Some courts also ask for" in content
    assert "Early Resolution Program notice" in content


@pytest.mark.django_db
def test_signed_in_filer_can_start_the_right_kind_of_filing(signed_in, user):
    content = signed_in.get(_intent_url("respond_to_an_eviction")).content.decode()
    assert 'name="existing_case" value="existing"' in content
    assert "File into my case" in content

    response = signed_in.post(
        reverse("start_filing", kwargs={"jurisdiction": "illinois"}), {"existing_case": "existing"}
    )

    draft = FilingDraft.objects.get(user=user)
    assert draft.existing_case == ExistingCase.EXISTING
    # Nothing is guessed about the court or the case type.
    assert draft.court_code == ""
    assert draft.case_type_code == ""
    assert response.url.startswith(reverse("upload_documents", kwargs={"jurisdiction": "illinois"}))


@pytest.mark.django_db
def test_coming_from_upload_returns_to_the_same_draft(signed_in, user):
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction="illinois",
        workflow_version=2,
        existing_case=ExistingCase.NEW,
        current_step=WorkflowStepKey.UPLOAD_DOCUMENTS,
    )
    upload_url = reverse("upload_documents", kwargs={"jurisdiction": "illinois"})

    upload = signed_in.get(upload_url, {"draft": draft.pk}).content.decode()
    assert f"{FIND_URL}?from=upload&amp;filing={draft.pk}" in upload

    results = signed_in.get(FIND_URL, {"from": "upload", "filing": draft.pk, "q": "name"}).content.decode()
    assert f"{upload_url}?draft={draft.pk}" in results
    assert f"{_intent_url('adult_name_change')}?from=upload&amp;filing={draft.pk}" in results

    detail = signed_in.get(_intent_url("adult_name_change"), {"from": "upload", "filing": draft.pk}).content.decode()
    assert f"{upload_url}?draft={draft.pk}" in detail
    # Already in a filing: the way on is back to it, not a second new one.
    assert "Start this filing" not in detail
    assert FilingDraft.objects.filter(user=user).count() == 1


@pytest.mark.django_db
def test_return_link_ignores_a_draft_id_that_is_not_a_number(client):
    content = client.get(FIND_URL, {"from": "upload", "filing": "1<script>"}).content.decode()

    assert "<script>" not in content.split("<main", 1)[1].split("</main>", 1)[0]
    assert f'href="{reverse("upload_documents", kwargs={"jurisdiction": "illinois"})}"' in content


@pytest.mark.django_db
def test_options_page_offers_the_finder_only_where_configured(client):
    illinois = client.get(reverse("efile_options", kwargs={"jurisdiction": "illinois"})).content.decode()
    vermont = client.get(reverse("efile_options", kwargs={"jurisdiction": "vermont"})).content.decode()

    assert FIND_URL in illinois
    assert reverse("find_documents", kwargs={"jurisdiction": "vermont"}) not in vermont
