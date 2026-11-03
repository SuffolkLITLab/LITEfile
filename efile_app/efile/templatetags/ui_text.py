"""``{% ui_text %}`` -- jurisdiction-configurable, translatable copy in templates.

    {% load ui_text %}
    <legend>{% ui_text "organize_documents.main_document_question" %}</legend>
    <p>{% ui_text "about.project_partner_description" %}</p>

The jurisdiction and its config come from the template context, which the
``jurisdiction_context`` context processor puts there on every page. Extra
keyword arguments become placeholder values:

    {% ui_text "parties.role_help" name=filer.first_name %}

A passage marked ``links=True`` in the catalog renders its inline Markdown as
sanitized HTML; every other key is plain, escaped text.

Other configured copy that may carry inline links, such as fee waiver guidance,
goes through the ``inline_markdown`` filter:

    <p>{{ fee_waiver.document_guidance|inline_markdown }}</p>

See ``efile/utils/ui_text.py`` for the catalog of keys.
"""

from django import template

from efile.utils.ui_text import get_html, render_inline_markdown

register = template.Library()


@register.simple_tag(takes_context=True)
def ui_text(context, key, **params):
    return get_html(
        key,
        jurisdiction=context.get("jurisdiction"),
        config=context.get("config"),
        **params,
    )


@register.filter
def inline_markdown(value):
    """Render a state's configured copy as inline HTML with safe links only."""
    return render_inline_markdown(str(value or ""))
