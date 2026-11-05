"""Turn the names printed on a document into the parties a filing needs.

Extraction reads a lead document for who is on it: plaintiffs or petitioners,
defendants or respondents, and anyone else the caption names alongside their
stated role. Those answers are *sides* (see ``efile.party_sides``), which is
exactly what makes them usable straight away -- the filer can see and correct
every name on the same screen the document was read on, long before the court
and case type that decide the real party-type codes have been confirmed.

Nothing here guesses a court party type. Mapping a side onto one court's code
is ``efile.services.people.apply_party_sides``, and it runs later, once there
is a case type to fetch the list for.
"""

from __future__ import annotations

import re
from typing import Any

from efile.models import FilingDraft, FilingParty
from efile.party_sides import PartySide, side_for_party_type_name
from efile.services.extraction_confirmation import extraction_is_confirmed

# The extraction fields that name people, and the side each one establishes.
SIDE_BY_GUESS_KEY: dict[str, str] = {
    "plaintiff or petitioner names": PartySide.INITIATING,
    "defendant or respondent names": PartySide.RESPONDING,
    "other party names": PartySide.OTHER,
}

# Names arrive joined the way the extraction prompt asks for them (semicolons),
# but documents and models both fall back to newlines, "and", and ampersands.
_NAME_SEPARATORS = re.compile(r"\s*(?:;|\n|\r|\s+&\s+|\s+\band\b\s+)\s*")

# A stated role trailing a name: "Jane Doe (Guardian ad Litem)" or
# "Jane Doe - Guardian ad Litem". A comma is deliberately not a separator here:
# "Smith, John" is a name, not a name and a role.
_TRAILING_ROLE = re.compile(r"^(?P<name>.*?)\s*(?:\((?P<paren>[^)]+)\)|[-–—]\s*(?P<dash>[^-–—]+))\s*$")

# Words that mean the name belongs to an organization rather than a person, so
# it goes in one field instead of being chopped into first and last.
_ORGANIZATION_MARKERS = frozenset(
    {
        "inc",
        "llc",
        "llp",
        "lp",
        "ltd",
        "co",
        "corp",
        "corporation",
        "company",
        "trust",
        "bank",
        "association",
        "authority",
        "department",
        "dept",
        "university",
        "hospital",
        "apartments",
        "properties",
        "management",
        "realty",
        "partners",
        "group",
        "services",
        "holdings",
        "foundation",
        "church",
        "estate",
    }
)
_ORGANIZATION_PHRASES = ("city of", "county of", "town of", "state of", "commonwealth of", "board of", "office of")

# Kept on the end of the last name rather than dropped: the suffix field takes
# one of the court's own codes ("JR"), so a suffix read off a document has
# nowhere valid to go, and losing it silently would be worse than a name the
# filer can correct on the party screen.
_NAME_SUFFIXES = frozenset({"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v", "esq", "esq."})

# Answers that are not names. A model told to list the parties will sometimes
# say that it could not find any, in the field where the names were supposed
# to go, and the filer then has to work out that "Unknown" is not a person
# they need to keep.
#
# Matched against the whole name and never against part of one, because the
# words themselves are ordinary in real party names: an eviction complaint
# genuinely names "All Unknown Occupants", and a case genuinely has a party
# called "None Smith" more often than this list should get to decide.
_NOT_A_NAME = frozenset(
    {
        "unknown",
        "unknown party",
        "unknown parties",
        "unknown name",
        "name unknown",
        "no name",
        "none",
        "none listed",
        "none given",
        "none stated",
        "n a",
        "na",
        "not applicable",
        "not available",
        "not given",
        "not listed",
        "not named",
        "not provided",
        "not specified",
        "not stated",
        "no parties listed",
        "no other parties",
        "tbd",
        "to be determined",
        "et al",
        "same as above",
        "see above",
        "see attached",
        "see caption",
        "null",
        "blank",
        "empty",
        "unspecified",
        "unnamed",
        "party",
        "parties",
        "plaintiff",
        "plaintiffs",
        "petitioner",
        "petitioners",
        "defendant",
        "defendants",
        "respondent",
        "respondents",
    }
)


def looks_like_organization(name: str) -> bool:
    lowered = str(name or "").lower()
    if any(phrase in lowered for phrase in _ORGANIZATION_PHRASES):
        return True
    return any(word.strip(".,") in _ORGANIZATION_MARKERS for word in lowered.split())


def split_person_name(name: str) -> dict[str, str]:
    """Split a printed personal name into the fields a filing party has."""

    tokens = [token for token in str(name or "").split() if token]
    if not tokens:
        return {"first_name": "", "middle_name": "", "last_name": ""}
    if len(tokens) == 1:
        return {"first_name": "", "middle_name": "", "last_name": tokens[0]}
    if len(tokens) >= 3 and tokens[-1].lower() in _NAME_SUFFIXES:
        return {
            "first_name": tokens[0],
            "middle_name": " ".join(tokens[1:-2]),
            "last_name": " ".join(tokens[-2:]),
        }
    return {"first_name": tokens[0], "middle_name": " ".join(tokens[1:-1]), "last_name": tokens[-1]}


def is_placeholder_name(name: str) -> bool:
    """True for an answer that says there is no name, rather than giving one.

    Whole-name comparison only. "All Unknown Occupants" is a real defendant in
    a real eviction, and a guard that reached inside names would delete them.
    """

    return _comparable_name(name) in _NOT_A_NAME


def split_extracted_names(value: Any) -> list[dict[str, str]]:
    """Split one extraction field into ``{"name", "role_hint"}`` entries."""

    if isinstance(value, list | tuple | set):
        parts = [str(item) for item in value]
    else:
        parts = _NAME_SEPARATORS.split(str(value or ""))

    entries: list[dict[str, str]] = []
    for part in parts:
        text = part.strip().strip(",").strip()
        if not text:
            continue
        role_hint = ""
        match = _TRAILING_ROLE.match(text)
        if match and match.group("name").strip():
            role_hint = (match.group("paren") or match.group("dash") or "").strip()
            text = match.group("name").strip()
        if is_placeholder_name(text):
            continue
        entries.append({"name": text, "role_hint": role_hint})
    return entries


def extracted_party_suggestions(guesses: dict[str, Any] | None) -> list[dict[str, str]]:
    """Every person the document named, in caption order, with their side.

    A name is listed once however many times the document printed it. The
    same person read onto two sides is a misreading rather than two people,
    so the first side -- caption order, which runs from the initiating side
    down -- is the one that survives.
    """

    suggestions: list[dict[str, str]] = []
    seen: set[str] = set()
    for key, side in SIDE_BY_GUESS_KEY.items():
        for entry in split_extracted_names((guesses or {}).get(key)):
            fingerprint = _comparable_name(entry["name"])
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            suggestions.append({**entry, "side": str(side)})
    return suggestions


def _comparable_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


def party_display_name(party: FilingParty) -> str:
    if party.organization_name:
        return party.organization_name
    return " ".join(part for part in (party.first_name, party.middle_name, party.last_name, party.suffix) if part)


def apply_name(party: FilingParty, name: str) -> None:
    """Write one printed name onto a party, as a person or an organization."""

    if looks_like_organization(name):
        party.organization_name = name.strip()
        party.first_name = party.middle_name = party.last_name = ""
        return
    party.organization_name = ""
    for field, value in split_person_name(name).items():
        setattr(party, field, value)


def review_rows(draft: FilingDraft) -> list[dict[str, Any]]:
    """The party rows the extraction-review screen shows and edits.

    Parties already saved for this draft are what is shown, so a filer coming
    back to the screen sees their own corrections rather than the document's
    first guess. Only a draft with no parties of its own yet falls back to the
    document, and those rows carry no id: they become real parties when the
    filer submits the screen, not when they merely look at it.
    """

    saved = list(FilingParty.objects.filter(draft=draft, role="other"))
    if saved:
        return [
            {
                "id": party.pk,
                "name": party_display_name(party),
                "side": party.party_side or side_for_party_type_name(party.party_type_name),
                "role_hint": party.party_role_hint,
                "party_type_name": party.party_type_name,
                "is_self": party.is_self,
                "is_organization": bool(party.organization_name),
            }
            for party in saved
        ]
    if extraction_is_confirmed(draft) or draft.supplemental_fields.get("_suggestion_source_changed"):
        return []
    return [
        {
            "id": "",
            "name": entry["name"],
            "side": entry["side"],
            "role_hint": entry["role_hint"],
            "party_type_name": "",
            "is_self": False,
            "is_organization": looks_like_organization(entry["name"]),
        }
        for entry in extracted_party_suggestions(draft.extracted_guesses)
        # Mentioning a child, witness, guardian, or other person does not
        # establish that they are a case party. Keep these names in the
        # supporting evidence; the filer can explicitly add a party if needed.
        if entry["side"] in {PartySide.INITIATING, PartySide.RESPONDING}
    ]


def save_reviewed_parties(draft: FilingDraft, rows: list[dict[str, str]]) -> None:
    """Persist the party rows submitted from the extraction-review screen.

    The screen owns this draft's list of other parties, so a row the filer
    deleted is deleted here too. Addresses and contact details are never
    touched: they are collected later, and a filer who corrects a spelling on
    this screen must not lose the address they typed on the party screen.
    """

    if (
        draft.existing_case_snapshot.get("confirmed")
        or FilingParty.objects.filter(draft=draft, source="court").exists()
    ):
        # Caption suggestions remain in extracted_guesses for review.
        return
    existing = {party.pk: party for party in FilingParty.objects.filter(draft=draft, role="other")}
    kept: set[int] = set()
    next_order = max((party.sort_order for party in existing.values()), default=-1) + 1
    # Only one of them can be the person filing, however many rows say so.
    claimed_self = False

    for index, row in enumerate(rows):
        name = row.get("name", "").strip()
        side = row.get("side", "").strip()
        if side not in set(PartySide):
            side = ""
        role_hint = row.get("role_hint", "").strip()
        party = existing.get(row.get("id"))

        if party is None:
            # "Unknown" is the document extraction saying it found nobody, not
            # a person to add to the case.
            if not name or is_placeholder_name(name):
                continue
            party = FilingParty(draft=draft, role="other", source="document", sort_order=next_order + index)
        else:
            kept.add(party.pk)

        if name and name != party_display_name(party):
            apply_name(party, name)
        # A side the filer changed invalidates whatever court party type was
        # resolved from the old one, so it is cleared and resolved again.
        if side != party.party_side and party.party_type:
            party.party_type = ""
            party.party_type_name = ""
        party.party_side = side
        party.party_role_hint = role_hint
        # An organization can never be the person signed in, whose account is
        # registered to an individual -- the rule
        # ``people.party_can_be_the_filer`` enforces everywhere else, applied
        # here too so the answer is never *stored* on a row that could only be
        # ignored later. It also keeps a company off the one self slot: a name
        # typed into the same submit that ticked it is only known to be a
        # company by the time ``apply_name`` above has run.
        is_self = str(row.get("is_self", "")).lower() == "true" and not party.organization_name and not claimed_self
        claimed_self = claimed_self or is_self
        party.is_self = is_self
        party.save()

    for pk, party in existing.items():
        if pk not in kept:
            party.delete()
