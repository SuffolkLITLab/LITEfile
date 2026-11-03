"""Shared fixtures for the Python suite."""

import pytest
from django.test import override_settings


@pytest.fixture(autouse=True)
def stand_in_document_disabled_by_default():
    """Run every test in the production-shaped configuration.

    EFSP_TEST_DOCUMENT_URL is a local-development affordance -- compose sets it by
    default so fee quotes work against LocalStack uploads -- but pytest-django
    forces DEBUG=False, and "stand-in URL set, DEBUG off" is precisely what
    substitute_test_document_urls refuses to run under. Without this fixture the
    suite would pass in CI and fail on a developer's machine, for any test that
    exercises a filing path.

    Tests that want the substitution opt into it with override_settings, so the
    dependency is visible in the test that relies on it.
    """
    with override_settings(EFSP_TEST_DOCUMENT_URL=""):
        yield


@pytest.fixture(autouse=True)
def court_requirements(monkeypatch):
    """Stand in for the court's disclaimer lookup so no test calls the live EFSP.

    Tests of the lookup itself override this fixture with one of the same name.
    """
    monkeypatch.setattr("efile.services.disclaimers.court_disclaimers", lambda draft, fresh=False: [])


@pytest.fixture(autouse=True)
def appellate_code_lists(monkeypatch):
    """Appellate tests opt into specific catalogs; other tests never call EFSP."""
    monkeypatch.setattr("efile.services.appeals.code_list", lambda jurisdiction, path, lookups=None: None)


@pytest.fixture(autouse=True)
def efsp_validation_metadata(monkeypatch):
    """Validation lookup tests opt into metadata; other tests use USPS fallback."""
    monkeypatch.setattr("efile.services.efsp_validation.RuleLookups.get", lambda *args: None)


@pytest.fixture(autouse=True)
def court_headings(monkeypatch):
    """Search and the court dropdown drop no heading courts unless a test opts in.

    Tests of the court lists themselves call court_selection directly.
    """
    monkeypatch.setattr("efile.services.filing_code_search.heading_court_codes", lambda *args: frozenset())
    monkeypatch.setattr("efile.api.dropdown_views.heading_court_codes", lambda *args: frozenset())


@pytest.fixture(autouse=True)
def payment_validation(monkeypatch):
    """Unrelated flow tests do not fetch live payment accounts.

    test_court_payments overrides this fixture and exercises the real validator,
    including each HTTP boundary and the browser flow.
    """
    monkeypatch.setattr("efile.api.filing_views.validate_filing_payment", lambda *args: {})
    monkeypatch.setattr("efile.views.submission.validate_filing_payment", lambda *args: {})
    monkeypatch.setattr("efile.services.court_payments.bank_payment_policy", lambda *args: "allowed")
    monkeypatch.setattr("efile.api.auth_views.bank_payment_policy", lambda *args: "allowed")


@pytest.fixture
def enabled_document_checklists(monkeypatch):
    """Exercise the optional checklist feature even while Illinois has it paused."""
    from efile.utils.config_loader import config_loader

    load = config_loader.load_jurisdiction_config

    def enabled(jurisdiction):
        return {**(load(jurisdiction) or {}), "document_checklists_enabled": True}

    monkeypatch.setattr(config_loader, "load_jurisdiction_config", enabled)
