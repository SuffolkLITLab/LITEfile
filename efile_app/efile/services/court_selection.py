"""Ask a filer the questions their own state's court structure actually needs.

Every e-filing service hands us one flat list of "courts": 207 of them in
Illinois, 170 in Massachusetts, 24 in Vermont. Dropping that list into a single
dropdown asks the filer to recognize their court among the ones that come
alphabetically near it, which is why a Peoria filer used to open the list on a
Cook County division.

The way out is not one better dropdown. It is asking the questions each state's
hierarchy is actually made of -- Illinois routes by county, Massachusetts by
court department plus a place the filer knows, Vermont by Superior Court unit --
and letting the answers narrow the same live list. So the questions live in each
jurisdiction's YAML (``court_selector:``) rather than in this module, and this
module is the small engine that runs them:

* a **step** is one question. It is shown when its ``when:`` conditions hold and
  it has something to ask; ``choice`` renders as radio cards, ``select`` as a
  dropdown, and ``location`` as a place lookup handed to a matcher.
* an **option** may carry a **court query** -- a few declarative rules matched
  against the live list. Answering a step narrows the pool to that query.
* the deepest answered step wins, so a broad answer ("Cook County") is replaced
  by the narrower one under it ("Cook County - Chancery - District 1").
* when the pool is down to a single court, that is the selection. When it is
  down to a handful, the filer picks from those instead of from 207.

Nothing here hardcodes a state. A jurisdiction with no ``court_selector:`` block
keeps the flat list it has today.

Court **names**, not codes, are what partner configuration matches on wherever
there is a choice: Tyler's codes differ per court and change without notice.
Codes still do the filing.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests
from django.conf import settings
from django.core.cache import cache

from efile.services.court_location import LocationLookupUnavailable, find_courts
from efile.utils.config_loader import config_loader

logger = logging.getLogger(__name__)

# Courts the e-filing service lists but nobody can file into: test fixtures,
# retired locations, and the "System" row. Matched against the lowercased name.
NON_FILING_COURT_MARKERS: tuple[str, ...] = (
    "(zodyssey)",
    "z -",
    "zz",
    "zdev",
    "courtview test",
    "rsi test",
    "do not use",
    "not used",
    "file & serve",
    "system",
)

# How long the live court list is reused for. It changes a few times a year, and
# the selector re-reads it on every answer the filer gives.
COURT_LIST_TTL_SECONDS = 600
# How long "nothing is dropped" stands after a court list could not be read.
HEADING_COURTS_RETRY_SECONDS = 60

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def is_non_filing_court(name: str) -> bool:
    """Whether a court name marks a row that exists only inside Tyler."""

    lowered = str(name or "").lower()
    return any(marker in lowered for marker in NON_FILING_COURT_MARKERS)


def selector_config(jurisdiction: str) -> dict[str, Any] | None:
    """The ``court_selector:`` block for a jurisdiction, if it has one."""

    try:
        config = config_loader.load_jurisdiction_config(jurisdiction)
    except Exception:  # pragma: no cover - a missing config is already logged
        logger.exception("Could not load configuration for %s", jurisdiction)
        return None
    selector = (config or {}).get("court_selector")
    if not isinstance(selector, dict) or not selector.get("steps"):
        return None
    return selector


def fetch_courts(jurisdiction: str) -> list[dict[str, str]]:
    """Every court the e-filing service will accept a filing for, as options.

    ``fileable_only`` is unreliable on the test service -- it hides courts that
    do offer filing categories -- so the whole named list is fetched and the
    rows that are plainly not courts are dropped here instead.
    """

    cache_key = f"court-selector:courts:{jurisdiction}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    url = f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/codes/courts/"
    response = requests.get(url, params={"fileable_only": False, "with_names": True}, timeout=15)
    response.raise_for_status()
    payload = response.json()
    courts = [
        {"value": str(court["code"]), "text": str(court["name"]).strip()}
        for court in payload
        if isinstance(court, dict) and court.get("code") and court.get("name")
        if not is_non_filing_court(court["name"])
    ]
    headings = heading_court_codes(
        jurisdiction, [str(court.get("code")) for court in payload if isinstance(court, dict)]
    )
    courts = [court for court in courts if court["value"] not in headings]
    rules = (selector_config(jurisdiction) or {}).get("court_names") or []
    for court in courts:
        court["text"] = _readable_name(court["text"], rules)
    courts.sort(key=lambda court: court["text"])
    cache.set(cache_key, courts, COURT_LIST_TTL_SECONDS)
    return courts


def _readable_name(name: str, rules: list[dict[str, str]]) -> str:
    """The name of a court as a court is named, not as the service files it.

    E-filing services name courts so they sort: "Juvenile Court -- Suffolk
    County -- Boston" puts the department first because that is how the list is
    organized. Nobody calls it that. It is the Boston Juvenile Court, and a
    filer holding paperwork is looking for those words. The first rule that
    matches wins, and a name no rule matches is already right.
    """

    for rule in rules:
        match = re.match(str(rule.get("match", "")), name, re.IGNORECASE)
        if not match:
            continue
        rewritten = str(rule.get("name", ""))
        for field, value in match.groupdict().items():
            rewritten = rewritten.replace(f"{{{field}}}", (value or "").strip())
        return " ".join(rewritten.split())
    return name


def heading_court_codes(jurisdiction: str, codes: list[str] | None = None) -> frozenset[str]:
    """Courts that are only headings over the courts that take filings.

    The court picker, the dropdown API and code search all leave these out, so
    a filer is never offered a court the e-filing service will refuse. Cached
    briefly; if either list cannot be read, only the courts a jurisdiction names
    as headings are dropped, and that answer is cached for a shorter time so
    search never waits on the service.

    ``court_selector.heading_courts`` names headings the shape of the list cannot
    reveal. Vermont's "Superior Courts" (``vermont:sc``) repeats every county
    unit's case types under its own code, but its units are coded ``sc:addison``,
    so nothing hangs off it.
    """

    configured = frozenset(str(code) for code in (selector_config(jurisdiction) or {}).get("heading_courts") or [])
    cache_key = f"court-selector:heading-courts:{jurisdiction}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached | configured
    if codes is None:
        codes = _court_codes(jurisdiction, fileable_only=False)
    fileable = None if codes is None else _court_codes(jurisdiction, fileable_only=True)
    if codes is None or fileable is None:
        cache.set(cache_key, frozenset(), HEADING_COURTS_RETRY_SECONDS)
        return configured
    fileable_codes = set(fileable)
    headings = frozenset(code for code in codes if _is_heading_only(code, codes, fileable_codes))
    cache.set(cache_key, headings, COURT_LIST_TTL_SECONDS)
    return headings | configured


def _court_codes(jurisdiction: str, *, fileable_only: bool) -> list[str] | None:
    """One of the e-filing service's court code lists, or ``None``.

    The fileable list is taken as a hint rather than as the truth. It leaves
    out courts that publish case categories and take filings -- nearly every
    Massachusetts District and Probate and Family Court, and Cook County's
    Elder Law locations -- so it only ever confirms what the shape of the list
    already suggests, and a request that fails changes nothing.
    """

    try:
        response = requests.get(
            f"{settings.EFSP_URL}/jurisdictions/{jurisdiction}/codes/courts/",
            params={"fileable_only": fileable_only, "with_names": True},
            timeout=5,
        )
        response.raise_for_status()
        return [str(court["code"]) for court in response.json() if isinstance(court, dict) and court.get("code")]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        logger.warning("Could not read the court list for %s", jurisdiction)
        return None


def _is_heading_only(code: str, codes: list[str], fileable: set[str]) -> bool:
    """Whether a court is only a heading over fileable courts underneath it.

    "Cook County" is such a row, and so is each Cook division: filings go to a
    district location such as "cook:cvd1", and choosing "cook:cvd" itself is
    refused by the e-filing service even though it publishes filing codes.
    Both things have to be true to drop a court -- the service leaves it out
    of its fileable list, and a court it does list as fileable hangs off its
    code -- because the fileable list alone leaves out real courts.
    """

    if code in fileable:
        return False
    return any(other != code and other in fileable and _hangs_off(other, code) for other in codes)


def _hangs_off(other: str, code: str) -> bool:
    # "cook" -> "cook:cvd1"; "cook:cvd" -> "cook:cvd1". A bare number is not a
    # prefix: Massachusetts court "1" does not head court "1036:BA".
    return other.startswith(f"{code}:") or (":" in code and other.startswith(code))


def _fill(template: Any, answers: dict[str, str]) -> Any:
    """Substitute ``{step_id}`` placeholders with the answers given so far."""

    if isinstance(template, str):
        return _PLACEHOLDER.sub(lambda match: str(answers.get(match.group(1), "")), template)
    if isinstance(template, list):
        return [_fill(item, answers) for item in template]
    if isinstance(template, dict):
        return {key: _fill(value, answers) for key, value in template.items()}
    return template


def _group_key(name: str, pattern: str) -> str:
    """The grouping name a court belongs to, e.g. "Kankakee" for four courts.

    Illinois names its counties' courts every which way -- "Adams County",
    "Kankakee - Civil", "Peoria CR", "St. Clair County-Backlog" -- so the county
    a court belongs to is read off the front of its name with a partner-supplied
    pattern rather than assumed to be the whole name.
    """

    match = re.match(pattern, name, re.IGNORECASE)
    if not match:
        return ""
    return (match.group(1) or "").strip()


def match_courts(
    courts: list[dict[str, str]],
    query: dict[str, Any] | None,
    answers: dict[str, str] | None = None,
    group_by: str = "",
) -> list[dict[str, str]]:
    """The courts a query selects, in the order the query implies.

    Supported rules, all optional and all combined with "and":

    ``codes``
        exact court codes, and the order to return them in.
    ``code_prefix`` / ``code_pattern``
        the code starts with, or matches, this.
    ``name_pattern``
        the name matches this (case-insensitive).
    ``group``
        the court's ``group_by`` name equals this.
    ``exclude_code_pattern`` / ``exclude_name_pattern``
        drop the courts these match.
    """

    if query is None:
        return list(courts)
    query = _fill(dict(query), answers or {})

    codes = [str(code) for code in query.get("codes", []) if code]
    code_prefix = query.get("code_prefix", "")
    code_pattern = query.get("code_pattern", "")
    name_pattern = query.get("name_pattern", "")
    group = query.get("group", "")
    exclude_code = query.get("exclude_code_pattern", "")
    exclude_name = query.get("exclude_name_pattern", "")

    selected = []
    for court in courts:
        code, name = court["value"], court["text"]
        if codes and code not in codes:
            continue
        if code_prefix and not code.startswith(code_prefix):
            continue
        if code_pattern and not re.search(code_pattern, code, re.IGNORECASE):
            continue
        if name_pattern and not re.search(name_pattern, name, re.IGNORECASE):
            continue
        if group and (not group_by or _group_key(name, group_by).casefold() != group.casefold()):
            continue
        if exclude_code and re.search(exclude_code, code, re.IGNORECASE):
            continue
        if exclude_name and re.search(exclude_name, name, re.IGNORECASE):
            continue
        selected.append(court)

    if codes:
        order = {code: index for index, code in enumerate(codes)}
        selected.sort(key=lambda court: order.get(court["value"], len(order)))
    return selected


def _visible(step: dict[str, Any], answers: dict[str, str]) -> bool:
    """Whether a step's ``when:`` conditions hold.

    ``when: {level: [trial], department: {not: [land]}}`` reads as "only for a
    Trial Court filing, and not for the Land Court". An unanswered step named in
    a condition fails it, which is what keeps later questions hidden until the
    ones they depend on are answered.
    """

    for field, condition in (step.get("when") or {}).items():
        answer = answers.get(field, "")
        if isinstance(condition, dict) and "not" in condition:
            # "not land" means "not known to be the Land Court". An unanswered
            # field passes: forward building never gets here with one, because
            # an unanswered step stops the walk, and working backwards from a
            # saved court has to be able to pass a step it cannot derive.
            if answer in {str(value) for value in condition["not"]}:
                return False
        else:
            allowed = condition if isinstance(condition, list) else [condition]
            if answer not in {str(value) for value in allowed}:
                return False
    return True


def _static_options(step: dict[str, Any]) -> list[dict[str, Any]]:
    options = [dict(option) for option in step.get("options", []) if isinstance(option, dict)]
    for group in step.get("option_groups", []) or []:
        # One option per member, each carrying the group's court query. Illinois
        # asks for the county an appeal came from and answers with its appellate
        # district, so the county is the option and the district is the court.
        for member in group.get("members", []) or []:
            options.append(
                {
                    "value": str(member),
                    "label": str(group.get("member_label", "{member} - {group}"))
                    .replace("{member}", str(member))
                    .replace("{group}", str(group.get("label", ""))),
                    "courts": group.get("courts"),
                }
            )
    return options


def _dynamic_options(
    step: dict[str, Any],
    courts: list[dict[str, str]],
    answers: dict[str, str],
    group_by: str,
) -> list[dict[str, Any]]:
    """Options read off the live court list rather than written out in YAML.

    Counties, Cook County's divisions and Vermont's units are all just the court
    list seen at a different depth, and generating them means the questions stay
    right when the e-filing service adds or renames a court.
    """

    spec = step.get("options_from_courts")
    if not isinstance(spec, dict):
        return []

    pool = match_courts(courts, spec.get("match"), answers, group_by)
    label_template = spec.get("label", "{name}")
    strip = spec.get("strip", "")
    grouping = group_by if spec.get("group") else spec.get("group_by", "")

    options: list[dict[str, Any]] = []
    seen: set[str] = set()
    for court in pool:
        name = court["text"]
        if grouping:
            key = _group_key(name, grouping)
            if not key or key.casefold() in seen:
                continue
            seen.add(key.casefold())
            options.append(
                {
                    "value": key,
                    "label": label_template.replace("{group}", key).replace("{name}", name),
                    "courts": spec.get("courts", {"group": "{value}"}),
                }
            )
            continue
        stem = re.sub(strip, "", name).strip() if strip else name
        label = label_template.replace("{name}", name).replace("{stem}", stem).replace("{code}", court["value"])
        group, within, extra = _option_group(spec.get("option_group_pattern", ""), stem)
        options.append(
            {
                "value": court["value"],
                "label": within or label,
                # What it has to be called away from its heading -- in the list
                # of answers already given, where there is no heading to read.
                "full_label": label,
                "group": group,
                "extra": extra,
                "courts": spec.get("courts", {"codes": ["{value}"]}),
            }
        )
    group_names = list(spec.get("option_group_names", []) or [])
    if group_names:
        return _name_groups(options, group_names, spec.get("option_group_other", ""))
    for option in options:
        option.pop("extra", "")
    return _demote_lonely_groups(sorted(options, key=lambda option: (option.get("group", ""), option["label"])))


def _option_group(pattern: str, name: str) -> tuple[str, str, str]:
    """Split a court's name into the heading it sits under and what is left.

    Cook County's e-filing locations are one long alphabetical run -- Chancery,
    then twenty-odd criminal branches, then the municipal districts -- and a
    filer is looking for their courthouse. Named captures say which part is
    which: ``group`` is the heading, ``label`` what to show under it, and
    ``extra`` anything that only matters when it is not the heading's own place.
    """

    if not pattern:
        return "", "", ""
    match = re.match(pattern, name, re.IGNORECASE)
    if not match:
        return "", "", ""
    named = match.groupdict()
    if "group" in named:
        return (
            (named.get("group") or "").strip(),
            (named.get("label") or "").strip(),
            (named.get("extra") or "").strip(),
        )
    groups = match.groups()
    within = (groups[1] or "").strip() if len(groups) > 1 else ""
    return (groups[0] or "").strip(), within, ""


def _name_groups(
    options: list[dict[str, Any]],
    names: list[str],
    other: str,
) -> list[dict[str, Any]]:
    """Give each heading its full name and its place in the order.

    Partners write the headings out because the court list does not carry them:
    the names say "District 2", and the filer knows the place. The order they
    are written in is the order they are shown in, which is how the Chicago
    courts come first in Cook County -- where most of its filings go.
    """

    headings = {}
    for position, name in enumerate(names):
        key = name.split(" - ")[0].strip()
        headings[key] = (position, name, name[len(key) :].lstrip(" -").strip())

    for option in options:
        position, heading, place = headings.get(option.get("group", ""), (len(names), "", ""))
        extra = option.pop("extra", "")
        if extra and extra != place:
            # A branch court inside the district, which its division alone does
            # not tell apart from the district's own courthouse.
            option["label"] = f"{option['label']} - {extra}" if option["label"] else extra
        option["group"] = heading if option.get("group") else other
        option["order"] = position if heading else len(names) + 1
    return sorted(options, key=lambda option: (option["order"], option["label"]))


def _demote_lonely_groups(options: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A heading over one court is a heading that says nothing twice."""

    counts: dict[str, int] = {}
    for option in options:
        group = option.get("group", "")
        counts[group] = counts.get(group, 0) + 1
    for option in options:
        if counts.get(option.get("group", ""), 0) < 2:
            option["group"] = ""
            option["label"] = option.get("full_label") or option["label"]
    # Ungrouped courts read first, then each heading with its own -- and under a
    # heading, the division itself before the courthouses inside it.
    return sorted(
        options,
        key=lambda option: (
            option.get("group", ""),
            option["label"] != option.get("group", ""),
            option["label"],
        ),
    )


def _default_answer(step: dict[str, Any], answers: dict[str, str]) -> str:
    """The answer to start this question at, given what is answered already.

    ``default_by`` reads one earlier answer and maps it to a court: Cook County
    starts at its Municipal Civil division, where most Cook filings go, because
    "Cook County" on its own is not a court anyone can file into.
    """

    spec = step.get("default_by")
    if not isinstance(spec, dict):
        return ""
    return str((spec.get("values") or {}).get(answers.get(spec.get("answer", ""), ""), ""))


def _answered_option(step: dict[str, Any], options: list[dict[str, Any]], answer: str) -> dict[str, Any] | None:
    for option in options:
        if str(option.get("value")) == answer:
            return option
    return None


def _public_step(step: dict[str, Any], options: list[dict[str, Any]], answer: str) -> dict[str, Any]:
    return {
        "id": step["id"],
        "type": step.get("type", "select"),
        "label": step.get("label", ""),
        "short_label": step.get("short_label", step.get("label", "")),
        "hint": step.get("hint", ""),
        "placeholder": step.get("placeholder", ""),
        "alternative_to": step.get("alternative_to", ""),
        "answer": answer,
        "defaulted": False,
        "default_hint": step.get("default_hint", ""),
        "options": [
            {
                "value": option["value"],
                "label": option.get("label", ""),
                "full_label": option.get("full_label", ""),
                "help": option.get("help", ""),
                "group": option.get("group", ""),
            }
            for option in options
        ],
    }


def build_selection(
    jurisdiction: str,
    answers: dict[str, str],
    courts: list[dict[str, str]] | None = None,
) -> dict[str, Any] | None:
    """Run the configured questions against the live court list.

    Returns what the screen needs to draw itself: the questions to ask now, the
    courts the answers have narrowed to, and the one court chosen if the answers
    have got that far. ``None`` means this jurisdiction has no configured
    selector and should keep its flat list.

    ``answers["court"]`` is the filer's final pick, and it is kept separate from
    the routing answers above it: a court is only ever the selection while it is
    still one of the courts those answers lead to.
    """

    config = selector_config(jurisdiction)
    if config is None:
        return None
    if courts is None:
        courts = fetch_courts(jurisdiction)

    answers = {key: str(value) for key, value in (answers or {}).items() if value not in (None, "")}
    group_by = config.get("group_by", "")

    steps: list[dict[str, Any]] = []
    chosen_options: dict[str, dict[str, Any]] = {}
    pool = list(courts)
    path: list[str] = []
    waiting = False  # a visible step is unanswered, so nothing below it applies
    waiting_on = ""
    location: dict[str, Any] | None = None

    for raw_step in config["steps"]:
        if not _visible(raw_step, answers):
            continue
        # Two ways of naming the same court -- the appellate district itself, or
        # the county the case came from. Both stay on screen so the filer can
        # use whichever their paperwork supports, but only one of them answers:
        # once its partner is answered, this step has nothing left to decide.
        alternative_to = raw_step.get("alternative_to", "")
        answered_instead = bool(alternative_to and answers.get(alternative_to))
        if waiting and alternative_to != waiting_on:
            continue

        if raw_step.get("type") == "location":
            step = _public_step(raw_step, [], "" if answered_instead else answers.get(raw_step["id"], ""))
            step["examples"] = list(raw_step.get("examples", []) or [])
            step["manual_label"] = raw_step.get("manual_label", "")
            step["button_label"] = raw_step.get("button_label", "")
            step["no_match_hint"] = raw_step.get(
                "no_match_hint",
                "We could not match that place to a court. Try adding more detail, or choose your court from the list.",
            )
            steps.append(step)
            if answered_instead:
                # The filer named their court outright, so there is nothing to
                # look up. The box stays on screen as the other way in.
                continue
            location = _lookup_location(raw_step, answers, chosen_options, pool)
            # One court serves this place, so that is the answer, shown by name
            # with the reason it was chosen. Anything else -- overlapping
            # jurisdictions, or a place the rules could not place at all --
            # leaves the filer to say which court is theirs, from the matches or
            # from the whole department. The pool stays the whole department
            # either way, because the list is always the way out.
            if location.get("place"):
                path.append(location["place"])
            matched = location.get("matched") or []
            if len(matched) == 1:
                answers.setdefault("court", matched[0]["value"])
                if waiting and alternative_to == waiting_on:
                    waiting, waiting_on = False, ""
            elif not waiting:
                waiting, waiting_on = True, raw_step["id"]
            continue

        options = _static_options(raw_step) or _dynamic_options(raw_step, pool, answers, group_by)
        if raw_step.get("options_from_courts") and len(options) < 2:
            # Nothing to ask: this county has one court, or this department one
            # location. Skipping keeps the extra question for the places that
            # genuinely have one, which in Illinois is mostly Cook County.
            continue

        given = "" if answered_instead else answers.get(raw_step["id"], "")
        chosen = _answered_option(raw_step, options, given)
        # A county with dozens of filing locations can start the filer at the
        # one most filings go to rather than at nothing. The question stays on
        # screen with that answer in it, so a suggestion is never mistaken for
        # something the filer said.
        defaulted = chosen is None and not answered_instead
        if defaulted:
            chosen = _answered_option(raw_step, options, _default_answer(raw_step, answers))
            defaulted = chosen is not None
        step = _public_step(raw_step, options, chosen["value"] if chosen else "")
        step["defaulted"] = defaulted
        steps.append(step)
        if chosen is None:
            if not waiting and not answered_instead:
                waiting, waiting_on = True, raw_step["id"]
            continue
        if waiting and alternative_to == waiting_on:
            waiting, waiting_on = False, ""

        chosen_options[raw_step["id"]] = chosen
        # The whole name, not the short one its heading made possible: the
        # trail is read on its own, with no heading above it.
        path.append(chosen.get("full_label") or chosen.get("label", ""))
        if chosen.get("courts"):
            pool = match_courts(pool, chosen["courts"], {**answers, "value": chosen["value"]}, group_by)

    # One court left, and every question answered, is an answer in itself: there
    # is nothing to choose between, so the filer is not asked to.
    selected = None
    picked = answers.get("court", "")
    if picked:
        selected = next((court for court in pool if court["value"] == picked), None)
    if selected is None and not waiting and len(pool) == 1:
        selected = pool[0]

    return {
        "jurisdiction": jurisdiction,
        "title": config.get("title", ""),
        "lede": config.get("lede", ""),
        "steps": steps,
        "path": path,
        "courts": pool,
        "location": location,
        "waiting": waiting,
        "selected": selected,
        "complete": selected is not None,
    }


def _lookup_location(
    step: dict[str, Any],
    answers: dict[str, str],
    chosen_options: dict[str, dict[str, Any]],
    pool: list[dict[str, str]],
) -> dict[str, Any]:
    """Ask the configured matcher which of ``pool`` serves the place typed in.

    A lookup that finds nothing is not an error and not a dead end: the same
    screen still offers every court in ``pool`` to choose from by hand, which is
    the only way through for an address the rules cannot place -- a Boston
    neighborhood with no street number, say, where which BMC division serves it
    is a question about a specific building.
    """

    place = answers.get(step["id"], "")
    source = step.get("court_types_from", "")
    court_types = list((chosen_options.get(source) or {}).get("court_types", []) or [])
    result: dict[str, Any] = {"place": place, "matched": [], "searched": bool(place), "error": ""}
    if not place:
        return result
    try:
        result["matched"] = find_courts(step.get("matcher", ""), place, court_types, pool)
    except LocationLookupUnavailable as error:
        logger.warning("Court location lookup unavailable for %s: %s", step.get("matcher", ""), error)
        result["error"] = str(error)
    except Exception:
        logger.exception("Court location lookup failed for place %r", place)
        result["error"] = "We could not look that place up just now. Choose a court from the list instead."
    return result


def derive_answers(jurisdiction: str, court_code: str, courts: list[dict[str, str]] | None = None) -> dict[str, str]:
    """Work backwards from a saved court code to the answers that reach it.

    A filer who comes back to a draft, or whose document named their court,
    should find the questions already answered rather than an empty first step.
    Each step is asked which of its options still contains the saved court, and
    the answer that does is filled in.
    """

    config = selector_config(jurisdiction)
    if config is None or not court_code:
        return {}
    if courts is None:
        courts = fetch_courts(jurisdiction)
    if not any(court["value"] == court_code for court in courts):
        return {}

    group_by = config.get("group_by", "")
    answers: dict[str, str] = {}
    pool = list(courts)
    for _ in range(len(config["steps"])):
        progressed = False
        for raw_step in config["steps"]:
            if raw_step["id"] in answers or raw_step.get("type") == "location":
                continue
            if answers.get(raw_step.get("alternative_to", "")):
                # Its partner already named this court; asking twice would only
                # invite the two answers to disagree.
                continue
            if not _visible(raw_step, answers):
                continue
            options = _static_options(raw_step) or _dynamic_options(raw_step, pool, answers, group_by)
            if raw_step.get("options_from_courts") and len(options) < 2:
                continue
            for option in options:
                query = option.get("courts")
                narrowed = match_courts(pool, query, {**answers, "value": option["value"]}, group_by) if query else pool
                if query and any(court["value"] == court_code for court in narrowed):
                    answers[raw_step["id"]] = str(option["value"])
                    pool = narrowed
                    progressed = True
                    break
            if progressed:
                break
        if not progressed:
            break
    return answers


def _significant_words(text: str) -> set[str]:
    return {word for word in re.split(r"[^a-z0-9]+", str(text or "").lower()) if word}


def courts_matching_text(guess: str, courts: list[dict[str, str]], group_by: str = "") -> list[dict[str, str]]:
    """The courts a free-text court name from a document could be naming.

    A court matches when every word of its name appears in the guess, so a
    caption reading "In the Circuit Court of Cook County" matches "Cook County"
    and not "Cook County - Chancery - District 1 - Chicago", which says three
    things the document never did. Only the most specific matches are kept: a
    document that did name the division should not still be offered the county.

    A match that other courts sit under brings them with it. "Circuit Court of
    Cook County" is true of every Cook division, so it can settle the county and
    nothing below it -- which is the point: the document named a county, and
    choosing a division on its behalf would be inventing something it never said.
    """

    words = _significant_words(guess)
    if not words:
        return []
    matches = [court for court in courts if _significant_words(court["text"]) <= words]
    if not matches and group_by:
        # No single court, but the document may still have named the county
        # every court in it belongs to. "Circuit Court of Cook County" is the
        # ordinary case: Cook has no court of its own to match, and eighty
        # locations that all belong to it.
        named = {
            key
            for key in (_group_key(court["text"], group_by) for court in courts)
            if key and _significant_words(key) <= words
        }
        return [court for court in courts if _group_key(court["text"], group_by) in named]
    if not matches:
        return []
    longest = max(len(_significant_words(court["text"])) for court in matches)
    matches = [court for court in matches if len(_significant_words(court["text"])) == longest]

    found = {court["value"]: court for court in matches}
    for match in matches:
        for court in courts:
            under = court["value"].startswith(f"{match['value']}:") or (
                court["text"].startswith(match["text"]) and court["text"] != match["text"]
            )
            if under:
                found[court["value"]] = court
    return list(found.values())


def derive_answers_from_guess(
    jurisdiction: str,
    guess: str,
    courts: list[dict[str, str]] | None = None,
) -> dict[str, str]:
    """Answer the questions a document's own words already settle.

    A caption that names the county answers the county question, even when it
    leaves the division open -- which is the ordinary case, since captions say
    "Circuit Court of Cook County" and stop there. A step is only answered when
    every court the guess could mean falls under one of its options, so nothing
    here decides something the document did not say.
    """

    config = selector_config(jurisdiction)
    if config is None or not guess:
        return {}
    if courts is None:
        courts = fetch_courts(jurisdiction)

    group_by = config.get("group_by", "")
    candidates = courts_matching_text(guess, courts, group_by)
    if not candidates:
        return {}
    if len(candidates) == 1:
        answers = derive_answers(jurisdiction, candidates[0]["value"], courts)
        answers["court"] = candidates[0]["value"]
        return answers

    wanted = {court["value"] for court in candidates}
    answers = {}
    pool = list(courts)
    for _ in range(len(config["steps"])):
        progressed = False
        for raw_step in config["steps"]:
            if raw_step["id"] in answers or raw_step.get("type") == "location":
                continue
            if answers.get(raw_step.get("alternative_to", "")) or not _visible(raw_step, answers):
                continue
            options = _static_options(raw_step) or _dynamic_options(raw_step, pool, answers, group_by)
            if raw_step.get("options_from_courts") and len(options) < 2:
                continue
            for option in options:
                query = option.get("courts")
                if not query:
                    continue
                narrowed = match_courts(pool, query, {**answers, "value": option["value"]}, group_by)
                if wanted <= {court["value"] for court in narrowed}:
                    answers[raw_step["id"]] = str(option["value"])
                    pool, progressed = narrowed, True
                    break
            if progressed:
                break
        if not progressed:
            break
    return answers
