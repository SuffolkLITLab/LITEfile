"""Help a filer who has no documents yet work out which ones they need.

The filing-code search starts from a document: "I am filing a motion, what does
the court call it?" This starts one step earlier, from what the filer wants to
do: "I want to change my name." It answers with the same partner checklist the
filer would later see on Check documents, and with places to get the forms.

Three rules keep it honest:

* An intent is a pointer to a checklist that already exists. It names a
  ``case_types`` or ``case_categories`` entry by its configuration key, plus the
  side of the case and the variant of the lead document when those matter. It
  never carries its own list of documents, so the finder and the filing plan
  cannot disagree.
* It never picks a court code. A filer who starts a filing from here carries
  over only whether it starts a case or joins one; the court, case type, and
  filing types are still chosen against the court's live lists.
* Every link is one a partner reviewed and wrote down. Links that are not
  ``https`` are dropped rather than shown.

The index is small and static -- the common filings partners have written
checklists for -- so it is searched in memory on every request.
"""

from __future__ import annotations

import logging
from typing import Any

from efile.services.document_checklists import (
    REQUIREMENT_LABELS,
    REQUIREMENT_ORDER,
    checklist_for_entry,
    configured_entry,
    entry_court_names,
    guidance_for_entry,
)
from efile.services.filing_code_search import search_tokens, stems
from efile.utils.config_loader import config_loader
from efile.workflow import ExistingCase

logger = logging.getLogger(__name__)

# What each kind of link is, in the order they are offered. A guided interview
# comes first because it is the easiest way to end up with correct forms.
RESOURCE_KINDS: dict[str, str] = {
    "interview": "Guided interview",
    "form": "Blank form",
    "instructions": "Instructions",
    "help": "Help page",
}

FILING_PATH_LABELS: dict[str, str] = {
    ExistingCase.NEW: "Starts a new case",
    ExistingCase.EXISTING: "For a case that already exists",
}

# Words that say nothing about which filing someone means. "I need to file
# court documents for my case" matches everything otherwise.
_GENERIC_WORDS = (
    "case",
    "court",
    "file",
    "filing",
    "document",
    "documents",
    "form",
    "forms",
    "paper",
    "papers",
    "need",
    "get",
    "help",
    "do",
    "what",
    "me",
    "please",
)
_GENERIC_STEMS = frozenset(stems(" ".join(_GENERIC_WORDS)))

# A query word found in an intent's title, keywords, or the court's own name
# for the case type counts for more than one found only in its description or
# the label of a document on its list.
_STRONG = 3
_WEAK = 1


def _finder_config(jurisdiction: str) -> dict[str, Any]:
    if not jurisdiction:
        return {}
    config = config_loader.load_jurisdiction_config(jurisdiction) or {}
    finder = config.get("document_finder")
    return finder if isinstance(finder, dict) else {}


def _as_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _clean_resources(raw: Any, where: str) -> list[dict[str, str]]:
    """Keep the well-formed, ``https`` links from a configured resource list."""

    if not isinstance(raw, list):
        return []
    resources = []
    for resource in raw:
        if not isinstance(resource, dict):
            logger.warning("Document finder resource in %s is not a mapping; ignoring it", where)
            continue
        kind = str(resource.get("kind") or "")
        url = str(resource.get("url") or "").strip()
        label = str(resource.get("label") or "").strip()
        if kind not in RESOURCE_KINDS or not label:
            logger.warning("Document finder resource %r in %s needs a known kind and a label", label or url, where)
            continue
        if not url.lower().startswith("https://"):
            logger.warning("Ignoring document finder link that is not https in %s: %r", where, url)
            continue
        cleaned = {"kind": kind, "kind_label": RESOURCE_KINDS[kind], "label": label, "url": url}
        for optional in ("source", "description"):
            if resource.get(optional):
                cleaned[optional] = str(resource[optional])
        resources.append(cleaned)
    order = list(RESOURCE_KINDS)
    resources.sort(key=lambda resource: order.index(resource["kind"]))
    return resources


def _checklist_ref(intent: dict[str, Any]) -> tuple[str, str]:
    """Which checklist entry an intent points at, as ``(section, key)``."""

    ref = intent.get("checklist")
    if not isinstance(ref, dict):
        return "", ""
    if ref.get("case_type"):
        return "case_types", str(ref["case_type"])
    if ref.get("case_category"):
        return "case_categories", str(ref["case_category"])
    return "", ""


def _intent_entry(jurisdiction: str, intent: dict[str, Any], court_code: str = "") -> dict[str, Any] | None:
    section, key = _checklist_ref(intent)
    return configured_entry(jurisdiction, section, key, court_code) if section else None


def _filing_path(intent: dict[str, Any]) -> str:
    path = str(intent.get("filing_path") or "")
    return path if path in FILING_PATH_LABELS else ""


def _role_label(entry: dict[str, Any] | None, filer_role: str) -> str:
    role = ((entry or {}).get("filer_roles") or {}).get(filer_role)
    if not isinstance(role, dict):
        return ""
    return str(role.get("label") or filer_role.replace("_", " ").capitalize())


def _intents(jurisdiction: str) -> dict[str, dict[str, Any]]:
    intents = _finder_config(jurisdiction).get("intents")
    if not isinstance(intents, dict):
        return {}
    return {str(key): value for key, value in intents.items() if isinstance(value, dict) and value.get("title")}


def has_document_finder(jurisdiction: str) -> bool:
    return bool(_intents(jurisdiction))


def _summary(jurisdiction: str, intent_id: str, intent: dict[str, Any]) -> dict[str, Any]:
    entry = _intent_entry(jurisdiction, intent)
    path = _filing_path(intent)
    filer_role = str(intent.get("filer_role") or "")
    return {
        "id": intent_id,
        "title": str(intent["title"]),
        "summary": str(intent.get("summary") or ""),
        "filing_path": path,
        "filing_path_label": FILING_PATH_LABELS.get(path, ""),
        "role_label": _role_label(entry, filer_role),
    }


def _index_text(jurisdiction: str, intent: dict[str, Any]) -> tuple[str, str]:
    """The text an intent is found by: ``(strong, weak)``."""

    entry = _intent_entry(jurisdiction, intent)
    strong = [str(intent.get("title") or ""), *_as_list(intent.get("keywords")), *entry_court_names(entry)]
    checklist = checklist_for_entry(
        entry,
        str(intent.get("lead_filing_type_name") or ""),
        str(intent.get("filer_role") or ""),
    )
    weak = [str(intent.get("summary") or ""), *(item["label"] for item in checklist.values())]
    return " ".join(strong), " ".join(weak)


def _query_tokens(query: str, jurisdiction: str) -> set[str]:
    return {token for token in search_tokens(query, jurisdiction, query=True) if token not in _GENERIC_STEMS}


def search_intents(jurisdiction: str, query: str = "", filing_path: str = "") -> dict[str, Any]:
    """Find the configured intents a plain-language description fits.

    Returns ``{"results": [...], "searched": bool}``. With nothing to search for,
    every intent is listed, because the whole index fits on one screen and
    browsing it is a fair way to find your situation. ``filing_path`` narrows the
    list to filings that start a case or that join one.

    An intent is listed when a query word appears in its title, keywords, or the
    court's own name for its case type -- or when every query word appears
    somewhere in it, including the documents on its list. Nothing is chosen for
    the filer: several matches are all shown, best first.
    """

    intents = _intents(jurisdiction)
    if filing_path in FILING_PATH_LABELS:
        intents = {key: intent for key, intent in intents.items() if _filing_path(intent) == filing_path}

    tokens = _query_tokens(query, jurisdiction)
    if not tokens:
        return {
            "results": [_summary(jurisdiction, key, intent) for key, intent in intents.items()],
            "searched": bool(query.strip()),
        }

    scored = []
    for position, (key, intent) in enumerate(intents.items()):
        strong_text, weak_text = _index_text(jurisdiction, intent)
        strong = search_tokens(strong_text, jurisdiction)
        weak = search_tokens(weak_text, jurisdiction)
        strong_hits = tokens & strong
        all_hits = tokens & (strong | weak)
        if not strong_hits and all_hits != tokens:
            continue
        score = _STRONG * len(strong_hits) + _WEAK * len(all_hits - strong_hits)
        scored.append((-score, position, key, intent))

    scored.sort(key=lambda row: row[:2])
    return {
        "results": [_summary(jurisdiction, key, intent) for _score, _position, key, intent in scored],
        "searched": True,
    }


def _court_variants(
    jurisdiction: str,
    intent: dict[str, Any],
    statewide: dict[str, dict[str, Any]],
) -> list[dict[str, str]]:
    """Documents that some courts add to the statewide list.

    The finder runs before a court is chosen, so it shows the statewide list --
    and, separately, what particular courts are known to ask for on top of it,
    so a filer in one of them is not surprised later.
    """

    section, key = _checklist_ref(intent)
    if not section:
        return []
    config = config_loader.load_jurisdiction_config(jurisdiction) or {}
    courts = config.get("court_specific_requirements") or {}
    variants: dict[str, dict[str, str]] = {}
    for court_code, requirements in courts.items():
        if not isinstance(requirements, dict) or key not in (requirements.get(section) or {}):
            continue
        entry = configured_entry(jurisdiction, section, key, str(court_code))
        local = checklist_for_entry(
            entry,
            str(intent.get("lead_filing_type_name") or ""),
            str(intent.get("filer_role") or ""),
        )
        for item_id, item in local.items():
            if item_id in statewide or item["label"] in variants:
                continue
            variants[item["label"]] = {"label": item["label"], "description": item.get("description", "")}
    return list(variants.values())


def intent_detail(jurisdiction: str, intent_id: str) -> dict[str, Any] | None:
    """Everything the page for one intent shows, or ``None`` if there is no such intent."""

    intent = _intents(jurisdiction).get(intent_id)
    if intent is None:
        return None

    entry = _intent_entry(jurisdiction, intent)
    filer_role = str(intent.get("filer_role") or "")
    checklist = checklist_for_entry(entry, str(intent.get("lead_filing_type_name") or ""), filer_role)
    if entry is not None and not checklist:
        logger.warning("Document finder intent %r resolves to an empty checklist", intent_id)

    document_resources = intent.get("document_resources") or {}
    if not isinstance(document_resources, dict):
        document_resources = {}
    groups: dict[str, list[dict[str, Any]]] = {level: [] for level in REQUIREMENT_ORDER}
    for item_id, item in checklist.items():
        groups[item["requirement"]].append(
            {
                "id": item_id,
                "label": item["label"],
                "description": item.get("description", ""),
                "resources": _clean_resources(
                    document_resources.get(item_id), f"{jurisdiction}.document_finder.{intent_id}.{item_id}"
                ),
            }
        )

    resources = _clean_resources(intent.get("resources"), f"{jurisdiction}.document_finder.{intent_id}")
    if not config_loader.document_checklists_enabled(jurisdiction):
        # Keep individual form links available without presenting them as a checklist.
        seen = {resource["url"] for resource in resources}
        for items in groups.values():
            for item in items:
                for resource in item["resources"]:
                    if resource["url"] not in seen:
                        resources.append(resource)
                        seen.add(resource["url"])

    return {
        **_summary(jurisdiction, intent_id, intent),
        "groups": [
            {"requirement": level, "label": REQUIREMENT_LABELS[level], "items": items}
            for level, items in groups.items()
            if items
        ],
        "guidance": guidance_for_entry(entry, filer_role),
        "resources": resources,
        "court_variants": _court_variants(jurisdiction, intent, checklist),
    }


def help_resource(jurisdiction: str) -> dict[str, str]:
    """The jurisdiction's own court-help contact, for when the finder has nothing."""

    if not jurisdiction:
        return {}
    info = (config_loader.load_jurisdiction_config(jurisdiction) or {}).get("jurisdiction") or {}
    url = str(info.get("help_url") or "")
    if not url.lower().startswith("https://"):
        return {}
    return {
        "url": url,
        "name": str(info.get("help_name") or ""),
        "number": str(info.get("help_number") or ""),
    }


def configured_links(jurisdiction: str) -> list[tuple[str, dict[str, str]]]:
    """Every link the finder would show, with where it is configured.

    For keeping links current: court PDF addresses change when a form is
    revised, and a dead link is worse than none.
    """

    links = []
    for intent_id, intent in _intents(jurisdiction).items():
        where = intent_id
        links.extend((where, resource) for resource in _clean_resources(intent.get("resources"), where))
        document_resources = intent.get("document_resources") or {}
        if not isinstance(document_resources, dict):
            continue
        for item_id, raw in document_resources.items():
            where = f"{intent_id}.{item_id}"
            links.extend((where, resource) for resource in _clean_resources(raw, where))
    return links
