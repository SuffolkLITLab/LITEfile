import pytest
from django.urls import reverse
from pytest_django.asserts import assertTemplateUsed


@pytest.mark.django_db(False)
def test_about_page_has_litefile_identity_and_project_links(client):
    response = client.get(reverse("about"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "About LITEfile" in content
    assert "Suffolk University Law School" in content
    assert "LITEfile works with courts that have approved us to integrate" in content
    assert "https://github.com/SuffolkLITLab/LITEFile" in content
    assert "https://assemblyline.suffolklitlab.org/docs/volunteer" in content
    assert "Legal Services Corporation" in content
    assert "State Justice Institute" in content
    assert "logo-lsc.svg" in content
    assert "logo-sji.png" in content
    assert "logo-ilao.png" in content
    assert "logo-lsv.png" in content
    assert "Illinois Legal Aid Online (ILAO)" in content
    assert "VTLawHelp.org" in content
    assert "Massachusetts Legal Resource Finder" in content
    assert "litefile-logo.svg" in content
    assert "lit-lab-logo-large.svg" in content
    assertTemplateUsed(response, "efile/site_base.html")


@pytest.mark.django_db(False)
def test_jurisdiction_about_uses_state_branding_and_court_placeholder(client):
    response = client.get(reverse("jurisdiction_about", kwargs={"jurisdiction": "massachusetts"}))

    assert response.status_code == 200
    content = response.content.decode()
    assert "the Massachusetts Trial Court" in content
    assert "court-logo-massachusetts.png" in content
    assert "Massachusetts Legal Resource Finder" in content
    assert "Massachusetts Court Service Centers" in content
    assert "through the jurisdiction's electronic filing system" in content
    assertTemplateUsed(response, "efile/site_base.html")


@pytest.mark.django_db(False)
def test_terms_page_covers_efiling_privacy_and_security(client):
    response = client.get(reverse("jurisdiction_terms_of_service", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Terms of Service and Privacy Policy" in content
    assert "court acceptance" in content
    assert "We do not sell your personal information" in content
    assert "HTTPS encryption" in content
    assert "Illinois Courts" in content
    assert "https://www.ilcourthelp.gov/" in content
    assertTemplateUsed(response, "efile/site_base.html")


@pytest.mark.django_db(False)
def test_invalid_jurisdiction_redirects_to_chooser(client):
    response = client.get(reverse("jurisdiction_about", kwargs={"jurisdiction": "invalid-state"}))

    assert response.status_code == 302
    assert response.url == reverse("efile_choose_jurisdiction")


@pytest.mark.django_db
def test_jurisdiction_about_page_shows_brand_title(client):
    response = client.get(reverse("jurisdiction_about", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Illinois e-Filing" in content
    assert "Illinois Legal Aid Online (ILAO)" in content
    assert "logo-ilao.png" in content
    assert "logo-lsc.svg" in content
    assert "logo-sji.png" in content
    assert "Illinois Court Help" in content
    assert "https://www.ilcourthelp.gov/" in content
    assert "Sign in" in content


@pytest.mark.django_db
def test_vermont_about_shows_lsv_partner_logo(client):
    response = client.get(reverse("jurisdiction_about", kwargs={"jurisdiction": "vermont"}))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Vermont e-Filing" in content
    assert "Legal Services Vermont (LSV)" in content
    assert "logo-lsv.png" in content
    assert "logo-lsc.svg" in content
    assert "logo-sji.png" in content
    assert "court-logo-vermont.svg" in content
    assert "https://www.vtcourts.gov/self-help" in content


@pytest.mark.django_db
@pytest.mark.parametrize("jurisdiction", [None, "illinois", "vermont"])
def test_about_page_leaks_no_template_source(client, jurisdiction):
    """Django comment tags are single-line. A multi-line `{# ... #}` renders
    every line after the first onto the page."""

    url = (
        reverse("about")
        if jurisdiction is None
        else reverse("jurisdiction_about", kwargs={"jurisdiction": jurisdiction})
    )

    content = client.get(url).content.decode()

    assert "{#" not in content
    assert "#}" not in content
    assert "{%" not in content


@pytest.mark.django_db
def test_vermont_about_uses_the_partner_approved_description(client):
    """LSV approved this wording; it comes from vermont.yaml, not the template."""

    response = client.get(reverse("jurisdiction_about", kwargs={"jurisdiction": "vermont"}))

    content = response.content.decode()
    assert "in close partnership with Legal Services Vermont (LSV)" in content
    assert "deploying LITEfile to expand accessible electronic court filing" in content
    # The earlier copy named a second organization and a different website.
    assert "Vermont Legal Aid, who collaborated" not in content
    assert "VTLawHelp to make court filing accessible" not in content


@pytest.mark.django_db
def test_illinois_about_partner_copy_is_unchanged(client):
    response = client.get(reverse("jurisdiction_about", kwargs={"jurisdiction": "illinois"}))

    content = response.content.decode()
    assert "Illinois Legal Aid Online (ILAO), who provided project leadership" in content
    assert "logo-ilao.png" in content
    assert "logo-lsv.png" not in content


@pytest.mark.django_db
def test_about_page_carries_filing_menu_when_signed_in(client, django_user_model):
    user = django_user_model.objects.create_user(username="about-nav-user", tyler_jurisdiction="illinois")
    client.force_login(user)
    session = client.session
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()

    response = client.get(reverse("jurisdiction_about", kwargs={"jurisdiction": "illinois"}))

    assert response.status_code == 200
    content = response.content.decode()
    assert "Start filing" in content
    assert "My draft e-filings" in content
    assert "Illinois e-Filing" in content


@pytest.mark.django_db(False)
def test_choose_jurisdiction_carries_footer(client):
    response = client.get(reverse("efile_choose_jurisdiction"))

    assert response.status_code == 200
    content = response.content.decode()
    assert "About LITEfile" in content
    assert "Terms of Service and Privacy" in content
    assert "suffolk-lit-lab-logo.svg" in content


@pytest.mark.django_db(False)
def test_footer_help_links_point_to_state_court_resources(client):
    il_res = client.get(reverse("efile_options", kwargs={"jurisdiction": "illinois"}))
    assert 'href="https://www.ilcourthelp.gov/"' in il_res.content.decode()

    ma_res = client.get(reverse("efile_options", kwargs={"jurisdiction": "massachusetts"}))
    assert 'href="https://www.mass.gov/orgs/court-service-centers"' in ma_res.content.decode()

    vt_res = client.get(reverse("efile_options", kwargs={"jurisdiction": "vermont"}))
    assert 'href="https://www.vtcourts.gov/self-help"' in vt_res.content.decode()


@pytest.mark.django_db
def test_root_about_does_not_inherit_session_jurisdiction(client, django_user_model):
    user = django_user_model.objects.create_user(username="session-test-user", tyler_jurisdiction="illinois")
    client.force_login(user)
    session = client.session
    session["jurisdiction"] = "illinois"
    session["auth_tokens"] = {"TYLER-TOKEN-ILLINOIS": "token"}
    session.save()

    # Navigating to root /about/ should remain transparent/global, not state-scoped
    response = client.get(reverse("about"))
    assert response.status_code == 200
    content = response.content.decode()
    assert response.context["jurisdiction"] is None
    assert "LITEfile works with courts that have approved us to integrate" in content
    assert "Seal of the Supreme Court of Illinois" in content
    assert "Seal of the Massachusetts Trial Court" in content
    assert "Seal of the State of Vermont" in content
    # Should NOT have the single-state tool statement callout on root about
    assert "official-tool-callout" not in content
