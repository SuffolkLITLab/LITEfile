# models.py - Optional extension to store additional user information
import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models, transaction
from django.utils import timezone

from efile.db_expressions import CourtCode
from efile.party_sides import PARTY_SIDE_CHOICES
from efile.workflow import ExistingCase, WorkflowStepKey, get_workflow_step_choices


class FilingCodeIndex(models.Model):
    """One complete, last-known-good code snapshot per jurisdiction and EFSP."""

    jurisdiction = models.CharField(max_length=20, unique=True)
    source_url = models.URLField()
    refreshed_at = models.DateTimeField()
    rules_digest = models.CharField(max_length=64)
    vocabulary = models.JSONField(default=dict)
    court_snapshots = models.JSONField(default=dict)
    # Set once every court's labels are saved. Until then, search groups
    # and courts are read from the paths table.
    labels_built_at = models.DateTimeField(null=True, blank=True)


class FilingCodePath(models.Model):
    """A real court/category/case-type/filing-type path, never a cross product."""

    index = models.ForeignKey(FilingCodeIndex, on_delete=models.CASCADE, related_name="paths")
    initial = models.BooleanField()
    court = models.JSONField()
    case_category = models.JSONField()
    case_type = models.JSONField()
    filing_type = models.JSONField()
    search_text = models.TextField()
    filing_terms = models.TextField()
    case_terms = models.TextField()
    explanation = models.TextField(blank=True)
    explanation_source = models.URLField(blank=True)

    class Meta:
        indexes = [models.Index(models.F("index"), CourtCode("court"), name="filing_path_court")]


class FilingCodeLabel(models.Model):
    """One filing/category/case-type name and alias vocabulary, across courts sharing both.

    Illinois repeats each label for 13 to 21 courts on average, as paths.
    Searching labels first keeps the search off the much larger paths table
    until a court is chosen.
    """

    # The unique (index, key) constraint serves lookups by index.
    index = models.ForeignKey(FilingCodeIndex, on_delete=models.CASCADE, related_name="labels", db_index=False)
    initial = models.BooleanField()
    key = models.CharField(max_length=32)
    filing_type_name = models.TextField()
    case_category_name = models.TextField()
    case_type_name = models.TextField()
    # Names and scoped aliases. A path's search text also adds its court's words.
    search_text = models.TextField()
    filing_terms = models.TextField()
    case_terms = models.TextField()
    # Court code -> how many of its paths have this label. One map per label,
    # not a row per court: Illinois has nearly a row per path that way.
    courts = models.JSONField(default=dict)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["index", "key"], name="unique_filing_label_per_index")]


class FilingCodeCourtCatalog(models.Model):
    """One court's filing code tables, copied from the EFSP codes database.

    The search index is built from these copies, so rebuilding it (after a
    search-rules change, say) never touches the EFSP. ``revision`` is the
    EFSP's own: it changes when the court's name or code lists change.
    """

    jurisdiction = models.CharField(max_length=20)
    code = models.CharField(max_length=100)
    name = models.CharField(max_length=255)
    revision = models.CharField(max_length=64)
    data = models.JSONField()
    filing_count = models.PositiveIntegerField(default=0)
    copied_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["jurisdiction", "code"], name="unique_court_catalog_per_jurisdiction")
        ]


class FilingCodeJob(models.Model):
    """A staff request to resync codes or rebuild indexes, run by the code index worker."""

    class Kind(models.TextChoices):
        RESYNC = "resync", "Resync codes"
        REBUILD = "rebuild", "Rebuild code search indexes"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    kind = models.CharField(max_length=20, choices=Kind.choices)
    # Blank for every jurisdiction.
    jurisdiction = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    requested_by = models.ForeignKey(
        "efile.UserProfile", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    message = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at"]


class UserProfile(AbstractUser):
    """
    Extended user profile to store eFile registration information.
    """

    # Tyler identities are jurisdiction-specific; ``tyler_username`` may repeat
    # across jurisdictions while each row remains a separate local account.
    tyler_jurisdiction = models.CharField(max_length=20)
    tyler_user_id = models.CharField(max_length=100, blank=True, null=True)
    tyler_username = models.CharField(max_length=254, blank=True)

    # TODO(brycew): uncomment when https://github.com/SuffolkLITLab/EfileProxyServer/issues/334 is in
    # token_expires_at = models.DateTimeField(blank=True, null=True)

    # Communication Preferences
    email_updates = models.BooleanField(default=False)
    text_updates = models.BooleanField(default=False)

    # The filer's standing answer to the AI question (issue #104): when set,
    # every filing they start opts out by default. A single filing can still
    # differ -- FilingDraft.ai_assistance_opted_out is what the worker reads --
    # so this is the value a new draft is born with, not a lock on it.
    ai_assistance_opted_out = models.BooleanField(default=False)
    analytics_excluded = models.BooleanField(default=False)
    # The last few ZIP codes this filer used to find a filing code, newest
    # first. Filers who file for others often file far from their own address,
    # so code search offers these as shortcuts, never as a prefilled value.
    recent_case_zips = models.JSONField(default=list, blank=True)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User profile"
        verbose_name_plural = "User profiles"
        constraints = [
            models.UniqueConstraint(
                fields=["tyler_jurisdiction", "tyler_username"],
                condition=~models.Q(tyler_username=""),
                name="unique_tyler_account_per_jurisdiction",
            )
        ]

    @property
    def account_email(self):
        """The external Tyler login shown to the user, never the internal username."""

        return self.tyler_username or self.email or self.username


class FilingPlan(models.Model):
    """A filer's long-lived matter: the documents they are gathering for it.

    A plan outlives any one envelope. It stores what the filer's case *is* in
    semantic terms -- the court, case category, case type, and lead filing type
    by name -- and never the court's numeric codes for them. Those codes belong
    to a filing: they differ per court and change without notice, so a later
    filing resolves the stored names against the live code lists instead of
    trusting a code saved months ago.

    ``checklist`` is a snapshot of the configured guidance, taken when the plan
    is created, plus the filer's own progress. Snapshotting means a partner
    editing the YAML later does not silently rewrite a checklist someone is
    already working through.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="filing_plans",
    )
    title = models.CharField(max_length=255)
    jurisdiction = models.CharField(max_length=40, db_index=True)

    court_code = models.CharField(max_length=100, blank=True)
    court_name = models.CharField(max_length=255, blank=True)
    case_category_name = models.CharField(max_length=255, blank=True)
    case_type_name = models.CharField(max_length=255, blank=True)
    lead_filing_type_name = models.CharField(max_length=255, blank=True)

    # Which side of the case the filer is on, as one of the role IDs the
    # partner configured for this case type ("landlord", "tenant"). It decides
    # which documents the checklist lists and how they are worded, so it is
    # part of what the matter *is*, not of any one envelope.
    filer_role = models.CharField(max_length=60, blank=True)

    # The court case this matter has become, once one exists: Tyler's case
    # tracking ID plus the docket number and title a person recognizes. Unlike
    # the code fields above, a tracking ID is a permanent identifier for one
    # case rather than a lookup key into a list the court renumbers, so it is
    # safe to keep. A plan that has one can file into that case directly.
    case_tracking_id = models.CharField(max_length=255, blank=True)
    docket_number = models.CharField(max_length=255, blank=True)
    case_title = models.CharField(max_length=500, blank=True)

    # {item_id: {"label": str, "requirement": "always|usually|sometimes",
    #            "description": str (optional), "status": "|have|filed|later",
    #            "due_date": "YYYY-MM-DD" (optional)}}
    checklist = models.JSONField(default=dict, blank=True)

    # What this kind of filing is about, in the partner's words, snapshotted the
    # same way and for the same reason as the checklist:
    # {"summary": str, "learn_more_url": str, "learn_more_label": str}
    guidance = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "jurisdiction"], name="plan_user_jurisdiction_idx"),
        ]

    def __str__(self):
        return self.title or f"Filing plan #{self.pk}"

    @property
    def is_linked_to_a_case(self) -> bool:
        return bool(self.case_tracking_id and self.docket_number)


class ArchivedCase(models.Model):
    """A court case the filer has told us to keep out of the way.

    "My cases" is built from the court's own filing history, which we do not
    own and cannot write to: an attorney with three hundred filings sees all
    three hundred, forever. Archiving is therefore ours to remember -- one row
    per case a filer has tidied away, and the case itself is untouched. Nothing
    is deleted or hidden from the court; the list simply stops leading with it,
    and the filer can still ask to see everything.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="archived_cases",
    )
    jurisdiction = models.CharField(max_length=40, db_index=True)
    # Tyler's permanent identifier for the case. Docket numbers are the human
    # name for it and can be reissued or corrected, so they are display only.
    case_tracking_id = models.CharField(max_length=255)
    docket_number = models.CharField(max_length=255, blank=True)
    case_title = models.CharField(max_length=500, blank=True)

    archived_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-archived_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "jurisdiction", "case_tracking_id"],
                name="unique_archived_case_per_user",
            )
        ]

    def __str__(self):
        return self.docket_number or self.case_title or f"Archived case #{self.pk}"


class FilingDraft(models.Model):
    """Durable aggregate for a single in-progress or submitted court filing."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        SUBMITTING = "submitting", "Submitting"
        SUBMITTED = "submitted", "Submitted"
        ERROR = "error", "Error"
        ABANDONED = "abandoned", "Abandoned"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="filing_drafts",
    )
    # The matter this filing belongs to, when the filer has one. A plan can
    # gather several filings over time; losing the plan must not lose the filing.
    plan = models.ForeignKey(
        "FilingPlan",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="filing_drafts",
    )
    correction_of_id: int | None
    correction_of = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.RESTRICT, related_name="correction"
    )
    submission_snapshot = models.JSONField(default=dict, blank=True)
    clerk_return = models.JSONField(default=dict, blank=True)
    correction_fields = models.JSONField(default=list, blank=True)

    jurisdiction = models.CharField(max_length=40, db_index=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True)
    current_step = models.CharField(
        max_length=64,
        choices=get_workflow_step_choices(),
        default=WorkflowStepKey.OPTIONS,
    )
    workflow_version = models.PositiveSmallIntegerField(default=2)

    existing_case = models.CharField(
        max_length=20,
        choices=[(value.value, value.name.title()) for value in ExistingCase],
        blank=True,
    )
    court_code = models.CharField(max_length=100, blank=True)
    court_name = models.CharField(max_length=255, blank=True)
    case_category_code = models.CharField(max_length=100, blank=True)
    case_category_name = models.CharField(max_length=255, blank=True)
    case_type_code = models.CharField(max_length=100, blank=True)
    case_type_name = models.CharField(max_length=255, blank=True)
    case_subtype_code = models.CharField(max_length=100, blank=True)
    case_subtype_name = models.CharField(max_length=255, blank=True)
    filing_type_code = models.CharField(max_length=100, blank=True)
    filing_type_name = models.CharField(max_length=255, blank=True)
    document_type_code = models.CharField(max_length=100, blank=True)
    document_type_name = models.CharField(max_length=255, blank=True)

    existing_case_snapshot = models.JSONField(default=dict, blank=True)
    previous_case_id = models.CharField(max_length=255, blank=True)
    docket_number = models.CharField(max_length=255, blank=True)
    case_title = models.CharField(max_length=500, blank=True)

    # Where notices about this case should go. Only asked of someone filing
    # for a party they are not, because only then are the two addresses
    # different questions: the account signed in belongs to the person doing
    # the filing, and the notices belong to whoever is meant to read them --
    # them, the party, or a relative handling the mail. Blank means the
    # filer's own account address, which is what it is offered filled in with.
    notice_email = models.EmailField(blank=True)

    selected_payment_account_id = models.CharField(max_length=255, blank=True)
    selected_payment_account_name = models.CharField(max_length=255, blank=True)
    # Tyler's paymentAccountTypeCode for the selected account (e.g. "WV" for a fee
    # waiver). Drives whether Review shows a fee total or waiver messaging.
    selected_payment_account_type = models.CharField(max_length=50, blank=True)
    # The fee quote shown on the Payment step, carried forward so Review can
    # display the same numbers instead of telling the filer to go look again.
    quoted_fee_total = models.CharField(max_length=50, blank=True)
    quoted_fee_breakdown = models.JSONField(default=list, blank=True)
    # What the quote was priced on (see efile.services.fee_quotes). A quote is
    # only current while the draft still produces the same fingerprint.
    quoted_fee_fingerprint = models.CharField(max_length=64, blank=True)

    name_change_reason = models.TextField(blank=True)

    optional_services = models.JSONField(default=list, blank=True)
    extracted_guesses = models.JSONField(default=dict, blank=True)
    extraction_review_fingerprint = models.CharField(max_length=64, blank=True)
    filing_type_proposal = models.JSONField(default=dict, blank=True)
    # Whether the filer asked us not to send this filing's documents to an AI
    # model. Held per draft rather than per account: it is a choice about these
    # documents, and a filer may answer it differently for the next filing.
    # When it is set, the lead document is still read locally for printed form
    # identifiers and case numbers (see services.document_extractions).
    ai_assistance_opted_out = models.BooleanField(default=False)
    document_checklist_acknowledged = models.BooleanField(default=False)
    # The side of the case this filer is on, when the case type distinguishes
    # them (see FilingPlan.filer_role). Held here as well as on the plan so the
    # question can be answered before a plan exists.
    filer_role = models.CharField(max_length=60, blank=True)
    # The dollar amount at stake, required by the EFSP when any document's
    # filing type is flagged "amountincontroversy: Required". Stored as text
    # (like the fee fields) since it's echoed back to the API rather than
    # computed on.
    amount_in_controversy = models.CharField(max_length=50, blank=True)
    # Area-of-law questionnaire answers (e.g. divorce children questions). These are
    # driven by the per-state/case-type config, not a fixed schema, so they live in a
    # structured JSON field rather than a column each. Only config-defined keys are
    # stored here -- it is not a catch-all for arbitrary case data.
    supplemental_fields = models.JSONField(default=dict, blank=True)
    # The court requirements the filer accepted at submit, with who and when
    # (see efile.services.disclaimers). Written with the submission claim.
    disclaimer_acceptance = models.JSONField(default=dict, blank=True)
    submission_response = models.JSONField(default=dict, blank=True)

    submitted_at = models.DateTimeField(blank=True, null=True)
    # The submission's address in notification links. Unguessable, unlike the
    # primary key, so a link reveals nothing about which submissions exist.
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    submission_operation = models.UUIDField(null=True, blank=True, editable=False)
    deletion_pending = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "status"], name="draft_user_status_idx"),
            models.Index(fields=["jurisdiction", "status"], name="draft_jurisdiction_status_idx"),
            models.Index(fields=["status", "updated_at"], name="draft_status_updated_idx"),
        ]

    def __str__(self):
        return f"{self.get_status_display()} filing draft #{self.pk} ({self.jurisdiction})"

    @property
    def is_filing_into_found_case(self):
        """Whether the case number and title are the court's, for a case it found.

        Only then are they shown. A new case has neither until the court opens
        it, and an existing case's number is only a search term until the case
        lookup has matched it to a court record.
        """
        return self.existing_case == ExistingCase.EXISTING and bool(self.previous_case_id)

    def mark_submitted(self, response_data):
        sync_primary_filing_type(self)
        from efile.services.handoff import full_snapshot

        self.submission_snapshot = full_snapshot(self)
        self.status = self.Status.SUBMITTED
        self.current_step = WorkflowStepKey.CONFIRMATION
        self.submission_response = response_data or {}
        self.submitted_at = timezone.now()
        self.save(
            update_fields=[
                "status",
                "current_step",
                "submission_response",
                "submission_snapshot",
                "submitted_at",
                "updated_at",
            ]
        )
        from efile.services.analytics import record_event, record_matter, record_transmission_details

        record_event(self, "transmitted")
        record_transmission_details(self)
        record_matter(self)

    def mark_error(self, response_data):
        self.status = self.Status.ERROR
        self.submission_response = response_data or {}
        self.save(update_fields=["status", "submission_response", "updated_at"])

    @transaction.atomic
    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if self.pk and (update_fields is None or {"filing_type_code", "filing_type_name"}.intersection(update_fields)):
            lead = self.documents.filter(role=FilingDocument.Role.LEAD).order_by("sort_order", "pk").first()
            if lead is not None:
                self.filing_type_code = lead.filing_type_code
                self.filing_type_name = lead.filing_type_name
        adding = self._state.adding
        super().save(*args, **kwargs)
        # A deferred response was not written, so the index needs no resync.
        written = "submission_response" in self.__dict__ and (
            update_fields is None or "submission_response" in update_fields
        )
        if written:
            # Keep searchable identifiers atomic with the response. QuerySet.update
            # and bulk writes must not be used to change submission_response.
            from efile.services.handoff import submitted_filing_ids

            filing_ids = submitted_filing_ids(self.submission_response)
            # Compare with the stored rows, not a load-time snapshot: another
            # request may have reindexed this draft since it was loaded.
            indexed = set() if adding else set(self.filing_identifiers.values_list("filing_id", flat=True))
            if filing_ids != indexed:
                self.filing_identifiers.exclude(filing_id__in=filing_ids).delete()
                # Another save of this draft may have indexed the same ID first.
                FilingIdentifier.objects.bulk_create(
                    [FilingIdentifier(draft=self, filing_id=value) for value in filing_ids], ignore_conflicts=True
                )


class FilingIdentifier(models.Model):
    """Search keys for a saved proxy response, not another filing status record."""

    draft = models.ForeignKey(FilingDraft, on_delete=models.CASCADE, related_name="filing_identifiers")
    filing_id = models.CharField(max_length=255, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["draft", "filing_id"], name="unique_draft_filing_id")]


class FilingDocument(models.Model):
    """Uploaded document; the lead's filing type is authoritative for the draft."""

    class Role(models.TextChoices):
        LEAD = "lead", "Lead document"
        SUPPORTING = "supporting", "Supporting document"

    draft = models.ForeignKey(FilingDraft, on_delete=models.CASCADE, related_name="documents")
    role = models.CharField(max_length=20, choices=Role.choices)
    sort_order = models.PositiveIntegerField(default=0)

    name = models.CharField(max_length=255, blank=True)
    original_filename = models.CharField(max_length=255, blank=True)
    size = models.PositiveBigIntegerField(blank=True, null=True)
    content_type = models.CharField(max_length=255, blank=True)
    s3_key = models.CharField(max_length=1024, blank=True)
    public_url = models.URLField(max_length=2048, blank=True)
    # Originals are private recovery copies; only s3_key is sent to the court.
    original_s3_key = models.CharField(max_length=1024, blank=True)
    upload_has_form_fields = models.BooleanField(null=True, blank=True)
    preparation = models.CharField(
        max_length=30,
        blank=True,
        choices=[
            ("unchanged", "Original PDF"),
            ("converted", "Converted from Word"),
            ("flattened", "Form fields locked"),
            ("converted_flattened", "Converted and fields locked"),
        ],
    )
    preparation_reviewed_at = models.DateTimeField(null=True, blank=True)

    # A blank document type is a completed answer only when Organize checked
    # that the court offers no confidentiality choices for this filing type.
    document_type_confirmed = models.BooleanField(default=False)

    filing_type_code = models.CharField(max_length=100, blank=True)
    filing_type_name = models.CharField(max_length=255, blank=True)
    document_type_code = models.CharField(max_length=100, blank=True)
    document_type_name = models.CharField(max_length=255, blank=True)
    filing_component_code = models.CharField(max_length=100, blank=True)
    filing_component_name = models.CharField(max_length=255, blank=True)
    # The court's own "amountincontroversy" flag for this document's filing
    # type (from the filing-types codes API) is "Required" for some case
    # types. Recorded per document, since each can carry a different filing
    # type; case_questions asks for the dollar amount if any document needs it.
    filing_requires_amount_in_controversy = models.BooleanField(default=False)

    # The plan checklist item this document answers, when the filer said which
    # one it is. It is how "I have my fee waiver" becomes "my fee waiver is in
    # this envelope", so the checklist can stop asking and the review step can
    # warn about anything the filer has but has not attached.
    checklist_item_id = models.CharField(max_length=100, blank=True)

    courtesy_copy_email = models.EmailField(blank=True)
    # Codes selected from the court's optional-services list for this document
    # (e.g. a certified copy), scoped per document since each can have its own
    # filing type. See efile.api.dropdown_views.get_optional_services.
    requested_optional_services = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["role", "sort_order", "created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["draft", "role", "sort_order"], name="unique_document_order_per_draft_role"
            ),
        ]

    def __str__(self):
        return self.name or f"{self.get_role_display()} for draft #{self.draft_id}"

    @transaction.atomic
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        sync_primary_filing_type(self.draft)


def sync_primary_filing_type(draft):
    """Persist the lead's filing type as the draft's primary filing summary.

    Query after saving: organization can promote or demote documents in either
    order. Also update the caller's draft instance so a later save cannot undo it.
    """
    lead = draft.documents.filter(role=FilingDocument.Role.LEAD).order_by("sort_order", "pk").first()
    values = {
        "filing_type_code": lead.filing_type_code if lead else "",
        "filing_type_name": lead.filing_type_name if lead else "",
    }
    FilingDraft.objects.filter(pk=draft.pk).update(**values)
    for field, value in values.items():
        setattr(draft, field, value)


class DocumentPreparationJob(models.Model):
    """Originals accepted durably before bounded preparation begins."""

    class Status(models.TextChoices):
        UPLOADING = "uploading", "Receiving files"
        PENDING = "pending", "Waiting"
        PROCESSING = "processing", "Preparing"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Needs attention"
        CANCELLED = "cancelled", "Cancelled"

    draft = models.ForeignKey(FilingDraft, on_delete=models.CASCADE, related_name="preparation_jobs")
    request_token = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    document_fingerprint = models.CharField(max_length=64)
    originals = models.JSONField(default=list)
    originals_ready = models.BooleanField(default=True)
    staged_keys = models.JSONField(default=list)
    fingerprint_version = models.PositiveSmallIntegerField(default=1)
    replacement_id = models.PositiveBigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    claim_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    available_at = models.DateTimeField(default=timezone.now)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["draft", "request_token"], name="preparation_request_once"),
            models.UniqueConstraint(
                fields=["draft"],
                condition=models.Q(status__in=["uploading", "pending", "processing", "failed"]),
                name="one_unfinished_preparation",
            ),
        ]
        indexes = [models.Index(fields=["status", "available_at"], name="preparation_available")]


class DocumentExtraction(models.Model):
    """Durable background work for analyzing one uploaded lead PDF."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting"
        PROCESSING = "processing", "Analyzing"
        COMPLETE = "complete", "Complete"
        FAILED = "failed", "Could not analyze"

    document = models.OneToOneField(
        FilingDocument,
        on_delete=models.CASCADE,
        related_name="extraction",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING, db_index=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    claim_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    available_at = models.DateTimeField(default=timezone.now)
    total_pages = models.PositiveIntegerField(blank=True, null=True)
    pages_analyzed = models.PositiveIntegerField(blank=True, null=True)
    # Structured direct evidence is separate from the flattened review copy so
    # amounts, excerpts, and selected form options keep their original shape.
    evidence = models.JSONField(default=dict, blank=True)
    # Exact current taxonomy selections. `route_key` values are transient and
    # always paired with the durable Tyler name and source endpoint metadata.
    classification = models.JSONField(default=dict, blank=True)
    analysis_metadata = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.get_status_display()} extraction for document #{self.document_id}"


class FilingParty(models.Model):
    """Person or organization associated with a filing draft."""

    # Rows on an existing court case's roster: "court" rows came from the
    # court and cannot be edited; "added" rows join the case in this filing.
    CASE_ROSTER_SOURCES = ("court", "added")

    draft = models.ForeignKey(FilingDraft, on_delete=models.CASCADE, related_name="parties")
    role = models.CharField(max_length=50)
    sort_order = models.PositiveIntegerField(default=0)

    party_type = models.CharField(max_length=100, blank=True)
    party_type_name = models.CharField(max_length=255, blank=True)
    external_party_id = models.CharField(max_length=255, blank=True)
    source = models.CharField(max_length=20, default="manual")
    source_case_id = models.CharField(max_length=255, blank=True)
    representation = models.JSONField(default=dict, blank=True)

    # Set on the review screen, where the people read off the document are
    # first shown: "this one is me". It is recorded on the party rather than
    # on the filer because the filer has no row of their own yet -- theirs is
    # made two screens later, on your-information -- and the parties screen
    # is where the two are finally put together.
    is_self = models.BooleanField(default=False)

    # Whether the filing is made *on behalf of* this party -- what Tyler calls
    # a filing party, and a different question from who is in the case. The
    # person signed in is usually a party themselves, and then their own row
    # carries this. But someone can file for a party they are not: a parent
    # for a child, a friend helping a neighbour answer an eviction. Their row
    # then has no ``party_type`` at all, and the party they are filing for
    # carries this instead. See ``efile.services.people.filing_parties``.
    is_filing_party = models.BooleanField(default=False)

    # Which side of the caption this person is on, in the only vocabulary a
    # document itself establishes: whoever started the case, whoever is
    # answering it, or someone else it names. Unlike ``party_type`` -- a code
    # out of one court's list for one case type -- a side is known as soon as
    # the document has been read, which is what lets the names be confirmed
    # before the court and case type are. See efile.services.extracted_parties.
    party_side = models.CharField(max_length=20, choices=PARTY_SIDE_CHOICES, blank=True)
    # The role the document gave this person in its own words ("Guardian ad
    # Litem"). Kept because it is often the only thing that can pick the right
    # party type out of a court list once one is available.
    party_role_hint = models.CharField(max_length=255, blank=True)

    first_name = models.CharField(max_length=100, blank=True)
    middle_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    suffix = models.CharField(max_length=50, blank=True)
    organization_name = models.CharField(max_length=255, blank=True)

    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)

    address_line_1 = models.CharField(max_length=255, blank=True)
    address_line_2 = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=50, blank=True)
    zip_code = models.CharField(max_length=20, blank=True)
    country = models.CharField(max_length=2, default="US", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["role", "sort_order", "created_at"]
        constraints = [
            models.UniqueConstraint(fields=["draft", "role", "sort_order"], name="unique_party_order_per_draft_role"),
            models.UniqueConstraint(
                fields=["draft", "source_case_id", "external_party_id"],
                condition=models.Q(source="court") & ~models.Q(external_party_id=""),
                name="unique_imported_case_party",
            ),
        ]
        verbose_name_plural = "Filing parties"

    @property
    def on_case_roster(self) -> bool:
        return self.source in self.CASE_ROSTER_SOURCES

    def __str__(self):
        display_name = " ".join(part for part in [self.first_name, self.middle_name, self.last_name] if part)
        return display_name or self.organization_name or f"{self.role} for draft #{self.draft_id}"

    @transaction.atomic
    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)


class InterviewHandoff(models.Model):
    """Authenticated source receipt, retained independently of browser sessions."""

    draft = models.OneToOneField(FilingDraft, on_delete=models.CASCADE, related_name="handoff")
    source = models.CharField(max_length=100)
    source_id = models.CharField(max_length=255)
    idempotency_key = models.CharField(max_length=255)
    fingerprint = models.CharField(max_length=64)
    payload = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "source_id"], name="handoff_source_identity"),
            models.UniqueConstraint(fields=["source", "idempotency_key"], name="handoff_idempotency"),
        ]


class FilingMetadataEvent(models.Model):
    """Append-only provenance of suggestions, resolutions, and corrections."""

    draft = models.ForeignKey(FilingDraft, on_delete=models.CASCADE, related_name="metadata_events")
    path = models.CharField(max_length=255)
    kind = models.CharField(max_length=40)
    value = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["pk"]


class HandoffDocumentUpdate(models.Model):
    """A replacement receipt makes retries safe without overwriting user edits."""

    draft = models.ForeignKey(FilingDraft, on_delete=models.CASCADE)
    source = models.CharField(max_length=100)
    idempotency_key = models.CharField(max_length=255)
    fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["source", "idempotency_key"], name="handoff_document_update_identity")
        ]


class PendingActivation(models.Model):
    """An account this app registered that the court's system has not activated.

    The e-filing service answers every failed sign-in the same way -- a bare
    403, whether the password is wrong, the account does not exist, or the
    filer has not yet clicked the activation link in their email. Only this app
    knows it registered the account moments ago, so it remembers, and a failed
    sign-in for that email can say what to do instead of "try again". Cleared
    by the first successful sign-in.
    """

    email = models.EmailField()
    jurisdiction = models.CharField(max_length=50)
    registered_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["email", "jurisdiction"], name="pending_activation_identity"),
        ]

    @classmethod
    def remember(cls, email, jurisdiction):
        cls.objects.update_or_create(email=email.strip().lower(), jurisdiction=jurisdiction)

    @classmethod
    def exists_for(cls, email, jurisdiction):
        return cls.objects.filter(email=email.strip().lower(), jurisdiction=jurisdiction).exists()

    @classmethod
    def forget(cls, email, jurisdiction):
        cls.objects.filter(email=email.strip().lower(), jurisdiction=jurisdiction).delete()


from efile.staff_models import (  # noqa: E402,F401
    PrivacyRequest,
    StaffAudit,
    StaffLoginThrottle,
    StaffRoleGrant,
    StoredUpload,
    UsageContributor,
    UsageCounter,
    UsageEvent,
    UsageMatter,
)
