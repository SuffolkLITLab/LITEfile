"""Build jurisdiction-selected, server-owned proxy notification copy."""

import re
from string import Template

from django.core.exceptions import ImproperlyConfigured
from django.urls import reverse

from efile.services.fee_quotes import WAIVER_ACCOUNT_TYPE
from efile.services.filings import court_contact
from efile.services.waiver_documents import has_waiver_document

PROXY_EMAIL_FIELDS_REQUIRED = (
    "email_confirmation_subject",
    "email_confirmation_contents",
    "acceptance_subject",
    "acceptance_contents",
    "rejected_subject",
    "rejected_contents",
    "neutral_subject",
    "neutral_contents",
)

PROXY_EMAIL_FIELDS = PROXY_EMAIL_FIELDS_REQUIRED + ("from_email",)


def strip_client_email_fields(efile_data):
    """Remove every supported notification field supplied by the browser."""
    for field in PROXY_EMAIL_FIELDS:
        efile_data.pop(field, None)


def submission_email_fields(request, draft, jurisdiction, config=None):
    """Build jurisdiction-owned copy, optionally with an HTML progress tracker."""
    config = config or {}
    notification_config = config.get("submission_notifications", {})
    if not isinstance(notification_config, dict) or notification_config.get("enabled") is not True:
        return {}

    templates = notification_config.get("templates") or {}
    if not isinstance(templates, dict) or not all(
        isinstance(templates.get(field), str) and templates[field].strip() for field in PROXY_EMAIL_FIELDS_REQUIRED
    ):
        return {}

    tracker_url = request.build_absolute_uri(reverse("submission_tracker", kwargs={"public_id": draft.public_id}))
    contact = court_contact(jurisdiction, draft.court_code, config)
    waiver_url = (config.get("fee_waiver") or {}).get("source_url")
    values = {
        "tracker_url": _template_value(tracker_url),
        "help_line": _help_line(contact),
        "waiver_line": _waiver_line(draft, waiver_url),
    }
    values.update(_conditional_text(notification_config, draft))
    fields = {
        field: Template(templates[field]).safe_substitute(values).strip() for field in PROXY_EMAIL_FIELDS_REQUIRED
    }
    if "from_email" in templates:
        fields["from_email"] = Template(templates["from_email"]).safe_substitute(values).strip()
    if notification_config.get("format") == "html":
        from efile.services.filing_timing import court_filing_timing
        from efile.services.notification_email_html import render_notification_email

        timing = court_filing_timing(jurisdiction, draft.court_code, config)
        # Timing text also crosses the proxy's second template-rendering pass.
        timing = {
            key: {name: _template_value(value) for name, value in item.items()} if isinstance(item, dict) else item
            for key, item in timing.items()
        }
        for field in PROXY_EMAIL_FIELDS:
            if field.endswith("_contents"):
                fields[field] = render_notification_email(field, fields[field], _template_value(tracker_url), timing)
    return fields


def _template_value(value):
    """Keep interpolated data from becoming a proxy-side Mustache expression."""
    return str(value).replace("{", "%7B").replace("}", "%7D")


def _help_line(contact):
    details = []
    if contact.get("name"):
        details.append(_template_value(contact["name"]))
    if contact.get("url"):
        details.append("Website: " + _template_value(contact["url"]))
    if contact.get("phone"):
        details.append("Phone: " + _template_value(contact["phone"]))
    if contact.get("email"):
        details.append("Email: " + _template_value(contact["email"]))
    return "\nFor help, " + "; ".join(details) if details else ""


def _waiver_line(draft, waiver_url):
    if not _fee_waiver_requested(draft):
        return ""
    line = "\nIf you requested a fee waiver, check the court’s decision for any next steps."
    if waiver_url:
        line += " More information: " + _template_value(waiver_url)
    return line


def _fee_waiver_requested(draft):
    """Whether the filing asks for a waiver, by payment account or waiver document."""
    return draft.selected_payment_account_type == WAIVER_ACCOUNT_TYPE or has_waiver_document(draft)


def _conditional_text(notification_config, draft):
    """Evaluate only server-defined submission facts, never partner-authored code."""
    blocks = notification_config.get("conditional_text", {})
    if not isinstance(blocks, dict):
        raise ImproperlyConfigured("submission_notifications.conditional_text must be a mapping")
    values = {}
    for name, block in blocks.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_]*", name)
            or name in {"tracker_url", "help_line", "waiver_line"}
            or not isinstance(block, dict)
            or set(block) - {"when", "then", "otherwise"}
            or block.get("when") != "fee_waiver_requested"
            or not isinstance(block.get("then"), str)
            or not isinstance(block.get("otherwise", ""), str)
        ):
            raise ImproperlyConfigured(f"Invalid notification conditional text: {name}")
        text = block["then"] if _fee_waiver_requested(draft) else block.get("otherwise", "")
        # Treat the selected copy as literal data across the proxy rendering pass.
        values[name] = "\n" + _template_value(text.strip()) if text.strip() else ""
    return values
