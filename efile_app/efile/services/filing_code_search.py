"""Deterministic search of complete EFSP paths, with portable local storage.

Snowball stems and YAML concepts are materialized when refreshing the index.
PostgreSQL uses an indexed simple tsvector; SQLite uses an FTS5 inverted index.
No external service is called while searching. Selection is revalidated live.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, nullcontext
from difflib import get_close_matches
from functools import lru_cache
from itertools import islice
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory, TemporaryFile
from threading import Lock, local
from urllib.parse import quote

import requests
import snowballstemmer
import yaml
from django.conf import settings
from django.contrib.postgres.search import SearchQuery, SearchVector
from django.core.cache import cache, caches
from django.db import connection, transaction
from django.db.models import Case, Count, IntegerField, Max, Q, Value, When
from django.db.models.expressions import RawSQL
from django.utils import timezone
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from efile.db_expressions import CourtCode
from efile.models import FilingCodeIndex, FilingCodeLabel, FilingCodePath
from efile.services.case_location import index_courts, locate
from efile.services.case_type_guidance import (
    _money,
    case_guidance,
    claim_range,
    facet_metadata,
    matches_amount,
    matches_case_filters,
    validate_case_filters,
)
from efile.services.case_type_guidance import case_topic as topic_of
from efile.services.court_selection import heading_court_codes, is_non_filing_court
from efile.services.filing_availability import filing_unavailable_message
from efile.services.filing_code_labels import LabelCounts
from efile.services.glossary import glossary_for

FACETS = ("court", "case_category", "case_type", "filing_type")
RULES_PATH = Path(__file__).resolve().parents[1] / "data" / "filing_code_search.yaml"
DEFINITIONS_PATH = RULES_PATH.with_name("filing_concept_definitions.yaml")
_stemmer_local = local()


@lru_cache(maxsize=1)
def rules():
    raw = RULES_PATH.read_bytes()
    config = yaml.safe_load(raw)
    if config.get("version") != 1 or config.get("stemming") != "english":
        raise ValueError("Unsupported filing search rules version or stemmer")
    if config.get("guided_form_aliases_file"):
        supplement = RULES_PATH.with_name(config["guided_form_aliases_file"]).read_bytes()
        aliases = json.loads(supplement)
        if aliases.get("version") != 1:
            raise ValueError("Unsupported guided form aliases version")
        duplicates = config["concepts"].keys() & aliases["concepts"].keys()
        if duplicates:
            raise ValueError(f"Duplicate guided form concepts: {sorted(duplicates)}")
        config["concepts"].update(aliases["concepts"])
        config.setdefault("path_aliases", []).extend(aliases["path_aliases"])
        config["guided_definitions"] = aliases.get("definitions", {})
        raw += b"\n" + supplement
    # Bump this prefix whenever tokenization changes incompatibly.
    return config, hashlib.sha256(b"filing-search-v2-court-aliases\n" + raw).hexdigest()


def words(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", text)


def stemmer():
    # Snowball keeps mutable cursor state. ASGI runs Django views in threads.
    if not hasattr(_stemmer_local, "english"):
        _stemmer_local.english = snowballstemmer.stemmer("english")
    return _stemmer_local.english


@lru_cache(maxsize=32768)
def stems(text):
    return stemmer().stemWords(words(text))


@lru_cache(maxsize=8)
def concept_phrases(jurisdiction, path_query=False):
    config, _ = rules()
    phrases = []
    for name, concept in config["concepts"].items():
        if jurisdiction not in concept.get("jurisdictions", config["jurisdictions"]):
            continue
        if concept.get("path_only") and not path_query:
            continue
        for term in concept["terms"]:
            phrases.append((stems(term), "concept" + re.sub(r"[^a-z0-9]", "", name)))
    return sorted(phrases, key=lambda pair: -len(pair[0]))


@lru_cache(maxsize=8)
def path_only_concept_tokens(jurisdiction):
    config, _ = rules()
    return frozenset(
        "concept" + re.sub(r"[^a-z0-9]", "", name)
        for name, concept in config["concepts"].items()
        if concept.get("path_only") and jurisdiction in concept.get("jurisdictions", config["jurisdictions"])
    )


@lru_cache(maxsize=32768)
def search_tokens(text, jurisdiction, *, query=False, path_query=False):
    tokens = stems(text)
    concepts = set()
    consumed = set()
    for phrase, concept in concept_phrases(jurisdiction, path_query):
        for start in range(len(tokens) - len(phrase) + 1):
            positions = set(range(start, start + len(phrase)))
            if query and consumed & positions:
                continue
            if tokens[start : start + len(phrase)] == phrase:
                concepts.add(concept)
                consumed.update(positions)
    stop = set(stems(" ".join(rules()[0]["stop_words"])))
    return concepts | {
        token for i, token in enumerate(tokens) if token not in stop and (not query or i not in consumed)
    }


def packed(tokens):
    return " " + " ".join(sorted(tokens)) + " "


@lru_cache(maxsize=1)
def concept_definitions():
    config = yaml.safe_load(DEFINITIONS_PATH.read_bytes())
    if config.get("version") != 1:
        raise ValueError("Unsupported concept definitions version")
    config.setdefault("concepts", {}).update(rules()[0].get("guided_definitions", {}))
    return config


def concept_meaning(concept, jurisdiction, court_name=""):
    """Resolve display text only, with explicit local scope and precedence."""
    config = concept_definitions()
    definition = config.get("concepts", {}).get(concept, {})
    result = {
        "key": concept,
        "label": definition.get("label", concept.replace("_", " ").capitalize()),
        "text": "",
        "source": "",
        "scope": "general",
        "scope_name": "",
    }

    def apply(override, scope, name=""):
        if "text" in override:
            # A local definition must not silently inherit a statewide source
            # that may not support the local meaning.
            result.update(text=override["text"], source=override.get("source", ""), scope=scope, scope_name=name)
        if "label" in override:
            result["label"] = override["label"]

    apply(definition.get("default", {}), "general")
    state = definition.get("jurisdictions", {}).get(jurisdiction, {})
    apply(state, "state", jurisdiction.replace("_", " ").title())
    if court_name:
        normalized = court_name.casefold().strip()
        for county, selectors in config.get("county_courts", {}).get(jurisdiction, {}).items():
            exact = any(normalized == name.casefold().strip() for name in selectors.get("names", []))
            prefix = any(normalized.startswith(name.casefold()) for name in selectors.get("prefixes", []))
            if exact or prefix:
                apply(state.get("counties", {}).get(county, {}), "county", county)
                break
        for name, override in state.get("courts", {}).items():
            if normalized == name.casefold().strip():
                apply(override, "court", court_name)
                break
    return result


def search_context(jurisdiction, query, labels, *, court_name="", corrected=(), resolved_query=None):
    """Explain discovery, without claiming a matched topic defines a document.

    A group contains filing labels only; a path also provides its case/court
    labels. Query concepts can match that associated context, not the document.
    """
    query_tokens = search_tokens(
        resolved_query if resolved_query is not None else query, jurisdiction, query=True, path_query=True
    )
    name_tokens = set().union(*(search_tokens(name, jurisdiction, query=True) for _, name in labels))
    related = []
    meanings = []
    for key, concept in rules()[0]["concepts"].items():
        if jurisdiction not in concept.get("jurisdictions", rules()[0]["jurisdictions"]):
            continue
        token = "concept" + re.sub(r"[^a-z0-9]", "", key)
        if token in query_tokens | name_tokens:
            meanings.append(concept_meaning(key, jurisdiction, court_name))
        if token in query_tokens:
            related.append(key)
    normalized_query = " ".join(words(query))
    filing_names = [name for facet, name in labels if facet == "filing_type"]
    title = filing_names[0] if filing_names else ""
    literal = normalized_query and normalized_query in " ".join(words(title))
    query_words = set(stems(query)) - set(stems(" ".join(rules()[0]["stop_words"])))
    title_words = set(stems(title))
    overlap = bool(query_words) and len(query_words & title_words) / len(query_words) >= 0.6
    reason = None
    if query and not literal and not overlap:
        if any(normalized_query and normalized_query in " ".join(words(name)) for name in filing_names[1:]):
            kind = "alternate_name"
        elif related:
            kind = "related_concept"
        elif corrected:
            kind = "spelling"
        else:
            kind = "context"
        reason = {"kind": kind, "query": query, "concepts": related, "corrected_terms": list(corrected)}
    return {"concepts": meanings, "match_reason": reason}


@lru_cache(maxsize=8)
def explanation_rules(jurisdiction, initial):
    compiled = []
    facets = set()
    for rule in rules()[0]["explanations"]:
        if jurisdiction not in rule["jurisdictions"]:
            continue
        if "initial" in rule and rule["initial"] != initial:
            continue
        constraints = {facet: {tuple(words(label)) for label in rule[facet]} for facet in FACETS if facet in rule}
        facets.update(constraints)
        if rule.get("case_concepts"):
            constraints["case_concepts"] = rule["case_concepts"]
            facets.update(("case_category", "case_type"))
        compiled.append((constraints, rule["text"], rule["source"]))
    return tuple(facet for facet in FACETS if facet in facets), compiled


@lru_cache(maxsize=32768)
def _explanation_for_names(jurisdiction, initial, names):
    facets, compiled = explanation_rules(jurisdiction, initial)
    normalized = dict(zip(facets, (tuple(words(name)) for name in names), strict=True))
    for constraints, text, source in compiled:
        if _matches_alias_constraints(jurisdiction, normalized, constraints):
            return text, source
    return "", ""


def explanation_for(jurisdiction, path, initial):
    # Catalogs repeat the same filing labels across thousands of valid paths.
    # Cache only the facets that rules actually constrain, preserving exact
    # context-specific matches without re-normalizing every rule for every row.
    facets, _ = explanation_rules(jurisdiction, initial)
    return _explanation_for_names(jurisdiction, initial, tuple(path[facet]["name"] for facet in facets))


def _matches_alias_constraints(jurisdiction, names, constraints):
    for facet, labels in constraints.items():
        if facet == "case_concepts":
            context = " ".join((*names["case_category"], *names["case_type"]))
            tokens = search_tokens(context, jurisdiction)
            if not all("concept" + re.sub(r"[^a-z0-9]", "", key) in tokens for key in labels):
                return False
        elif facet == "court_codes":
            if names.get("court_code", "") not in labels:
                return False
        elif facet == "court_code_prefixes":
            if not any(names.get("court_code", "").startswith(prefix) for prefix in labels):
                return False
        elif names[facet] not in labels:
            return False
    return True


@lru_cache(maxsize=24)
def path_alias_rules(jurisdiction, initial, facet="filing_type"):
    """Compile exact constraints shared by grouped labels and individual paths.

    Court selectors use provider codes, not fuzzy court-name matching. Grouped
    labels include their indexed vocabulary in their identity so a local alias
    cannot leak into another court with the same provider label names.
    """
    compiled = []
    for rule in rules()[0].get("path_aliases", []):
        target = rule.get("facet", "filing_type")
        if target not in {"filing_type", "case_category", "case_type"} or target not in rule or "initial" not in rule:
            raise ValueError("Path aliases require an exact target facet label and initial stage")
        if "court" in rule:
            raise ValueError("Use court_codes or court_code_prefixes for court-specific aliases")
        if target != facet:
            continue
        if jurisdiction not in rule["jurisdictions"] or initial != rule["initial"]:
            continue
        constraints = {facet: {tuple(words(label)) for label in rule[facet]} for facet in FACETS if facet in rule}
        if rule.get("case_concepts"):
            constraints["case_concepts"] = rule["case_concepts"]
        for selector in ("court_codes", "court_code_prefixes"):
            if selector in rule:
                if not rule[selector] or any(not value for value in rule[selector]):
                    raise ValueError("Court alias selectors cannot be empty")
                constraints[selector] = tuple(rule[selector])
        tokens = set().union(*(search_tokens(term, jurisdiction) for term in rule.get("terms", [])))
        tokens.update("concept" + re.sub(r"[^a-z0-9]", "", key) for key in rule.get("concepts", []))
        compiled.append((constraints, tokens))
    return compiled


@lru_cache(maxsize=32768)
def path_alias_tokens(jurisdiction, initial, category, case_type, filing, court_code="", facet="filing_type"):
    names = dict(
        zip(
            ("case_category", "case_type", "filing_type"),
            (tuple(words(name)) for name in (category, case_type, filing)),
            strict=True,
        )
    )
    names["court_code"] = court_code
    tokens = set()
    for constraints, aliases in path_alias_rules(jurisdiction, initial, facet):
        if _matches_alias_constraints(jurisdiction, names, constraints):
            tokens.update(aliases)
    return frozenset(tokens)


def indexed_path(jurisdiction, path, initial):
    """Materialize the same scoped aliases for every catalog transport."""
    terms = {facet: search_tokens(path[facet]["name"], jurisdiction) for facet in FACETS}
    for facet in ("filing_type", "case_category", "case_type"):
        terms[facet] = terms[facet] | path_alias_tokens(
            jurisdiction,
            initial,
            *(path[level]["name"] for level in ("case_category", "case_type", "filing_type")),
            court_code=path["court"]["code"],
            facet=facet,
        )
    all_terms = set.union(*terms.values())
    all_terms.update(token for facet in FACETS for token in stems(path[facet]["code"]))
    explanation, source = explanation_for(jurisdiction, path, initial)
    return dict(
        **path,
        initial=initial,
        search_text=packed(all_terms),
        filing_terms=packed(terms["filing_type"]),
        case_terms=packed(terms["case_category"] | terms["case_type"]),
        explanation=explanation,
        explanation_source=source,
    )


def source_url():
    return settings.EFSP_URL.rstrip("/")


class CodeCatalog:
    """Strict API reader: a failed branch must not publish a partial index."""

    def __init__(self, jurisdiction, *, timeout=(10, 60)):
        if jurisdiction not in rules()[0]["jurisdictions"]:
            raise ValueError("Unsupported jurisdiction")
        self.base = f"{source_url()}/jurisdictions/{jurisdiction}/codes/courts/"
        self._local = local()
        self._sessions = []
        self._session_lock = Lock()
        self.timeout = timeout

    @property
    def session(self):
        # Keep cookies and connection pools private to each catalog thread.
        if not hasattr(self._local, "session"):
            session = requests.Session()
            retry = Retry(
                total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"]
            )
            session.mount("https://", HTTPAdapter(max_retries=retry))
            session.mount("http://", HTTPAdapter(max_retries=retry))
            self._local.session = session
            with self._session_lock:
                self._sessions.append(session)
        return self._local.session

    def close(self):
        for session in self._sessions:
            session.close()

    def get(self, suffix="", **params):
        # Adapter retries don't cover failures while Requests consumes a
        # streamed response body. Retry that whole read as well.
        for attempt in range(4):
            response = None
            try:
                response = self.session.get(self.base + suffix, params=params, timeout=self.timeout)
                response.raise_for_status()
                data = response.json()
                break
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 3:
                    raise
                time.sleep(2**attempt)
            finally:
                if response is not None:
                    response.close()
        if not isinstance(data, list):
            raise ValueError("Court code API did not return a list")
        result = {}
        for item in data:
            if not isinstance(item, dict) or item.get("code") is None or not item.get("name"):
                raise ValueError("Court code API returned an invalid option")
            code = str(item["code"])
            if not code:
                raise ValueError("Court code API returned an empty code")
            result[code] = {"code": code, "name": str(item["name"])}
            if "amountincontroversy" in item:
                result[code]["amountincontroversy"] = item["amountincontroversy"]
        return list(result.values())

    def courts(self):
        return [c for c in self.get(fileable_only="false", with_names="true") if not is_non_filing_court(c["name"])]

    def categories(self, court, initial=True):
        return self.get(
            quote(court, safe="") + "/categories",
            fileable_only="true",
            timing="Initial" if initial else "Subsequent",
        )

    def types(self, court, category, initial=True):
        return self.get(
            quote(court, safe="") + "/case_types/",
            category_id=category,
            timing="Initial" if initial else "Subsequent",
        )

    def filings(self, court, category, case_type, initial):
        return self.get(
            quote(court, safe="") + "/filing_types/",
            category_id=category,
            type_id=case_type,
            initial=str(initial).lower(),
        )


def catalog_entries(catalog, jurisdiction, progress, cache_dir=None):
    # Stage courts concurrently so a slow court does not idle the whole build.
    # A shared pool bounds filing-type requests across all four court readers.
    # Per-court files keep memory bounded even when results finish out of order.
    # Every branch still must succeed before publishing the snapshot.
    digest = hashlib.sha256(f"{source_url()}:{rules()[1]}:{jurisdiction}".encode()).hexdigest()
    location = Path(cache_dir) / digest if cache_dir else None
    with nullcontext(location) if location else TemporaryDirectory(prefix="filing-codes-") as directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=8) as filings, ThreadPoolExecutor(max_workers=4) as courts:

            def stage(item):
                _, court = item
                path = Path(directory) / (hashlib.sha256(court["code"].encode()).hexdigest() + ".json.gz")
                if location and path.exists() and time.time() - path.stat().st_mtime < 21600:
                    if progress:
                        progress(f"{court['name']} (resuming completed court)")
                    return path
                if progress:
                    progress(court["name"])
                with NamedTemporaryFile(dir=directory, delete=False) as temporary:
                    temporary_path = Path(temporary.name)
                try:
                    with gzip.open(temporary_path, "wt", encoding="utf-8", compresslevel=1) as output:
                        for entry in _court_entries(catalog, jurisdiction, court, filings):
                            output.write(json.dumps(entry) + "\n")
                    temporary_path.replace(path)
                finally:
                    temporary_path.unlink(missing_ok=True)
                return path

            for path in courts.map(stage, enumerate(catalog.courts())):
                with gzip.open(path, "rt", encoding="utf-8") as snapshot:
                    for line in snapshot:
                        yield json.loads(line)
                if not location:
                    path.unlink()


def _court_entries(catalog, jurisdiction, court, executor):
    for initial in (True, False):
        for category in catalog.categories(court["code"], initial):

            def fetch_filings(case_type, court=court, category=category, initial=initial):
                return case_type, catalog.filings(court["code"], category["code"], case_type["code"], initial)

            # Consume the whole map before advancing these parent facets.
            for case_type, filings in executor.map(
                fetch_filings, catalog.types(court["code"], category["code"], initial)
            ):
                for filing in filings:
                    path = dict(zip(FACETS, (court, category, case_type, filing), strict=True))
                    yield indexed_path(jurisdiction, path, initial)


def refresh_index(jurisdiction, *, progress=None, cache_dir=None, transport=None, force=(), dry_run=False):
    """Synchronize changed court exports; the expensive old crawler requires explicit opt-in."""
    mode = transport or getattr(settings, "FILING_CODE_SYNC_MODE", "bulk")
    if mode == "database":
        from efile.services.filing_code_copy import resync

        return resync(jurisdiction, progress=progress, force=force, dry_run=dry_run)
    if mode == "bulk":
        from efile.services.filing_code_sync import synchronize_index

        return synchronize_index(jurisdiction, progress=progress, force=force, dry_run=dry_run)
    if mode != "legacy":
        raise ValueError("FILING_CODE_SYNC_MODE must be 'database', 'bulk', or 'legacy'")
    if force or dry_run:
        raise ValueError("Forcing courts and dry runs need the bulk sync")
    return _refresh_index_legacy(jurisdiction, progress=progress, cache_dir=cache_dir)


def _refresh_index_legacy(jurisdiction, *, progress=None, cache_dir=None):
    """Replace a jurisdiction atomically after *every* branch was fetched.

    No database transaction is held open during the slow network traversal.
    A later-started refresh cannot be overwritten by an older overlapping run.
    """
    started = timezone.now()
    vocabulary = set()
    indexed_terms = set()
    for concept in rules()[0]["concepts"].values():
        if jurisdiction in concept.get("jurisdictions", rules()[0]["jurisdictions"]):
            vocabulary.update(word for term in concept["terms"] for word in words(term))
    count = 0
    courts = {}
    # Large catalogs share many names but still have many distinct paths. Stage
    # on disk, then insert bounded batches: memory does not grow with row count.
    with closing(CodeCatalog(jurisdiction)) as catalog, TemporaryFile(mode="w+b") as staging:
        with gzip.open(staging, "wt", encoding="utf-8", compresslevel=1) as output:
            for entry in catalog_entries(catalog, jurisdiction, progress, cache_dir):
                output.write(json.dumps(entry) + "\n")
                indexed_terms.update(entry["search_text"].split())
                courts.setdefault(entry["court"]["code"], entry["court"]["name"])
                vocabulary.update(word for facet in FACETS for word in words(entry[facet]["name"]))
                count += 1
        if not count:
            raise ValueError("No filing paths returned; keeping the previous index")
        if progress:
            progress(f"Saving {count} filing paths")
        staging.seek(0)
        with gzip.open(staging, "rt", encoding="utf-8") as snapshot, transaction.atomic():
            index, _ = FilingCodeIndex.objects.get_or_create(
                jurisdiction=jurisdiction,
                defaults={"source_url": source_url(), "refreshed_at": started, "rules_digest": rules()[1]},
            )
            index = FilingCodeIndex.objects.select_for_update().get(pk=index.pk)
            if index.refreshed_at > started:
                return 0
            index.paths.all().delete()
            index.labels.all().delete()
            labels = LabelCounts()
            saved = 0
            while entries := [json.loads(line) for line in islice(snapshot, 500)]:
                batch = []
                for entry in entries:
                    labels.add(entry)
                    batch.append(FilingCodePath(index=index, **entry))
                FilingCodePath.objects.bulk_create(batch, batch_size=500)
                saved += len(batch)
                if progress and saved % 100000 == 0:
                    progress(f"Saved {saved} of {count} filing paths")
            index.source_url = source_url()
            index.refreshed_at = started
            index.rules_digest = rules()[1]
            index.vocabulary = {"words": sorted(vocabulary), "tokens": sorted(indexed_terms), "courts": courts}
            index.court_snapshots = {}
            labels.save(index)
            index.labels_built_at = timezone.now()
            index.save()
    return count


def current_index(jurisdiction):
    # Search needs the vocabulary, but not the much larger import snapshots.
    # Keep snapshots lazy for callers that inspect synchronization metadata.
    return (
        FilingCodeIndex.objects.filter(
            jurisdiction=jurisdiction,
            source_url=source_url(),
            rules_digest=rules()[1],
        )
        .defer("court_snapshots")
        .first()
    )


def case_description(path):
    """Use existing case-specific explanations or normalized case meanings."""
    jurisdiction = path.index.jurisdiction
    for rule in rules()[0]["explanations"]:
        if "case_type" not in rule or jurisdiction not in rule["jurisdictions"]:
            continue
        if "initial" in rule and rule["initial"] != path.initial:
            continue
        if all(
            tuple(words(getattr(path, facet)["name"])) in {tuple(words(name)) for name in rule[facet]}
            for facet in FACETS
            if facet in rule
        ):
            return rule["text"]
    guidance = case_guidance(
        jurisdiction, path.case_category["name"], path.case_type["name"], path.filing_type["name"], path.court["name"]
    )
    if guidance.get("text"):
        return guidance["text"]
    meanings = search_context(jurisdiction, "", [("case_type", path.case_type["name"])], court_name=path.court["name"])[
        "concepts"
    ]
    return next((meaning["text"] for meaning in meanings if meaning.get("text")), "")


JURY_SIZE = re.compile(r"\(\s*jury\s*-\s*(\d+)\s*\)", re.IGNORECASE)
NON_JURY = re.compile(r"\bnon\s*-?\s*jury\b", re.IGNORECASE)
JURY = re.compile(r"\bjury\b", re.IGNORECASE)
SELF_REPRESENTED = re.compile(r"self\s*-?\s*represented|\bSRL\b|\bpro se\b", re.IGNORECASE)
GOVERNMENT = re.compile(r"\(\s*govn'?t\s*\)|\bgovernment(al)? (filer|entity|agency)\b", re.IGNORECASE)


def path_qualifiers(category, case_type, filing_name):
    """Choices some courts code as separate case or filing types, in plain values.

    Cook County, for one, has a separate code for each mix of jury size,
    government filer, self-represented litigant, and claim amount. The court
    step asks only the ones its paths actually differ on.
    """
    case_text = f"{category} {case_type}"
    if size := JURY_SIZE.search(filing_name):
        jury = size.group(1)
    elif NON_JURY.search(case_text) or NON_JURY.search(filing_name):
        jury = "none"
    elif JURY.search(case_text) or JURY.search(filing_name):
        jury = "jury"
    else:
        jury = "none"
    bounds = claim_range(case_type) or claim_range(category)
    amount = None
    if bounds:
        low, high = bounds
        amount = {
            "value": f"{low}:{high if high is not None else ''}",
            "label": f"{_money(low)} to {_money(high)}" if high is not None else f"More than {_money(low - 1)}",
        }
    return {
        "jury": jury,
        "representation": "self" if SELF_REPRESENTED.search(case_text) else "lawyer",
        "government": "yes" if GOVERNMENT.search(filing_name) else "no",
        "amount": amount,
    }


def serialize_path(path, *, query="", corrected=(), resolved_query=None):
    return {
        "qualifiers": path_qualifiers(path.case_category["name"], path.case_type["name"], path.filing_type["name"]),
        "filing_label": unqualified_label(path.filing_type["name"]),
        "glossary": glossary_for(path.index.jurisdiction, [path.filing_type["name"], path.case_type["name"]]),
        "id": path.pk,
        **{facet: getattr(path, facet) for facet in FACETS},
        "initial": path.initial,
        "explanation": path.explanation,
        "explanation_source": path.explanation_source,
        "facets": filing_facets(path.filing_type["name"], path.index.jurisdiction),
        "case_description": case_description(path),
        "case_guidance": case_guidance(
            path.index.jurisdiction,
            path.case_category["name"],
            path.case_type["name"],
            path.filing_type["name"],
            path.court["name"],
        ),
        "case_context": case_context_label(path.case_category["name"], path.case_type["name"]),
        **search_context(
            path.index.jurisdiction,
            query,
            [(facet, getattr(path, facet)["name"]) for facet in ("filing_type", "case_type", "case_category", "court")],
            court_name=path.court["name"],
            corrected=corrected,
            resolved_query=resolved_query,
        ),
        "unavailable": filing_unavailable_message(
            path.index.jurisdiction,
            path.court["code"],
            case_category=path.case_category["name"],
            case_type=path.case_type["name"],
            filing_types=[path.filing_type["name"]],
        ),
    }


def corrected_search_query(index, query):
    vocabulary = set(index.vocabulary["words"])
    indexed_terms = set(index.vocabulary["tokens"])
    corrected = []
    query_words = words(query)
    # Correct before stemming and phrase expansion. A typo in "eviction" can
    # prevent Snowball from recognizing its suffix; the full word must be in
    # the vocabulary even if this state's labels only say "summary process".
    for position, word in enumerate(query_words):
        if len(word) < 5 or not word.isalpha() or word in vocabulary or set(stems(word)) <= indexed_terms:
            continue
        matches = get_close_matches(word, sorted(vocabulary), n=1, cutoff=0.86)
        if matches:
            query_words[position] = matches[0]
            corrected.append(matches[0])
    return " ".join(query_words), corrected


def matching_paths(index, query, initial):
    resolved_query, corrected = corrected_search_query(index, query)
    tokens = search_tokens(resolved_query, index.jurisdiction, query=True, path_query=True)
    exact_name = query.strip()
    exact_fallback = bool(tokens & path_only_concept_tokens(index.jurisdiction))
    literal_tokens = search_tokens(resolved_query, index.jurisdiction, query=True) if exact_fallback else frozenset()
    if not tokens:
        return index.paths.none().alias(court_code=CourtCode("court")).annotate(score=Value(0)), []
    # A court that only heads the locations under it is refused by the e-filing
    # service, even when an older index imported filing codes for it.
    # Filter courts through court_code: court__code compiles to a jsonb
    # comparison that the (index, court code) index cannot serve, and Postgres
    # then reads every path that matches the words, in every jurisdiction.
    paths = (
        index.paths.filter(initial=initial)
        .alias(court_code=CourtCode("court"))
        .exclude(court_code__in=heading_court_codes(index.jurisdiction))
    )
    if connection.vendor == "postgresql":
        search = SearchQuery(" ".join(sorted(tokens)), config="simple")
        paths = paths.annotate(document=SearchVector("search_text", config="simple"))
        if exact_fallback and literal_tokens:
            literal_search = SearchQuery(" ".join(sorted(literal_tokens)), config="simple")
            paths = paths.annotate(literal_document=SearchVector("search_text", config="simple")).filter(
                Q(document=search) | (Q(literal_document=literal_search) & Q(filing_type__name__iexact=exact_name))
            )
        else:
            paths = paths.filter(document=search)
    elif connection.vendor == "sqlite":
        # Look up candidate row IDs before ranking/grouping. LIKE '% token %'
        # scanned every path in the jurisdiction (millions for Illinois).
        # Quote each normalized token and bind the MATCH expression as data;
        # user punctuation must never become FTS operators or SQL syntax.
        terms = " AND ".join('"' + token.replace('"', '""') + '"' for token in sorted(tokens))
        expression = f'index_id : "{int(index.pk)}" AND initial : "{int(initial)}" AND search_text : ({terms})'
        alias_candidates = Q(
            pk__in=RawSQL(
                "SELECT rowid FROM filing_code_search_fts WHERE filing_code_search_fts MATCH %s",
                (expression,),
            )
        )
        if exact_fallback and literal_tokens:
            literal_terms = " AND ".join('"' + token.replace('"', '""') + '"' for token in sorted(literal_tokens))
            literal_expression = (
                f'index_id : "{int(index.pk)}" AND initial : "{int(initial)}" AND search_text : ({literal_terms})'
            )
            exact_candidates = Q(
                pk__in=RawSQL(
                    "SELECT rowid FROM filing_code_search_fts WHERE filing_code_search_fts MATCH %s",
                    (literal_expression,),
                )
            ) & Q(filing_type__name__iexact=exact_name)
            paths = paths.filter(alias_candidates | exact_candidates)
        else:
            paths = paths.filter(alias_candidates)
    else:
        token_query = Q()
        for token in sorted(tokens):
            token_query &= Q(search_text__contains=f" {token} ")
        if exact_fallback and literal_tokens:
            literal_query = Q()
            for token in sorted(literal_tokens):
                literal_query &= Q(search_text__contains=f" {token} ")
            paths = paths.filter(token_query | (literal_query & Q(filing_type__name__iexact=exact_name)))
        else:
            paths = paths.filter(token_query)
    scores = {
        "filing_terms": Value(0, output_field=IntegerField()),
        "case_terms": Value(0, output_field=IntegerField()),
    }
    for token in sorted(tokens):
        for field, weight in (("filing_terms", 8), ("case_terms", 3)):
            clause = Q(**{f"{field}__contains": f" {token} "})
            scores[field] += Case(When(clause, then=Value(weight)), default=Value(0), output_field=IntegerField())
    if exact_fallback:
        # An exact label is a full name match, on the same scale as a
        # synonym. A large bonus would hide valid aliases in strong-only UI.
        scores["filing_terms"] = Case(
            When(filing_type__name__iexact=exact_name, then=Value(8 * len(tokens))),
            default=scores["filing_terms"],
            output_field=IntegerField(),
        )
    paths = paths.annotate(name_score=scores["filing_terms"])
    return paths.annotate(score=scores["filing_terms"] + scores["case_terms"]), corrected


def court_terms(index):
    """Each court's search words, as its paths' search text holds them."""
    key = (index.pk, index.refreshed_at)
    if key not in _court_terms:
        # One entry per jurisdiction's current index; older refreshes drop out.
        for old in [old for old in _court_terms if old[0] == index.pk]:
            del _court_terms[old]
        _court_terms[key] = {
            code: frozenset(search_tokens(name, index.jurisdiction)) | frozenset(stems(code))
            for code, name in index_courts(index).items()
        }
    return _court_terms[key]


_court_terms = {}


def matching_labels(index, query, initial, courts=(), *, names=None, contexts=None):
    """The labels of the paths matching_paths finds, each with its matching courts' path counts.

    A path matches when every search word is in its label or its court's
    name, so the courts a label counts depend on the words left for the
    label to match. Most searches name no court: one query for every word.
    """
    resolved_query, corrected = corrected_search_query(index, query)
    tokens = search_tokens(resolved_query, index.jurisdiction, query=True, path_query=True)
    exact_name = query.strip()
    exact_fallback = bool(tokens & path_only_concept_tokens(index.jurisdiction))
    literal_tokens = search_tokens(resolved_query, index.jurisdiction, query=True) if exact_fallback else frozenset()
    if not tokens:
        return [], tokens, corrected
    headings = heading_court_codes(index.jurisdiction)
    required = {
        code: frozenset(tokens) - words_in_name
        for code, words_in_name in court_terms(index).items()
        if code not in headings and (not courts or code in courts)
    }
    found = {}
    for words_left in set(required.values()):
        labels = FilingCodeLabel.objects.filter(index=index, initial=initial)
        if names is not None:
            labels = labels.filter(filing_type_name__in=names)
        if contexts is not None:
            labels = labels.filter(context_filter(contexts, ("case_category_name", "case_type_name")))
        if not words_left:
            # Only court words: every label of those courts.
            court_match = Q(courts__has_any_keys=[code for code, left in required.items() if not left])
            if exact_fallback and literal_tokens:
                exact_text = Q()
                for token in sorted(literal_tokens):
                    exact_text &= Q(search_text__contains=f" {token} ")
                labels = labels.filter(court_match | (exact_text & Q(filing_type_name__iexact=exact_name)))
            else:
                labels = labels.filter(court_match)
        elif connection.vendor == "postgresql":
            search = SearchQuery(" ".join(sorted(words_left)), config="simple")
            labels = labels.alias(document=SearchVector("search_text", config="simple"))
            if exact_fallback and literal_tokens:
                literal_search = SearchQuery(" ".join(sorted(literal_tokens)), config="simple")
                labels = labels.alias(literal_document=SearchVector("search_text", config="simple")).filter(
                    Q(document=search) | (Q(literal_document=literal_search) & Q(filing_type_name__iexact=exact_name))
                )
            else:
                labels = labels.filter(document=search)
        else:
            text_query = Q()
            for token in sorted(words_left):
                text_query &= Q(search_text__contains=f" {token} ")
            if exact_fallback and literal_tokens:
                exact_text = Q()
                for token in sorted(literal_tokens):
                    exact_text &= Q(search_text__contains=f" {token} ")
                labels = labels.filter(text_query | (exact_text & Q(filing_type_name__iexact=exact_name)))
            else:
                labels = labels.filter(text_query)
        for label in labels.iterator(chunk_size=2000):
            if label.pk in found:
                continue
            label_words = set(label.search_text.split())
            counts = {
                code: count
                for code, count in label.courts.items()
                if code in required
                and (
                    required[code] <= label_words
                    or (
                        exact_fallback and literal_tokens and label.filing_type_name.casefold() == exact_name.casefold()
                    )
                )
            }
            if counts:
                found[label.pk] = (label, counts)
    return list(found.values()), tokens, corrected


def label_score(terms, tokens, weight):
    return sum(weight for token in tokens if f" {token} " in terms)


def search_paths(
    index,
    query,
    *,
    initial=True,
    offset=0,
    limit=20,
    names=None,
    court=None,
    contexts=None,
    courts=(),
    category=None,
    case_type=None,
    filing_type=None,
    include_total=True,
    include_details=True,
):
    paths, corrected = matching_paths(index, query, initial)
    resolved_query = corrected_search_query(index, query)[0] if corrected else query
    if names is not None:
        paths = paths.filter(filing_type__name__in=names)
    if court:
        paths = paths.filter(court_code=court)
    if courts:
        paths = paths.filter(court_code__in=courts)
    if category:
        paths = paths.filter(case_category__code=category)
    if case_type:
        paths = paths.filter(case_type__code=case_type)
    if filing_type:
        paths = paths.filter(filing_type__code=filing_type)
    if contexts is not None:
        paths = paths.filter(context_filter(contexts))
    total = paths.count() if include_total else None
    paths = paths.order_by("-score", "court__name", "case_type__name", "filing_type__name", "pk")
    return {
        "results": [
            serialize_path(path, query=query, corrected=corrected, resolved_query=resolved_query)
            if include_details
            else {
                **{facet: getattr(path, facet) for facet in FACETS},
                "explanation": path.explanation,
                "explanation_source": path.explanation_source,
                "unavailable": filing_unavailable_message(
                    index.jurisdiction,
                    path.court["code"],
                    case_category=path.case_category["name"],
                    case_type=path.case_type["name"],
                    filing_types=[path.filing_type["name"]],
                ),
            }
            # index.paths already carries its index as a known related object.
            # Joining it repeats megabytes of vocabulary/snapshots for every
            # row, particularly on the court screen's 500-result pages.
            for path in paths[offset : offset + limit]
        ],
        "total": total,
        "corrected_terms": corrected,
    }


def clean_filing_label(name):
    name = re.sub(r"^\s*e[\s-]?filed\s*[-:–—]?\s*", "", name, flags=re.IGNORECASE)
    return re.sub(r"\s+filed[.\s]*$", "", name, flags=re.IGNORECASE).strip(" .-")


# Qualifiers some courts code as separate filings of the same document, such as
# Cook County's "Complaint / Petition - Fraud (Jury - 12) (Govn't) - Fee". The
# filer still picks the exact one in the court step; search shows it once.
FILING_QUALIFIERS = re.compile(
    r"\((?:jury[^)]*|govn'?t|government|no fee|fee)\)|\s+-\s+(?:no\s+)?fee\s*$",
    re.IGNORECASE,
)


def unqualified_label(name):
    previous = None
    name = clean_filing_label(name)
    while previous != name:
        previous, name = name, clean_filing_label(FILING_QUALIFIERS.sub("", name))
    return re.sub(r"\s{2,}", " ", name)


def filing_group_key(name, jurisdiction):
    # Group discovery results using the same curated synonyms and stemming as
    # search. These are related labels, not interchangeable court codes: the
    # filer still chooses and validates an exact court/category/type path.
    label = " ".join(words(unqualified_label(name)))
    aliases = {
        "answer to complaint": "answer",
        "response to complaint": "answer",
        "civil complaint": "complaint",
        "complaint civil": "complaint",
        "motion to the court": "motion",
    }
    label = aliases.get(label, label)
    terms = search_tokens(label, jurisdiction, query=True) or set(words(name))
    parts = words(label)
    # Preserve the document's role: "Motion to dismiss" is not a "Dismissal
    # of motion", even though a bag of search words would make them identical.
    # Allow subject-before-document labels like "Eviction complaint".
    role = parts[0] if parts else ""
    document_roles = {
        "complaint",
        "petition",
        "answer",
        "motion",
        "order",
        "notice",
        "affidavit",
        "dismissal",
        "application",
        "summons",
    }
    if (
        role not in document_roles
        and parts
        and parts[-1] in {"complaint", "petition", "answer", "order", "affidavit", "summons"}
    ):
        role = parts[-1]
    if role not in document_roles:
        role = ""
    return hashlib.sha256((role + ":" + " ".join(sorted(terms))).encode()).hexdigest()[:24]


@lru_cache(maxsize=1)
def facet_rules():
    config = yaml.safe_load(RULES_PATH.with_name("filing_code_facets.yaml").read_bytes())
    if config.get("version") != 1:
        raise ValueError("Unsupported filing facet rules version")
    return [(re.compile(rule["pattern"], re.IGNORECASE), rule) for rule in config["rules"]]


@lru_cache(maxsize=1)
def action_rules():
    config = yaml.safe_load(RULES_PATH.with_name("filing_code_facets.yaml").read_bytes())
    return [(re.compile(rule["pattern"], re.IGNORECASE), rule) for rule in config.get("actions", [])]


@lru_cache(maxsize=65536)
def filing_action(name):
    """What a filing does, in plain words, guessed from its name; "" when unknown."""
    label = " ".join(words(clean_filing_label(name)))
    return next((rule["key"] for pattern, rule in action_rules() if pattern.search(label)), "")


def action_options(groups):
    counts = {}
    for group in groups:
        for action in {filing_action(label["name"]) for label in group["_labels"]} - {""}:
            counts[action] = counts.get(action, 0) + 1
    return [
        {"value": rule["key"], "label": rule["label"], "count": counts[rule["key"]]}
        for _, rule in action_rules()
        if rule["key"] in counts
    ]


def category_options(groups):
    """Case categories (Cook's "Civil", "Probate", ...) the groups are filed under, most used first."""
    counts = {}
    for group in groups:
        for name in {label["category"] for label in group["_labels"]}:
            key = tuple(words(name))
            counts.setdefault(key, [name, 0])[1] += 1
    return [
        {"value": name, "label": name, "count": count}
        for name, count in sorted(counts.values(), key=lambda item: (-item[1], item[0].casefold()))
    ]


def strong_matches(groups):
    """How many leading groups match by filing name rather than only by case type.

    Groups sort by name score first, so these lead. A name match has to be at
    least half as strong as the best one, so a two-word search does not count a
    one-word partial match beside an exact one.
    """
    top = max((group["name_rank"] for group in groups), default=0)
    return sum(1 for group in groups if top and group["name_rank"] >= top / 2)


def filing_facets(name, jurisdiction):
    label = " ".join(words(clean_filing_label(name)))
    result = {"purpose": "unknown", "document": "unknown"}
    for pattern, rule in facet_rules():
        if jurisdiction not in rule.get("jurisdictions", rules()[0]["jurisdictions"]):
            continue
        if pattern.search(label):
            for facet in result:
                if facet in rule:
                    result[facet] = rule[facet]
            return result
    return result


def case_context_label(category, case_type):
    return case_type if words(category) == words(case_type) else f"{category} › {case_type}"


# Parts of a case-type name that only say jury, self-representation, or the
# small-claims band: the court step asks about those, so step 1 counts and
# shows "Personal Injury Complaint" once instead of once per variant.
CASE_QUALIFIERS = re.compile(
    r"\s*-?\s*\b(?:non\s*-?\s*)?jury(?: demand)?\b"
    r"|\s*-?\s*\b(?:self\s*-?\s*represented litigant|srl)\b"
    r"|\s*-?\s*\bsmall claims\b\s*-?\s*\$[\d,]+\s*to\s*\$[\d,]+",
    re.IGNORECASE,
)


def unqualified_case_type(case_type):
    return re.sub(r"\s*-\s*$", "", CASE_QUALIFIERS.sub("", case_type)).strip() or case_type


def display_case_context(category, case_type):
    return case_context_label(category, unqualified_case_type(case_type))


def case_context_key(category, case_type):
    return hashlib.sha256(json.dumps([words(category), words(case_type)]).encode()).hexdigest()[:24]


def context_filter(labels, fields=("case_category__name", "case_type__name")):
    condition = Q(pk__in=[])
    for category, case_type in {(label["category"], label["case_type"]) for label in labels}:
        condition |= Q(**dict(zip(fields, (category, case_type), strict=True)))
    return condition


def case_contexts(labels):
    contexts = {}
    for label in labels:
        category, case_type = label["category"], label["case_type"]
        key = (tuple(words(category)), tuple(words(unqualified_case_type(case_type))))
        context = contexts.setdefault(key, {"label": display_case_context(category, case_type), "count": 0, "rank": 0})
        context["count"] += label["path_count"]
        context["rank"] = max(context["rank"], label["rank"])
    return [
        item["label"]
        for item in sorted(
            contexts.values(), key=lambda item: (-item["rank"], -item["count"], item["label"].casefold())
        )
    ]


def filter_filing_groups(
    groups,
    jurisdiction,
    purpose="",
    document="",
    role="",
    property_kind="",
    relief="",
    case_topic="",
    case_filters=None,
    action="",
    category="",
):
    selected = []
    for group in groups:
        labels = []
        for label in group["_labels"]:
            guidance = case_guidance(jurisdiction, label["category"], label["case_type"], label["name"])
            if not matches_case_filters(
                guidance, role, property_kind, relief, topic=case_topic, filters=case_filters
            ) or not matches_amount(label, (case_filters or {}).get("amount", "")):
                continue
            facets = filing_facets(label["name"], jurisdiction)
            if action and filing_action(label["name"]) != action:
                continue
            if category and words(label["category"]) != words(category):
                continue
            if (not purpose or facets["purpose"] == purpose) and (not document or facets["document"] == document):
                labels.append(label)
        if not labels:
            continue
        names = sorted(
            {label["name"] for label in labels}, key=lambda name: (len(name), name.isupper(), name.casefold())
        )
        distinct = {}
        for name in names:
            distinct.setdefault(tuple(words(unqualified_label(name))), unqualified_label(name))
        contexts = case_contexts(labels)
        selected.append(
            {
                **group,
                "_labels": labels,
                "_names": names,
                "variants": list(distinct.values()),
                "name": unqualified_label(names[0]),
                "path_count": sum(label["path_count"] for label in labels),
                "rank": max(label["rank"] for label in labels),
                "name_rank": max(label["name_rank"] for label in labels),
                "case_contexts": contexts[:8],
                "case_context_count": len(contexts),
                "case_context_options": list(
                    {
                        case_context_key(label["category"], label["case_type"]): {
                            "key": case_context_key(label["category"], label["case_type"]),
                            "label": display_case_context(label["category"], label["case_type"]),
                        }
                        for label in labels
                        if display_case_context(label["category"], label["case_type"]) in contexts[:8]
                    }.values()
                ),
                "facets": {
                    facet: values.pop()
                    if len(values := {filing_facets(name, jurisdiction)[facet] for name in names}) == 1
                    else "unknown"
                    for facet in ("purpose", "document")
                },
            }
        )
    return sorted(
        selected,
        key=lambda group: (
            -group["name_rank"],
            -group["rank"],
            -group["path_count"],
            len(search_tokens(group["name"], jurisdiction, query=True)),
            group["name"].casefold(),
        ),
    )


def filing_groups(index, query, initial, courts=()):
    headings = sorted(heading_court_codes(index.jurisdiction))
    fingerprint = hashlib.sha256(
        f"{index.pk}:{index.refreshed_at}:{initial}:{query}:{courts}:{headings}".encode()
    ).hexdigest()
    key = f"filing-groups-v12:{fingerprint}:{bool(index.labels_built_at)}"
    # The court and case-type steps repeat the search that listed the groups,
    # often in another worker or machine: keep results where all of them read.
    cached = cache.get(key)
    if cached is None:
        cached = caches["shared"].get(key)
        if cached is not None:
            cache.set(key, cached, timeout=300)
    if cached is not None:
        return cached
    if index.labels_built_at:
        labels, corrected = _label_rows(index, query, initial, courts)
    else:
        paths, corrected = matching_paths(index, query, initial)
        if courts:
            paths = paths.filter(court_code__in=courts)
        labels = (
            paths.order_by()
            .values("filing_type__name", "case_category__name", "case_type__name")
            .annotate(path_count=Count("pk"), rank=Max("score"), name_rank=Max("name_score"))
        )
    # Ties between case contexts keep this order, which neither query fixes.
    labels = sorted(
        labels,
        key=lambda label: tuple(
            str(label[field]) for field in ("filing_type__name", "case_category__name", "case_type__name")
        ),
    )
    groups = {}
    for label in labels:
        # SQLite's JSON key lookup decodes numeric-looking names, such as Illinois' "97" case type.
        name = str(label["filing_type__name"])
        group_key = filing_group_key(name, index.jurisdiction)
        group = groups.setdefault(
            group_key, {"key": group_key, "variants": [], "path_count": 0, "rank": 0, "name_rank": 0, "_labels": []}
        )
        group["_labels"].append(
            {
                "name": name,
                "path_count": label["path_count"],
                "rank": label["rank"],
                "name_rank": label["name_rank"],
                "category": str(label["case_category__name"]),
                "case_type": str(label["case_type__name"]),
            }
        )
        group["variants"].append(name)
        group["path_count"] += label["path_count"]
        group["rank"] = max(group["rank"], label["rank"])
        group["name_rank"] = max(group["name_rank"], label["name_rank"])
    for group in groups.values():
        group["variants"] = sorted(
            set(group["variants"]), key=lambda name: (len(name), name.isupper(), name.casefold())
        )
        group["_names"] = group["variants"][:]
        distinct = {}
        for name in group["variants"]:
            distinct.setdefault(tuple(words(unqualified_label(name))), unqualified_label(name))
        group["variants"] = list(distinct.values())
        group["name"] = group["variants"][0]
    result = (
        sorted(
            groups.values(),
            key=lambda group: (
                -group["name_rank"],
                -group["rank"],
                -group["path_count"],
                len(search_tokens(group["name"], index.jurisdiction, query=True)),
                group["name"].casefold(),
            ),
        ),
        corrected,
    )
    cache.set(key, result, timeout=300)
    caches["shared"].set(key, result, timeout=300)
    return result


def _label_rows(index, query, initial, courts):
    """filing_groups' per-label rows, summed over the matching courts' paths."""
    labels, tokens, corrected = matching_labels(index, query, initial, courts)
    exact_fallback = bool(tokens & path_only_concept_tokens(index.jurisdiction))
    exact_name = query.strip().casefold()
    rows = []
    for label, counts in labels:
        name_rank = (
            8 * len(tokens)
            if exact_fallback and label.filing_type_name.casefold() == exact_name
            else label_score(label.filing_terms, tokens, 8)
        )
        rows.append(
            {
                "filing_type__name": label.filing_type_name,
                "case_category__name": label.case_category_name,
                "case_type__name": label.case_type_name,
                "path_count": sum(counts.values()),
                "rank": name_rank + label_score(label.case_terms, tokens, 3),
                "name_rank": name_rank,
            }
        )
    return rows, corrected


def count_groups(index, query, initial, courts, *, purpose, document, topic, case_filters, action, category):
    """How many filing types the same search and answers find in the other case stage."""
    groups = filing_groups(index, query, initial, courts)[0]
    groups = filter_filing_groups(groups, index.jurisdiction, purpose, document)
    groups = filter_filing_groups(groups, index.jurisdiction, case_topic=topic, case_filters=case_filters)
    if action:
        groups = filter_filing_groups(groups, index.jurisdiction, action=action)
    if category:
        groups = filter_filing_groups(groups, index.jurisdiction, category=category)
    return len(groups)


def search_grouped_paths(
    index,
    query,
    *,
    initial=True,
    offset=0,
    limit=20,
    group_key="",
    court="",
    purpose="",
    document="",
    context="",
    postal_code="",
    role="",
    property_kind="",
    relief="",
    case_topic="",
    case_filters=None,
    action="",
    category="",
    strong_only=False,
):
    location = locate(index, postal_code) or {"counties": [], "courts": []}
    counties, courts = location["counties"], tuple(location["courts"])
    groups, corrected = filing_groups(index, query, initial, courts)
    resolved_query = corrected_search_query(index, query)[0] if corrected else query
    case_filters = case_filters or {}
    if any((role, property_kind, relief)):
        case_topic = "eviction"
        case_filters = {
            key: value for key, value in {"role": role, "property": property_kind, "relief": relief}.items() if value
        }
    validate_case_filters(index.jurisdiction, case_topic, case_filters)
    groups = filter_filing_groups(groups, index.jurisdiction, purpose, document)
    metadata = facet_metadata(
        index.jurisdiction, query, [label for group in groups for label in group["_labels"]], case_topic, case_filters
    )
    selected_topic = "" if metadata["case_topic"] == "all" else metadata["case_topic"]
    groups = filter_filing_groups(groups, index.jurisdiction, case_topic=selected_topic, case_filters=case_filters)
    topic_counts = {}
    for group in filing_groups(index, query, initial, courts)[0] if metadata["case_topics"] else []:
        for topic in {
            topic_of(index.jurisdiction, label["category"], label["case_type"]) for label in group["_labels"]
        }:
            topic_counts[topic] = topic_counts.get(topic, 0) + 1
    for item in metadata["case_topics"]:
        item["count"] = topic_counts.get(item["value"], 0)
    metadata["case_topics"].sort(key=lambda item: (-item["count"], item["label"].casefold()))
    actions = action_options(groups)
    if action:
        groups = filter_filing_groups(groups, index.jurisdiction, action=action)
    categories = category_options(groups)
    if category:
        groups = filter_filing_groups(groups, index.jurisdiction, category=category)
    if not group_key:
        strong = strong_matches(groups)
        shown = groups[:strong] if strong_only and strong else groups
        # Sealing an eviction, say, is filed in the existing case: a filer
        # searching new-case filings is told the other list has matches.
        other_stage_total = (
            count_groups(
                index,
                query,
                not initial,
                courts,
                purpose=purpose,
                document=document,
                topic=selected_topic,
                case_filters=case_filters,
                action=action,
                category=category,
            )
            if not offset
            else None
        )
        return {
            "groups": [
                {
                    **{key: value for key, value in group.items() if not key.startswith("_")},
                    # Only the names shown: filing and case-type names without the
                    # qualifiers step 1 drops, and not categories (Cook files every
                    # Law-division injury under "Personal Injury/Wrongful Death").
                    "glossary": glossary_for(
                        index.jurisdiction,
                        [
                            group["name"],
                            *sorted({unqualified_case_type(label["case_type"]) for label in group["_labels"]}),
                        ],
                    ),
                    **search_context(
                        index.jurisdiction,
                        query,
                        [("filing_type", name) for name in [group["name"], *group["variants"]]],
                        corrected=corrected,
                        resolved_query=resolved_query,
                    ),
                }
                for group in shown[offset : offset + limit]
            ],
            **metadata,
            "actions": actions,
            "categories": categories,
            "strong_total": strong,
            "other_stage_total": other_stage_total,
            "eviction_facets": selected_topic == "eviction",
            "location_counties": counties,
            "total": len(groups),
            "corrected_terms": corrected,
        }
    group = next((group for group in groups if group["key"] == group_key), None)
    if group is None:
        raise ValueError("These results have changed. Search again.")
    contexts = group["_labels"] if selected_topic or case_filters else None
    if context:
        contexts = [
            label for label in group["_labels"] if case_context_key(label["category"], label["case_type"]) == context
        ]
        if not contexts:
            raise ValueError("This case type is no longer in these results. Search again.")
    if court:
        return search_paths(
            index,
            query,
            initial=initial,
            offset=offset,
            limit=limit,
            names=group["_names"],
            court=court,
            contexts=contexts,
            courts=courts,
        )
    if index.labels_built_at:
        return {
            "courts": _label_courts(index, query, initial, courts, group["_names"], contexts),
            "corrected_terms": corrected,
        }
    paths, _ = matching_paths(index, query, initial)
    if courts:
        paths = paths.filter(court_code__in=courts)
    if contexts is not None:
        paths = paths.filter(context_filter(contexts))
    courts = (
        paths.filter(filing_type__name__in=group["_names"])
        .order_by("court__name")
        .values("court__code", "court__name")
        .distinct()
    )
    return {
        "courts": [{"code": str(row["court__code"]), "name": str(row["court__name"])} for row in courts],
        "corrected_terms": corrected,
    }


def _label_courts(index, query, initial, courts, names, contexts):
    labels = matching_labels(index, query, initial, courts, names=names, contexts=contexts)[0]
    codes = {code for _, counts in labels for code in counts}
    court_names = index_courts(index)
    return sorted(
        ({"code": code, "name": court_names.get(code, code)} for code in codes),
        key=lambda court: (court["name"], court["code"]),
    )


def validate_path(path):
    """Re-fetch each parent-child relationship; names come from today's API."""
    catalog = CodeCatalog(path.index.jurisdiction, timeout=(5, 8))

    def find(options, facet):
        chosen = next((o for o in options if o["code"] == getattr(path, facet)["code"]), None)
        if chosen is None:
            raise ValueError("This filing path is no longer offered. Search again or use the court lists.")
        return chosen

    try:
        headings = heading_court_codes(path.index.jurisdiction)
        options = {"court": [court for court in catalog.courts() if court["code"] not in headings]}
        court = find(options["court"], "court")
        options["case_category"] = catalog.categories(court["code"], path.initial)
        category = find(options["case_category"], "case_category")
        options["case_type"] = catalog.types(court["code"], category["code"], path.initial)
        case_type = find(options["case_type"], "case_type")
        options["filing_type"] = catalog.filings(court["code"], category["code"], case_type["code"], path.initial)
        filing = find(options["filing_type"], "filing_type")
    finally:
        catalog.close()
    for facet, option in zip(FACETS, (court, category, case_type, filing), strict=True):
        setattr(path, facet, option)
    path.explanation, path.explanation_source = explanation_for(
        path.index.jurisdiction,
        {facet: getattr(path, facet) for facet in FACETS},
        path.initial,
    )
    result = serialize_path(path)
    result["options"] = options
    if result["unavailable"]:
        raise ValueError(result["unavailable"])
    return result
