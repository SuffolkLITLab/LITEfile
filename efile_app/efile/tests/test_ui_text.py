"""Copy that a state can reword, and the plumbing that keeps it translatable."""

import pytest
from django.core.management import CommandError, call_command
from django.template import Context, Template
from django.urls import reverse

from efile.checks import configured_ui_text_keys_are_known
from efile.models import FilingDocument, FilingDraft
from efile.services.current_drafts import CURRENT_DRAFT_SESSION_KEY
from efile.tests.helpers import reviewed_document
from efile.utils.ui_text import UI_STRINGS, config_overrides, get_html, get_text, get_texts
from efile.workflow import ExistingCase, WorkflowStepKey


def test_a_state_without_an_override_gets_the_default():
    assert get_text("terms.starting_document_example", jurisdiction="illinois") == "petition"


def test_a_state_can_reword_a_term():
    """The document that opens a case is a complaint in Vermont, not a petition."""

    assert get_text("terms.starting_document_example", jurisdiction="vermont") == "complaint"


def test_a_term_reaches_the_sentences_that_use_it():
    """A state rewords the noun once, and every sentence naming it follows."""

    illinois = get_text("organize_documents.main_document_help", jurisdiction="illinois")
    vermont = get_text("organize_documents.main_document_help", jurisdiction="vermont")

    assert "like a petition" in illinois
    assert "like a complaint" in vermont


def test_jurisdiction_values_fill_placeholders():
    lede = get_text(
        "your_information.lede",
        config={
            "jurisdiction": {"brand_name": "LITEfile"},
            "text": {"your_information": {"lede": "Check your {brand_name} account details."}},
        },
    )

    assert "LITEfile account" in lede
    assert "eFile account" not in lede


def test_an_unknown_key_is_a_programming_error():
    with pytest.raises(KeyError, match="Unknown UI text key"):
        get_text("organize_documents.no_such_string", jurisdiction="illinois")


def test_an_unknown_placeholder_survives_rendering():
    """Configured copy is written outside the templates, so a stray placeholder
    has to show the filer a sentence rather than raise a 500."""

    config = {"text": {"parties": {"role_question": "Your role in {not_a_real_value}?"}}}

    assert get_text("parties.role_question", config=config) == "Your role in {not_a_real_value}?"


def test_overrides_are_read_as_dotted_keys():
    config = {"text": {"terms": {"starting_document_example": "complaint"}}}

    assert config_overrides(config) == {"terms.starting_document_example": "complaint"}


def test_get_texts_keys_by_short_name_for_javascript():
    texts = get_texts(["organize_documents.loading_choices"], jurisdiction="illinois")

    assert list(texts) == ["loading_choices"]


def test_the_template_tag_reads_the_jurisdiction_from_the_page():
    template = Template('{% load ui_text %}{% ui_text "organize_documents.main_document_help" %}')

    rendered = template.render(Context({"jurisdiction": "vermont"}))

    assert "like a complaint" in rendered


def test_every_configured_key_is_a_key_we_ship():
    """Guards the state YAML files themselves: a typo there would silently leave
    the filer reading the wording the state asked to change."""

    assert configured_ui_text_keys_are_known(None) == []


def test_the_check_reports_a_key_that_does_not_exist(monkeypatch):
    """Stand in for a state file that misspells a key, or names one we renamed."""

    monkeypatch.delitem(UI_STRINGS, "terms.starting_document_example")

    problems = configured_ui_text_keys_are_known(None)

    assert [problem.id for problem in problems] == ["efile.W002"]


# -- Links in configured copy ------------------------------------------------

RULE_LINK = {
    "text": {
        "organize_documents": {
            "confidentiality_help": (
                "Only when [Rule 138](https://courts.example.gov/rule-138) or a court order requires it."
            ),
        }
    }
}


def test_a_passage_can_link_to_a_states_own_rules():
    rendered = get_html("organize_documents.confidentiality_help", config=RULE_LINK)

    assert '<a href="https://courts.example.gov/rule-138"' in rendered
    assert 'target="_blank"' in rendered
    assert 'rel="noopener noreferrer"' in rendered
    # Said, not only shown: a new tab opening is a surprise to a screen reader user.
    assert "Rule 138 (opens in a new tab)</a>" in rendered
    # Inline: the template already wraps it in its own paragraph.
    assert "<p>" not in rendered


def test_the_template_tag_renders_a_passages_link():
    template = Template('{% load ui_text %}<small>{% ui_text "organize_documents.confidentiality_help" %}</small>')

    rendered = template.render(Context({"jurisdiction": "illinois", "config": RULE_LINK}))

    assert '<small>Only when <a href="https://courts.example.gov/rule-138"' in rendered


def test_the_inline_markdown_filter_links_fee_waiver_copy():
    template = Template("{% load ui_text %}<p>{{ band.description|inline_markdown }}</p>")
    band = {"description": "Use your income. [150% FPL](https://example.org/fpl). <script>x</script>"}

    rendered = template.render(Context({"band": band}))

    assert '<p>Use your income. <a href="https://example.org/fpl"' in rendered
    assert "150% FPL (opens in a new tab)</a>" in rendered
    assert "<script" not in rendered
    assert "<p><p>" not in rendered


def test_an_empty_override_hides_a_passage():
    config = {"text": {"payment": {"waiver_documents_warning": ""}}}

    assert get_html("payment.waiver_documents_warning", config=config) == ""
    assert "may reject" in get_html("payment.waiver_documents_warning", config={})


def test_an_optional_passage_is_empty_until_a_state_sets_it():
    template = Template(
        '{% load ui_text %}{% ui_text "organize_documents.filing_type_help" as help %}'
        "{% if help %}<small>{{ help }}</small>{% endif %}"
    )

    assert template.render(Context({"config": {}})) == ""
    config = {"text": {"organize_documents": {"filing_type_help": "Pick the *specific* motion."}}}
    assert template.render(Context({"config": config})) == "<small>Pick the <em>specific</em> motion.</small>"


def test_email_and_phone_links_stay_in_this_tab():
    config = {"text": {"parties": {"role_help": "Ask [us](mailto:help@example.org) or [call](tel:+18005550100)."}}}

    rendered = get_html("parties.role_help", config=config)

    assert '<a href="mailto:help@example.org">us</a>' in rendered
    assert '<a href="tel:+18005550100">call</a>' in rendered
    assert "target" not in rendered


@pytest.mark.parametrize(
    "unsafe",
    [
        "[click](javascript:alert(1))",
        '<a href="https://example.org" onclick="alert(1)">click</a>',
        "<script>alert(1)</script>",
        '<img src="x" onerror="alert(1)">',
        "# A heading",
    ],
)
def test_configured_copy_cannot_carry_script_or_block_markup(unsafe):
    config = {"text": {"parties": {"role_help": unsafe}}}

    rendered = get_html("parties.role_help", config=config)

    for forbidden in ("javascript:", "onclick", "onerror", "<script", "<img", "<h1"):
        assert forbidden not in rendered


def test_a_placeholder_value_cannot_become_a_link():
    """A value filled in at render time -- which can be a filer's own name -- is
    shown as typed, never read as Markdown or HTML."""

    rendered = get_html(
        "your_information.lede",
        config={"text": {"your_information": {"lede": "Check your {brand_name} account details."}}},
        brand_name="[x](https://evil.example) <b>",
    )

    assert "<a" not in rendered
    assert "<b>" not in rendered
    assert "[x](https://evil.example) &lt;b&gt;" in rendered


def test_a_label_shows_only_the_words_of_a_link():
    """A link inside a <label> or <legend> is a control no one can use, and a
    label also ends up in aria-label attributes, so it stays plain text."""

    config = {"text": {"parties": {"role_question": "Your [role](https://example.org)?"}}}

    assert get_html("parties.role_question", config=config) == "Your role?"
    assert get_text("parties.role_question", config=config) == "Your role?"


def test_plain_text_for_scripts_drops_the_markdown():
    assert get_text("organize_documents.confidentiality_help", config=RULE_LINK) == (
        "Only when Rule 138 or a court order requires it."
    )


def test_the_check_reports_a_link_where_one_cannot_show(monkeypatch):
    from efile.utils import config_loader as loader_module

    real = loader_module.config_loader.load_jurisdiction_config

    def with_link(jurisdiction):
        config = dict(real(jurisdiction) or {})
        if jurisdiction == "illinois":
            config["text"] = {"parties": {"role_question": "Your [role](https://example.org)?"}}
        return config

    monkeypatch.setattr(loader_module.config_loader, "load_jurisdiction_config", with_link)

    problems = configured_ui_text_keys_are_known(None)

    assert [problem.id for problem in problems] == ["efile.W003"]


def test_configured_copy_is_extracted_for_translators():
    """xgettext cannot read YAML, so the generated stub is what puts a state's
    own wording into the Spanish catalog. It must not go stale."""

    call_command("extract_config_text", "--check")


def test_the_extraction_check_fails_when_config_copy_changes(monkeypatch):
    from efile.management.commands import extract_config_text

    monkeypatch.setattr(extract_config_text, "_render", lambda: "something else entirely")
    with pytest.raises(CommandError, match="out of date"):
        call_command("extract_config_text", "--check")


# -- The pages themselves ---------------------------------------------------


def organize_draft(client, django_user_model, jurisdiction):
    user = django_user_model.objects.create_user(username=f"{jurisdiction}-user", tyler_jurisdiction=jurisdiction)
    draft = FilingDraft.objects.create(
        user=user,
        jurisdiction=jurisdiction,
        workflow_version=2,
        existing_case=ExistingCase.NEW,
        court_code="court",
        case_category_code="100",
        case_type_code="200",
        current_step=WorkflowStepKey.ORGANIZE_DOCUMENTS,
        document_checklist_acknowledged=True,
    )
    reviewed_document(draft=draft, role=FilingDocument.Role.LEAD, sort_order=0, name="filing.pdf")
    reviewed_document(draft=draft, role=FilingDocument.Role.SUPPORTING, sort_order=0, name="exhibit.pdf")
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["auth_tokens"] = {f"TYLER-TOKEN-{jurisdiction.upper()}": "token"}
    session["jurisdiction"] = jurisdiction
    session.save()
    return draft


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("jurisdiction", "example"),
    [("illinois", "like a petition"), ("vermont", "like a complaint")],
)
def test_organize_documents_names_the_document_this_state_starts_with(client, django_user_model, jurisdiction, example):
    organize_draft(client, django_user_model, jurisdiction)

    response = client.get(reverse("organize_documents", kwargs={"jurisdiction": jurisdiction}))

    assert response.status_code == 200
    assert example in response.content.decode()


@pytest.mark.django_db
def test_organize_documents_separates_the_main_document_from_court_components(client, django_user_model):
    """The filer picks one main document; the court can label several PDFs
    "Lead Document" as filing components. The screen must not read as though
    the second is a repeat of the first."""

    organize_draft(client, django_user_model, "illinois")

    content = client.get(reverse("organize_documents", kwargs={"jurisdiction": "illinois"})).content.decode()

    assert "Document role" not in content
    assert "Court filing component" in content
    assert "This is separate from your main document" in content
    # The card header says the same thing the question above it said.
    assert "Lead document" not in content


@pytest.mark.django_db
def test_organize_documents_says_certified_copies_are_optional(client, django_user_model):
    organize_draft(client, django_user_model, "illinois")

    content = client.get(reverse("organize_documents", kwargs={"jurisdiction": "illinois"})).content.decode()

    assert "Certified copy and courtesy email (optional)" in content
    assert "A certified copy is a copy the court marks as official." in content


@pytest.mark.django_db
def test_organize_documents_hands_its_own_strings_to_the_script(client, django_user_model):
    """The script rewrites these lists after the court answers, so its copy has
    to travel with the page to stay configurable and translatable."""

    organize_draft(client, django_user_model, "illinois")

    content = client.get(reverse("organize_documents", kwargs={"jurisdiction": "illinois"})).content.decode()

    assert "loading_choices" in content
    assert "filing_component_fixed_note" in content
    assert '"default_confidentiality": "non_confidential"' in content
    assert "only file a document confidentially when a court rule or court order requires it" in content


@pytest.mark.django_db
def test_vermont_explains_document_names_and_filing_codes_at_each_field(client, django_user_model):
    draft = organize_draft(client, django_user_model, "vermont")
    lead = draft.documents.get(role=FilingDocument.Role.LEAD)

    content = client.get(reverse("organize_documents", kwargs={"jurisdiction": "vermont"})).content.decode()

    assert f'aria-describedby="document-name-help-{lead.pk}"' in content
    assert "Give each exhibit its own description." in content
    assert f'aria-describedby="filing-type-help-{lead.pk}"' in content
    assert "choose it instead of “Motion.”" in content


@pytest.mark.django_db
def test_a_state_without_field_help_shows_none(client, django_user_model):
    organize_draft(client, django_user_model, "illinois")

    content = client.get(reverse("organize_documents", kwargs={"jurisdiction": "illinois"})).content.decode()

    assert "document-name-help-" not in content
    assert "filing-type-help-" not in content


@pytest.mark.django_db
def test_vermont_upload_page_gives_its_document_rules_before_the_drop_zone(client, django_user_model):
    user = django_user_model.objects.create_user(username="vermont-upload", tyler_jurisdiction="vermont")
    draft = FilingDraft.objects.create(user=user, jurisdiction="vermont", workflow_version=2)
    client.force_login(user)
    session = client.session
    session[CURRENT_DRAFT_SESSION_KEY] = draft.pk
    session["auth_tokens"] = {"TYLER-TOKEN-VERMONT": "token"}
    session["jurisdiction"] = "vermont"
    session.save()

    content = client.get(reverse("upload_documents", kwargs={"jurisdiction": "vermont"})).content.decode()

    help_at = content.index('id="upload-help"')
    assert help_at < content.index('id="document-drop-zone"')
    assert "needs to be its own PDF" in content
    assert "Add your most important document first" in content
    assert 'aria-describedby="upload-help upload-requirements upload-preparation"' in content
    # The rules link sits in the court-rules box, outside the clickable drop zone.
    assert "e-filing questions and answers (opens in a new tab)</a>" in content


@pytest.mark.django_db
def test_vermont_start_page_explains_continuing_e_filing_and_after_hours_help(client):
    content = client.get(reverse("efile_options", kwargs={"jurisdiction": "vermont"})).content.decode()

    assert "keep e-filing in that case for as long as it lasts" in content
    assert 'href="https://www.vtcourts.gov/family/relief-abuse"' in content
