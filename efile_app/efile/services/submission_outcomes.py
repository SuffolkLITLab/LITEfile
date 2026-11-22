"""Find filings whose submission outcome this app does not know (#445).

A submit claims its draft (DRAFT -> SUBMITTING) before calling the court and is
never retried automatically, because the filing API has no idempotency key and a
retry could file twice. A draft is left without a known outcome when:

* it stays SUBMITTING: the request was cut off (a machine stopped mid-call) after
  the filing may have been sent, so nothing recorded the court's answer; or
* it is ERROR: the court call failed in a way that may still have filed.

Staff check these against the court's records by hand. This module only reads.
"""

from datetime import timedelta

from django.db.models import Q, QuerySet
from django.utils import timezone

from efile.models import FilingDraft

# A live submit finishes well inside this: the court call times out after
# EFILE_SUBMISSION_READ_TIMEOUT_SECONDS (300 by default).
STUCK_SUBMITTING_AFTER = timedelta(minutes=15)


def unconfirmed_submissions(jurisdictions=None, *, now=None) -> QuerySet[FilingDraft]:
    """Drafts whose filing may or may not have reached the court, oldest first."""
    cutoff = (now or timezone.now()) - STUCK_SUBMITTING_AFTER
    drafts = FilingDraft.objects.filter(
        Q(status=FilingDraft.Status.ERROR) | Q(status=FilingDraft.Status.SUBMITTING, updated_at__lt=cutoff)
    )
    if jurisdictions is not None:
        drafts = drafts.filter(jurisdiction__in=list(jurisdictions))
    return drafts.order_by("updated_at", "pk")
