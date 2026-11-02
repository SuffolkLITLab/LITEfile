from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext
from django.views.decorators.http import require_http_methods

from efile.api.suffolk_api_views import get_tyler_token
from efile.models import FilingParty
from efile.party_sides import PARTY_SIDE_LABELS
from efile.services.current_drafts import ensure_current_draft
from efile.services.drafts import draft_snapshot
from efile.services.extracted_parties import party_display_name
from efile.services.people import (
    NOT_A_PARTY,
    absorb_filer_duplicates,
    apply_party_sides,
    case_has_named_parties,
    claim_party_as_filer,
    claim_replaces_a_name,
    discard_empty_parties,
    document_named_the_parties,
    ensure_required_parties,
    filer_name_match,
    filing_party_candidates,
    get_case_questions,
    get_party_types,
    guess_filer_party_type,
    incomplete_parties,
    missing_required_party_types,
    names_match,
    needs_amount_in_controversy,
    party_can_be_the_filer,
    party_is_complete,
    self_claimed_party,
    set_filing_parties,
)
from efile.workflow import (
    RETURN_TO_REVIEW,
    ExistingCase,
    WorkflowStepKey,
    continue_step,
    continue_url,
    get_workflow_context,
    return_target,
    with_return_to,
)


def _party_details_url(jurisdiction, party, return_to=None):
    url = f"{reverse('party_details', kwargs={'jurisdiction': jurisdiction})}?party={party.pk}"
    return with_return_to(url, return_to)


def _parties_url(jurisdiction, return_to=None):
    return with_return_to(reverse("parties", kwargs={"jurisdiction": jurisdiction}), return_to)


def _is_email(value):
    try:
        validate_email(value)
    except ValidationError:
        return False
    return True


def _chosen_filing_parties(request, draft):
    """The roster rows the filer ticked as the people they are filing for."""

    ids = [value for value in request.POST.getlist("filing_for") if str(value).isdigit()]
    if not ids:
        return []
    return list(FilingParty.objects.filter(draft=draft, role="other", pk__in=ids))


def _continue_from_parties(request, jurisdiction, draft, party_types, return_to):
    """Fill in the court's required parties, then move on or collect the gaps."""

    if draft.existing_case == ExistingCase.EXISTING:
        missing = [item["name"] for item in missing_required_party_types(draft, party_types)]
        if missing:
            messages.error(
                request,
                "The court requires these roles: "
                + ", ".join(missing)
                + ". Use Add a new party if someone needs to join this case, or contact support about the court roster.",
            )
            return redirect(_parties_url(jurisdiction, return_to))
    ensure_required_parties(draft, party_types)
    incomplete = incomplete_parties(draft, party_types=party_types)
    if incomplete:
        draft.current_step = WorkflowStepKey.PARTY_DETAILS
        draft.save(update_fields=["current_step", "updated_at"])
        return redirect(_party_details_url(jurisdiction, incomplete[0], return_to))

    has_questions = bool(get_case_questions(draft)) or needs_amount_in_controversy(draft)
    draft.supplemental_fields = {
        **(draft.supplemental_fields or {}),
        "_case_questions_required": has_questions,
    }
    default_step = WorkflowStepKey.CASE_QUESTIONS if has_questions else WorkflowStepKey.PAYMENT
    draft.current_step = continue_step(draft, return_to, default_step)
    draft.save(update_fields=["supplemental_fields", "current_step", "updated_at"])
    return redirect(continue_url(draft, jurisdiction, return_to, default_step))


def _continue_previews(draft, filer, party_types, return_to):
    """What Continue will do for each answer to the role question.

    Continue carries whichever role is selected when it is pressed, which need
    not be the saved one, so its label is worked out for every choice and the
    page shows the one matching the selected radio. The filer's own row counts
    as whatever role they pick; everyone else is as saved.
    """

    others = list(FilingParty.objects.filter(draft=draft, role="other"))
    others_incomplete = any(not party_is_complete(party, party_types=party_types) for party in others)
    held_by_others = {party.party_type for party in others if party.party_type}
    saved_type = filer.party_type

    def preview(role_code):
        filer.party_type = role_code
        try:
            filer_incomplete = not party_is_complete(filer, party_types=party_types)
        finally:
            filer.party_type = saved_type
        missing = [
            item["name"]
            for item in party_types
            if item["required"] and item["code"] not in held_by_others and item["code"] != role_code
        ]
        hints = [gettext("The court also needs a %(name)s in this case.") % {"name": name} for name in missing]
        if missing and draft.existing_case == ExistingCase.EXISTING:
            hints.append(gettext("Use Add a new party if someone needs to join this case, or contact support."))
            return {"label": gettext("Continue"), "hint": " ".join(hints)}
        if missing or others_incomplete or filer_incomplete:
            hints.append(gettext("Continue will ask for the details that are still missing."))
            label = gettext("Continue to missing party details")
        elif return_to == RETURN_TO_REVIEW:
            label = gettext("Continue to review")
        else:
            label = gettext("Continue")
        return {"label": label, "hint": " ".join(hints)}

    previews = {item["code"]: preview(item["code"]) for item in party_types}
    previews[NOT_A_PARTY] = preview("")
    # Nothing chosen yet: Continue will only ask for a role, so it promises
    # nothing about where it goes next.
    previews[""] = {"label": gettext("Continue"), "hint": ""}
    return previews


def _save_filer_role(request, draft, filer, party_types):
    """Record the filer's answer to the role question, or say what is missing.

    Shared by Save, which stays on this screen, and Continue, which moves on:
    the answer is the same answer whichever button carried it.
    """

    if draft.existing_case == ExistingCase.EXISTING:
        chosen = _chosen_filing_parties(request, draft)
        if not chosen or not all(p.on_case_roster for p in chosen):
            messages.error(request, "Choose the existing party you are filing for.")
            return False
        notice_email = request.POST.get("notice_email", "").strip() or filer.email
        if not _is_email(notice_email):
            messages.error(request, "Give an email address for notices about this case.")
            return False
        set_filing_parties(draft, chosen)
        draft.notice_email = notice_email
        draft.save(update_fields=["notice_email", "updated_at"])
        return True
    party_type_names = {item["code"]: item["name"] for item in party_types}
    filer_type = request.POST.get("filer_party_type", "").strip()
    if filer_type == NOT_A_PARTY:
        # Filing for someone else. Tyler still needs a party to file on
        # behalf of, so the filer names one instead of becoming one.
        chosen = _chosen_filing_parties(request, draft)
        notice_email = request.POST.get("notice_email", "").strip()
        if not chosen:
            messages.error(request, "Choose who you are filing for.")
            return False
        if not _is_email(notice_email):
            messages.error(request, "Give an email address for notices about this case.")
            return False
        filer.party_type = ""
        filer.party_type_name = ""
        filer.save(update_fields=["party_type", "party_type_name", "updated_at"])
        set_filing_parties(draft, chosen)
        draft.notice_email = notice_email
        draft.save(update_fields=["notice_email", "updated_at"])
        return True
    if filer_type in party_type_names:
        filer.party_type = filer_type
        filer.party_type_name = party_type_names[filer_type]
        filer.save(update_fields=["party_type", "party_type_name", "updated_at"])
        set_filing_parties(draft, [filer])
        if draft.notice_email:
            # A party in their own case is reached at their own address,
            # and the review screen should stop naming one that no longer
            # applies to anything.
            draft.notice_email = ""
            draft.save(update_fields=["notice_email", "updated_at"])
        return True
    messages.error(request, "Choose your role in this case, or tell us you are filing for someone else.")
    return False


@require_http_methods(["GET", "POST"])
def parties(request, jurisdiction):
    if not request.user.is_authenticated or not get_tyler_token(request, jurisdiction):
        return redirect("efile_login", jurisdiction=jurisdiction)

    draft = ensure_current_draft(
        request,
        jurisdiction,
        current_step=WorkflowStepKey.PARTIES,
        workflow_version=2,
    )
    filer = FilingParty.objects.filter(draft=draft, role="filer").first()
    if filer is None:
        return redirect("your_information", jurisdiction=jurisdiction)
    party_types = get_party_types(draft)
    # A person the filer started adding and never named is not a party they
    # meant to add, and reaches this list as an entry they cannot tell apart
    # from one they did. Only ever cleared on the way in: a POST is somebody
    # acting on a row, including the blank one they just made.
    if request.method == "GET":
        discard_empty_parties(draft)
    # The document said which side each person is on; the case type -- settled
    # by now -- says what this court calls that side. Folding the filer's own
    # duplicate in first keeps them from reaching the court twice.
    absorb_filer_duplicates(draft)
    apply_party_sides(draft, party_types)
    # The answer given two screens ago, now that there is a filer row to give
    # it to and a court party type to give them. Folded straight in when the
    # two rows agree on the name; when they do not, replacing one name with
    # another is a question, and it is put below rather than done quietly.
    marked_self = self_claimed_party(draft)
    if marked_self is not None and party_can_be_the_filer(marked_self) and names_match(filer, marked_self):
        claim_party_as_filer(draft, marked_self)
        filer.refresh_from_db()

    if request.method == "POST":
        action = request.POST.get("action", "continue")
        return_to = return_target(request)
        if action == "add":
            last_order = (
                FilingParty.objects.filter(draft=draft, role="other")
                .order_by("-sort_order")
                .values_list("sort_order", flat=True)
                .first()
            )
            joins_court_case = draft.existing_case == ExistingCase.EXISTING
            party = FilingParty.objects.create(
                draft=draft,
                role="other",
                source="added" if joins_court_case else "manual",
                source_case_id=draft.previous_case_id if joins_court_case else "",
                sort_order=0 if last_order is None else last_order + 1,
            )
            draft.current_step = WorkflowStepKey.PARTY_DETAILS
            draft.save(update_fields=["current_step", "updated_at"])
            return redirect(_party_details_url(jurisdiction, party, return_to))
        if action == "claim_party":
            # "That party is me." Said of a person the document named, or of
            # the blank row someone started before realising they were adding
            # themselves. Either way the filer is already on this draft with a
            # name and an address, so the other row goes rather than reaching
            # the court as a second person.
            party = get_object_or_404(FilingParty, pk=request.POST.get("party_id"), draft=draft, role="other")
            if not party_can_be_the_filer(party):
                messages.error(
                    request,
                    "An organization cannot be you. Say you are filing for them instead.",
                )
                return redirect(f"{_parties_url(jurisdiction, return_to)}#your-role")
            name_choice = request.POST.get("name_choice", "")
            if claim_replaces_a_name(filer, party) and name_choice not in {"mine", "theirs"}:
                # Replacing a differently-named party changes who the court is
                # told this case is about. Nobody should be able to do that by
                # clicking one button, so the screen asks first and this is
                # what happens when the answer did not arrive.
                messages.error(
                    request,
                    "Say which name the court should see before replacing a party with yourself.",
                )
                return redirect(_parties_url(jurisdiction, return_to))
            claim_party_as_filer(draft, party, use_party_name=name_choice == "theirs")
            filer.refresh_from_db()
            if party.on_case_roster:
                messages.success(
                    request, "You are linked to this party. Your account contact information stays separate."
                )
            elif filer.party_type:
                messages.success(
                    request,
                    f"You are listed in this case as the {filer.party_type_name or filer.party_type}.",
                )
            else:
                messages.success(request, "Choose your own role below to add yourself as a party.")
            return redirect(f"{_parties_url(jurisdiction, return_to)}#your-role")
        if action == "remove":
            party = get_object_or_404(FilingParty, pk=request.POST.get("party_id"), draft=draft, role="other")
            if party.source == "court":
                messages.error(request, "A party already on the court case cannot be removed.")
                return redirect(_parties_url(jurisdiction, return_to))
            party.delete()
            messages.success(request, "Party removed.")
            return redirect(_parties_url(jurisdiction, return_to))

        if action in {"save_role", "continue"} and _save_filer_role(request, draft, filer, party_types):
            if action == "continue":
                return _continue_from_parties(request, jurisdiction, draft, party_types, return_to)
            # Saving the role is not the end of this screen. The party list
            # below it is who the court will be told this case is about, and
            # the filer gets to read it -- with their own row now in it --
            # before anything moves them on.
            messages.success(request, "Your role is saved. Check the party list, then continue.")
            return redirect(f"{_parties_url(jurisdiction, return_to)}#party-list")

    roster_parties = list(FilingParty.objects.filter(draft=draft))
    has_linked_party = any(party.on_case_roster and party.is_self for party in roster_parties)
    if has_linked_party:
        # The linked court party represents the filer in this list. Keep the
        # separate account contact for correspondence, without listing it twice.
        roster_parties = [party for party in roster_parties if party.role != "filer"]
    roster = [
        {
            "party": party,
            "name": party_display_name(party),
            "complete": party_is_complete(party, party_types=party_types),
            # Whether claiming this row is confirming who you are or replacing
            # somebody with a different name. The two are not the same action
            # and the screen does not call them the same thing.
            "replaces_a_name": claim_replaces_a_name(filer, party),
            "claimable": party_can_be_the_filer(party) and not party.is_self,
        }
        for party in roster_parties
    ]
    saved_filing_for = {
        party.pk for party in FilingParty.objects.filter(draft=draft, role="other", is_filing_party=True)
    }
    # The document naming the filer is a better answer than the case posture,
    # and a more concrete question to put to them: it can say which party they
    # are rather than which side they are probably on. Only asked while the
    # role question is still unanswered -- a filer who has said they are
    # filing for someone else has answered it, and does not need telling again
    # every time they come back to this screen.
    marked_self = self_claimed_party(draft)
    named_in_document = (
        None
        if saved_filing_for or draft.existing_case == ExistingCase.EXISTING
        else (marked_self or filer_name_match(draft))
    )
    # Never alongside the people themselves: a filer who has said who is in
    # this case has answered a better version of this question already, and
    # being told what they are "likely" to be contradicts it.
    guessed_party_type = (
        None
        if filer.party_type or named_in_document is not None or case_has_named_parties(draft)
        else guess_filer_party_type(draft, party_types)
    )
    # Which branch of the role question the screen comes back on. A filer who
    # has never answered gets neither pre-selected -- their own role is not
    # something to guess at on their behalf -- but an answer that has just
    # been refused is still their answer, and stays on the screen with the
    # error rather than making them find it again.
    attempted = request.POST.get("filer_party_type", "").strip() if request.method == "POST" else ""
    attempted_filing_for = {int(value) for value in request.POST.getlist("filing_for") if str(value).isdigit()}
    filing_for = attempted_filing_for or saved_filing_for
    filing_for_someone_else = attempted == NOT_A_PARTY or (bool(saved_filing_for) and not filer.party_type)
    return_to = return_target(request)
    # What Continue will do next, said before it does it -- for the role that
    # is selected, which parties.js keeps in step as the selection changes.
    continue_previews = _continue_previews(draft, filer, party_types, return_to)
    selected_role = attempted or (NOT_A_PARTY if filing_for_someone_else else filer.party_type)
    context = {
        "existing_court_case": draft.existing_case == ExistingCase.EXISTING,
        "caption_discrepancy": bool(
            draft.existing_case_snapshot and draft.extracted_guesses and document_named_the_parties(draft)
        ),
        "is_logged_in": True,
        "filing_draft": draft_snapshot(draft),
        "filer": filer,
        "has_linked_party": has_linked_party,
        "return_to": return_to,
        "continue_previews": continue_previews,
        "continue_preview": continue_previews.get(selected_role, continue_previews[""]),
        "party_types": party_types,
        "roster": roster,
        "guessed_party_type": guessed_party_type,
        "not_a_party_value": NOT_A_PARTY,
        "filer_display_name": party_display_name(filer),
        "named_in_document": named_in_document,
        # Whether that came from the filer ticking "this is me" while reading
        # their document, which is worth saying back to them in those words.
        "named_in_document_was_claimed": marked_self is not None and named_in_document == marked_self,
        # Two different routes reach this screen with a party list: the
        # document named these people and the filer confirmed them, or the
        # filer typed them all in because they turned AI off and the keyword
        # scan never reads names. The screen must not credit the first when
        # it was the second.
        "case_has_parties": case_has_named_parties(draft),
        "parties_from_document": document_named_the_parties(draft),
        "named_in_document_name": party_display_name(named_in_document) if named_in_document else "",
        "named_in_document_role": (
            (named_in_document.party_type_name or PARTY_SIDE_LABELS.get(named_in_document.party_side, ""))
            if named_in_document
            else ""
        ),
        "filing_for_someone_else": filing_for_someone_else,
        "filing_for_candidates": [
            {"party": party, "selected": party.pk in filing_for} for party in filing_party_candidates(draft)
        ],
        # Offered filled in with the filer's own address, because that is the
        # right answer most of the time and a blank box is a question nobody
        # asked to be asked. It stays editable for the times it is not.
        "notice_email": (
            request.POST.get("notice_email", "").strip() if request.method == "POST" else draft.notice_email
        )
        or filer.email,
    }
    context.update(get_workflow_context(WorkflowStepKey.PARTIES, jurisdiction, draft))
    return render(request, "efile/parties.html", context)
