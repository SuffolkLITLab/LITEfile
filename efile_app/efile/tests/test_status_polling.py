import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from efile.models import FilingDraft
from efile.tests.test_document_extractions import authorize


@pytest.mark.django_db
def test_status_does_not_create_an_unrequested_draft(client, django_user_model):
    user = django_user_model.objects.create_user(username="poll-empty", tyler_jurisdiction="illinois")
    client.force_login(user)
    session = client.session
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "synthetic-token"}
    session.save()
    result = client.get(reverse("document_extraction_status", kwargs={"jurisdiction": "illinois"}))
    assert result.json() == {"success": True, "status": "not_queued", "ready": False}
    assert not FilingDraft.objects.filter(user=user).exists()


@pytest.mark.django_db
def test_status_does_not_mutate_the_filing(client, django_user_model):
    user = django_user_model.objects.create_user(username="poll-read", tyler_jurisdiction="illinois")
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", workflow_version=2)
    authorize(client, draft)
    before = draft.updated_at
    with CaptureQueriesContext(connection) as queries:
        response = client.get(reverse("document_extraction_status", kwargs={"jurisdiction": "illinois"}))
    assert response.status_code == 200
    assert not response.json()["ready"]
    draft.refresh_from_db()
    assert draft.updated_at == before
    assert not any('UPDATE "efile_filingdraft"' in q["sql"] for q in queries)
    # Django's session middleware still refreshes the normal rolling expiry.
    assert sum('UPDATE "django_session"' in q["sql"] for q in queries) == 1
