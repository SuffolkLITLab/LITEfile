"""Pausing checklists hides suggestions without deleting saved work."""

import pytest
from django.urls import reverse

from efile.models import FilingPlan
from efile.services.filing_plans import documents_missing_from_envelope, ensure_plan_for_draft, plans_for
from efile.tests.test_filing_plan_actions import sign_in
from efile.tests.test_filing_plans import make_draft
from efile.utils.config_loader import config_loader


@pytest.fixture
def draft(django_user_model):
    user = django_user_model.objects.create_user(username="paused-plans", tyler_jurisdiction="illinois")
    return make_draft(user)


def test_checklists_are_paused_only_for_illinois():
    assert not config_loader.document_checklists_enabled("illinois")
    assert config_loader.document_checklists_enabled("massachusetts")
    assert config_loader.document_checklists_enabled("vermont")


@pytest.mark.django_db
def test_paused_checklists_preserve_saved_plans_and_document_checks(client, draft):
    plan = FilingPlan.objects.create(
        user=draft.user,
        jurisdiction="illinois",
        title="Saved name change",
        checklist={"petition": {"label": "Petition", "requirement": "always", "status": ""}},
    )
    draft.plan = plan
    draft.save(update_fields=["plan"])
    sign_in(client, draft.user, draft)
    assert ensure_plan_for_draft(draft) is None
    assert plans_for(draft.user, "illinois") == []
    assert documents_missing_from_envelope(plan, draft) == []
    response = client.get(reverse("document_checklist", kwargs={"jurisdiction": "illinois"}))
    assert response.status_code == 200
    assert response.context["checklist_groups"] == []
    assert b"petition.pdf" in response.content
    assert b"Your document plan" not in response.content
    assert b"My filing plans" not in response.content
    for name in ["filing_plans", "start_filing_from_plan"]:
        kwargs = {"jurisdiction": "illinois"}
        if name == "start_filing_from_plan":
            kwargs["plan_id"] = plan.pk
        response = client.post(reverse(name, kwargs=kwargs), {"action": "delete", "plan_id": plan.pk})
        assert response.status_code == 302
        assert response.url == reverse("efile_options", kwargs={"jurisdiction": "illinois"})
    draft.refresh_from_db()
    plan.refresh_from_db()
    assert draft.plan_id == plan.pk
    assert plan.checklist["petition"]["label"] == "Petition"


@pytest.mark.django_db
def test_paused_checklists_do_not_create_plans(draft):
    assert ensure_plan_for_draft(draft) is None
    assert not FilingPlan.objects.filter(user=draft.user).exists()


def test_form_finder_keeps_resources_without_suggested_checklists(client):
    from efile.services.document_finder import search_intents

    intent = search_intents("illinois")["results"][0]
    response = client.get(
        reverse("find_documents_intent", kwargs={"jurisdiction": "illinois", "intent_id": intent["id"]})
    )
    assert response.status_code == 200
    assert b"finder-checklist-heading" not in response.content
    assert b"Ready to file?" in response.content
    assert b"Get the forms" in response.content
    assert b"Request for Name Change" in response.content
