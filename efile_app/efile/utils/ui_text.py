"""User-facing text that a jurisdiction can reword and a translator can translate.

Two separate problems meet in this module.

The first is that states do not use the same words for the same thing. The
document that opens a case is a *petition* in Illinois and a *complaint* in
Vermont, and a filer who is told to look for the wrong one is being given wrong
instructions, not a stylistic variation. Those differences belong in the state's
own YAML file, next to everything else that is specific to that state.

The second is Spanish. Almost every string on these screens is ordinary English
copy that no state wants to change, and moving all of it into YAML to make the
handful of state-varying strings configurable would put thousands of strings
somewhere ``makemessages`` cannot see them. So the split is:

* Copy that does not vary by state stays in the template under ``{% translate %}``.
* Copy that a state might reword lives here, with its English default, and can
  be overridden by a ``text:`` section in that state's YAML.

Both halves end up in the same gettext catalog. A string resolved through this
module is passed through :func:`pgettext` with the key as its message context,
so the Illinois wording and the Vermont wording of the same key are two
translatable messages that a translator can tell apart. Defaults are picked up
from this file by ``makemessages``; the YAML overrides are picked up from the
stub that ``manage.py extract_config_text`` generates. See ``efile/locale/README.md``.

Usage from a template::

    {% load ui_text %}
    <p>{% ui_text "organize_documents.main_document_help" %}</p>

and from Python::

    from efile.utils.ui_text import get_text
    get_text("your_information.lede", jurisdiction="vermont")

Placeholders are filled in from the jurisdiction's config (``brand_name``,
``state_name``, ``state_code``, ``court_name``), from every ``terms.*`` key
(``terms.starting_document_example`` fills ``{starting_document_example}``), and
from any keyword arguments passed by the caller. An unknown placeholder is left
in the text rather than raising, because these strings can come from a YAML file
that no test has rendered.

A passage marked ``links=True`` may carry inline Markdown -- ``[text](url)``,
``*emphasis*``, ``**strong**`` -- so a state can point filers at its own rules or
help pages. ``{% ui_text %}`` renders it as sanitized HTML. Everywhere else (a
label, a question, a string handed to JavaScript) the Markdown is reduced to its
plain text, because a link inside a ``<label>`` or ``<legend>``, or an attribute,
is a control no one can use.
"""

import html
import logging
import re
from dataclasses import dataclass
from string import Formatter

import bleach
import markdown
from django.utils.safestring import SafeString
from django.utils.translation import gettext, pgettext

logger = logging.getLogger(__name__)

#: Config section, in a state's YAML file, that holds overrides for these keys.
CONFIG_SECTION = "text"

#: Keys under this prefix are short nouns rather than sentences. Each one is
#: also offered to every other string as a ``{placeholder}`` named for its last
#: path segment, so a passage can say "like a {starting_document_example}".
TERM_PREFIX = "terms."


@dataclass(frozen=True)
class UIString:
    """One overridable, translatable string.

    Args:
        default: The English wording used when a state configures nothing.
        description: Why this string is configurable, and what a state should
            consider when rewording it. Shown to config authors in the docs and
            to translators as a comment.
        links: Whether the string is a passage that may carry inline Markdown
            links. Only set on text a template renders as running prose, never
            on a label, a question, or a string handed to JavaScript.
    """

    default: str
    description: str = ""
    links: bool = False


UI_STRINGS: dict[str, UIString] = {
    "upload_documents.preparation_help": UIString(
        default="Upload your files as they are. We make PDF copies for court.",
        description="Preparation guidance when flatten_pdf_forms is enabled. May link to state-specific requirements.",
        links=True,
    ),
    "upload_documents.preparation_help_unflattened": UIString(
        default="We turn Word files into PDFs. You can check them next.",
        description="Preparation guidance when flatten_pdf_forms is disabled.",
        links=True,
    ),
    # -- Terms ---------------------------------------------------------------
    # Short nouns. Keep them lowercase and in the middle of a sentence: they are
    # interpolated into the passages below, which capitalize for themselves.
    "terms.starting_document_example": UIString(
        default="petition",
        description=(
            "What courts here usually call the document that opens a case, used as an example "
            "when asking the filer which PDF is the main one. Vermont says complaint."
        ),
    ),
    "terms.main_document": UIString(
        default="main document",
        description=(
            "The one PDF a filing is built around. Deliberately not the court's own "
            "'lead document', which is a per-document filing component and can apply to more "
            "than one PDF in the same filing."
        ),
    ),
    # -- Organize documents --------------------------------------------------
    "organize_documents.main_document_question": UIString(
        default="Which PDF is the {main_document}?",
    ),
    "organize_documents.main_document_help": UIString(
        default="Choose the document that starts this filing, like a {starting_document_example}.",
        description="Names the kind of document that opens a case, which differs by state.",
        links=True,
    ),
    "organize_documents.filing_component_label": UIString(
        default="Court filing component",
        description=(
            "Label for the court's own per-document category. This is the court's term, not "
            "ours, so a state whose court calls it something else should say so here."
        ),
    ),
    "organize_documents.filing_component_help": UIString(
        default="The court may call more than one PDF a “Lead Document”. This is separate from your {main_document}.",
        description=(
            "Explains why several PDFs can each say “Lead Document” here. Reword it if the "
            "court in this state names its components differently."
        ),
        links=True,
    ),
    "organize_documents.filing_component_fixed_note": UIString(
        default="Required by the court.",
    ),
    "organize_documents.filing_component_required_note": UIString(
        default="Required by the court for this filing type.",
    ),
    "organize_documents.confidentiality_help": UIString(
        default="Choose confidential only when a court rule or court order requires it.",
        description=(
            "Explains when filers in this jurisdiction may mark a document confidential. "
            "A state should use the rule or order language its court approves."
        ),
        links=True,
    ),
    "organize_documents.optional_services_summary": UIString(
        default="Certified copy and courtesy email (optional)",
    ),
    "organize_documents.optional_services_help": UIString(
        default="A certified copy is a copy the court marks as official.",
        description="Explains the unfamiliar term certified copy; the section heading marks these services optional.",
        links=True,
    ),
    # Placeholders the organize-documents page renders from JavaScript once the
    # court's own choices have loaded. They are resolved here, and handed to the
    # script through the page, so they stay configurable and translatable.
    "organize_documents.choose_filing_type_first": UIString(
        default="Select a filing type first",
    ),
    "organize_documents.loading_choices": UIString(
        default="Loading choices…",
    ),
    "organize_documents.no_document_types": UIString(
        default="No confidentiality choices are available",
    ),
    "organize_documents.no_filing_components": UIString(
        default="No filing components are available",
    ),
    # -- Extraction review --------------------------------------------------
    "extraction_review.court_label": UIString(
        default="Court or county",
        description="The jurisdiction's plain-language name for the court or venue shown on its forms.",
    ),
    "extraction_review.court_help": UIString(
        default="Choose the court that should receive this filing.",
        links=True,
    ),
    "extraction_review.case_category_label": UIString(
        default="Case category",
        description="The jurisdiction's term for the broad category or division of a court case.",
    ),
    "extraction_review.case_category_help": UIString(
        default="Choose the broad category that best describes the case.",
        description="May include familiar examples that help filers recognize the court's formal categories.",
        links=True,
    ),
    # -- Your information ----------------------------------------------------
    "your_information.lede": UIString(
        default="Check your name and contact information.",
        description="Reminder to check the contact information used for this filing.",
        links=True,
    ),
    # -- People / parties ----------------------------------------------------
    "parties.role_question": UIString(
        default="What is your role in this case?",
    ),
    "parties.role_help": UIString(
        default="Look for your role next to your name on the court documents.",
        links=True,
    ),
    # -- Public pages --------------------------------------------------------
    "about.project_partner_description": UIString(
        # No {state_name} in the default on purpose: a state that has partners
        # to name is a state that should be naming them in its own words.
        default=(
            "LITEfile is built with legal aid partners who help fund, build, and deploy it for "
            "self-represented litigants in their states:"
        ),
        description=(
            "The partner paragraph on this jurisdiction's About page. Partners approve their own "
            "wording, so set it per state."
        ),
        links=True,
    ),
}


class _LenientParams(dict):
    """Leave an unknown ``{placeholder}`` alone instead of raising KeyError."""

    def __missing__(self, key):
        logger.warning("ui_text: no value for placeholder {%s}", key)
        return "{" + key + "}"


def _flatten(section, prefix=""):
    """Turn a nested ``text:`` config section into ``{"a.b": "value"}``."""
    flat = {}
    if not isinstance(section, dict):
        return flat
    for key, value in section.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten(value, prefix=f"{path}."))
        elif isinstance(value, str):
            flat[path] = value
    return flat


def config_overrides(config):
    """Return the flattened ``text:`` overrides in a jurisdiction config."""
    return _flatten((config or {}).get(CONFIG_SECTION) or {})


def _load_config(jurisdiction, config):
    if config is not None:
        return config
    if not jurisdiction:
        return {}
    # Imported here: config_loader instantiates itself at import time and reads
    # settings, and ui_text is imported from templatetags and system checks.
    from efile.utils.config_loader import config_loader

    return config_loader.load_jurisdiction_config(jurisdiction) or {}


def _source(key, overrides):
    entry = UI_STRINGS.get(key)
    if entry is None:
        raise KeyError(
            f"Unknown UI text key {key!r}. Add it to efile.utils.ui_text.UI_STRINGS "
            f"before using it in a template or a state's text: config."
        )
    return overrides.get(key) or entry.default


def _jurisdiction_params(config):
    jurisdiction = (config or {}).get("jurisdiction") or {}
    state = (config or {}).get("state") or {}
    return {
        "brand_name": jurisdiction.get("brand_name") or "LITEfile",
        "court_name": jurisdiction.get("official_court_name") or "the court",
        "jurisdiction_name": jurisdiction.get("display_name") or "",
        "state_code": state.get("code") or "",
        "state_name": state.get("name") or jurisdiction.get("display_name") or "",
    }


def _format(text, params):
    try:
        return Formatter().vformat(text, (), _LenientParams(params))
    except (IndexError, ValueError) as error:
        # A stray brace in configured copy should show the filer the sentence,
        # not a server error.
        logger.warning("ui_text: could not format %r (%s)", text, error)
        return text


# Inline only: these passages sit inside a <p>, <small>, or <summary> that the
# template already provides, so nothing here may open a block of its own.
INLINE_TAGS = frozenset({"a", "em", "strong", "code"})
LINK_PROTOCOLS = frozenset({"http", "https", "mailto", "tel"})

# ``[text](url)`` and ``<url>`` -- enough to reduce a link to its words.
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_AUTOLINK = re.compile(r"<((?:https?|mailto|tel):[^>\s]+)>")
_MARKDOWN_EMPHASIS = re.compile(r"(\*\*|__|\*|`)(.+?)\1")
# Every character Markdown gives a meaning to, so a value filled into a
# placeholder -- a filer's name, a brand name -- is shown as typed.
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!<>|~])")


def has_markdown_link(text):
    """Whether configured copy tries to link somewhere."""
    return bool(_MARKDOWN_LINK.search(text or "") or _MARKDOWN_AUTOLINK.search(text or ""))


def plain_text(text):
    """Configured copy with its inline Markdown reduced to the words."""
    text = _MARKDOWN_LINK.sub(r"\1", text or "")
    text = _MARKDOWN_AUTOLINK.sub(r"\1", text)
    return _MARKDOWN_EMPHASIS.sub(r"\2", text)


def _escape_markdown(value):
    return _MARKDOWN_SPECIAL.sub(r"\\\1", html.escape(str(value), quote=False))


def _open_off_site_links_in_new_tab(attrs, new=False):
    href = attrs.get((None, "href"), "")
    if href.startswith(("http://", "https://")):
        attrs[(None, "target")] = "_blank"
        attrs[(None, "rel")] = "noopener noreferrer"
        # Said out loud, and not only by an icon: a new tab is a surprise
        # to someone who cannot see it open.
        attrs["_text"] = f"{attrs['_text']} ({gettext('opens in a new tab')})"
    return attrs


def render_inline_markdown(text):
    """Render trusted configured copy as inline HTML, allowing only safe links.

    The copy comes from a state's own YAML, but it is still sanitized: only
    inline tags survive, links keep only ``href`` and ``title``, and only web,
    email, and phone links are allowed. A ``javascript:`` link loses its href.
    """
    rendered = markdown.markdown(text or "")
    cleaned = bleach.clean(
        rendered,
        tags=INLINE_TAGS,
        attributes={"a": ["href", "title"]},
        protocols=LINK_PROTOCOLS,
        strip=True,
    ).strip()
    linked = bleach.linkify(cleaned, callbacks=[_open_off_site_links_in_new_tab], parse_email=False)
    # Bleach enforces the allowlists above before this reviewed safe-output boundary.
    return SafeString(linked)  # nosec B703


def get_text(key, jurisdiction=None, config=None, **params):
    """Resolve one UI string for a jurisdiction, translated and formatted.

    Args:
        key: A key in :data:`UI_STRINGS`, e.g. ``"parties.role_question"``.
        jurisdiction: Jurisdiction code, used to load config when ``config`` is
            not given.
        config: An already-loaded jurisdiction config, to avoid reloading it.
        **params: Extra placeholder values, which win over the automatic ones.

    Returns:
        str: The final string to show the filer.
    """
    text, values = _source_and_values(key, jurisdiction, config, params)
    return plain_text(_format(text, values))


def get_html(key, jurisdiction=None, config=None, **params):
    """Like :func:`get_text`, but a passage's inline Markdown becomes HTML.

    Only strings marked ``links=True`` are rendered; any other key comes back
    as its plain text, escaped. Placeholder values are escaped before they go
    in, so a filer's name cannot turn into markup or a link.

    Returns:
        SafeString: Ready to put straight into a template.
    """
    text, values = _source_and_values(key, jurisdiction, config, params)
    entry = UI_STRINGS.get(key)
    if entry is None or not entry.links:
        return SafeString(html.escape(plain_text(_format(text, values))))  # nosec B703
    return render_inline_markdown(_format(text, {name: _escape_markdown(value) for name, value in values.items()}))


def _source_and_values(key, jurisdiction, config, params):
    config = _load_config(jurisdiction, config)
    overrides = config_overrides(config)
    text = pgettext(key, _source(key, overrides))

    values = _jurisdiction_params(config)
    if not key.startswith(TERM_PREFIX):
        # Terms are resolved without other terms in scope, so a term cannot
        # refer to itself or to another term and loop.
        for term_key in UI_STRINGS:
            if term_key.startswith(TERM_PREFIX):
                values[term_key[len(TERM_PREFIX) :]] = plain_text(pgettext(term_key, _source(term_key, overrides)))
    values.update(params)
    return text, values


def get_texts(keys, jurisdiction=None, config=None, **params):
    """Resolve several keys at once, for handing a page's strings to JavaScript.

    Returns:
        dict: ``{last_path_segment: resolved text}`` -- the short name keeps the
        JSON readable on the page and in the script that reads it.
    """
    config = _load_config(jurisdiction, config)
    return {key.rsplit(".", 1)[-1]: get_text(key, config=config, **params) for key in keys}
