"""PostgreSQL concurrency checks for the extraction queue's actual claim function."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, connections, transaction

from efile.models import DocumentExtraction, FilingDraft
from efile.services.document_extractions import claim_next_extraction, queue_document_extraction
from efile.tests.helpers import reviewed_document

pytestmark = pytest.mark.django_db(transaction=True)


def job_for(user):
    draft = FilingDraft.objects.create(user=user, jurisdiction="illinois", workflow_version=2)
    document = reviewed_document(draft=draft, role="lead", name="synthetic.pdf", s3_key="synthetic/fixture.pdf")
    return draft, queue_document_extraction(document)


@pytest.mark.parametrize("lock_target", ["draft", "job"])
def test_locked_oldest_draft_does_not_idle_an_available_worker(django_user_model, lock_target):
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL row locks")
    user = django_user_model.objects.create_user(username="claim-fairness")
    first_draft, first = job_for(user)
    _, second = job_for(user)
    locked, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with transaction.atomic():
                if lock_target == "draft":
                    FilingDraft.objects.select_for_update().get(pk=first_draft.pk)
                else:
                    DocumentExtraction.objects.select_for_update().get(pk=first.pk)
                locked.set()
                assert release.wait(10)
        except Exception as error:
            errors.append(error)
        finally:
            connections.close_all()

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert locked.wait(5)
        started = time.perf_counter()
        claimed = claim_next_extraction()
        print(
            json.dumps(
                {
                    "claim_seconds": time.perf_counter() - started,
                    "available_job_claimed": claimed is not None and claimed.pk == second.pk,
                }
            )
        )
        assert claimed is not None and claimed.pk == second.pk
        first.refresh_from_db()
        assert first.status == "pending"
    finally:
        release.set()
        thread.join(timeout=10)
    assert not errors
    assert claim_next_extraction().pk == first.pk


def test_competing_workers_never_claim_the_same_job(django_user_model):
    if connection.vendor != "postgresql":
        pytest.skip("Requires real PostgreSQL row locks")
    user = django_user_model.objects.create_user(username="claim-competition")
    _, job = job_for(user)
    ready = threading.Barrier(2)

    def claim():
        try:
            ready.wait(timeout=5)
            claimed = claim_next_extraction()
            return claimed.pk if claimed else None
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: claim(), range(2)))
    assert results.count(job.pk) == 1
    job.refresh_from_db()
    assert job.attempts == 1
    assert job.claim_token is not None
    assert DocumentExtraction.objects.filter(status="processing").count() == 1


def test_provider_throttling_has_bounded_retries_and_does_not_stall_other_jobs(django_user_model, settings):
    import httpx
    from django.utils import timezone
    from openai import RateLimitError

    from efile.services.document_extractions import extraction_is_waiting, record_extraction_failure

    settings.DOCUMENT_EXTRACTION_MAX_ATTEMPTS = 3
    user = django_user_model.objects.create_user(username="claim-throttle")
    _, job = job_for(user)
    _, other = job_for(user)
    error = RateLimitError(
        "Synthetic throttle",
        response=httpx.Response(429, request=httpx.Request("POST", "https://synthetic.invalid")),
        body={},
    )
    for attempt in range(1, 4):
        claimed = claim_next_extraction()
        assert claimed.pk == job.pk
        assert claimed.attempts == attempt
        record_extraction_failure(job.pk, claimed.claim_token, error)
        job.refresh_from_db()
        assert job.status == ("failed" if attempt == 3 else "pending")
        assert job.available_at > timezone.now()
        assert "Synthetic throttle" not in job.error
        if attempt == 1:
            assert claim_next_extraction().pk == other.pk
            DocumentExtraction.objects.filter(pk=other.pk).update(status="complete")
        assert claim_next_extraction() is None
        DocumentExtraction.objects.filter(pk=job.pk).update(available_at=timezone.now())
    assert not extraction_is_waiting(job)
