from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from django.urls import reverse

from efile.build_info import commit_metadata


@pytest.fixture(autouse=True)
def clear_metadata_cache():
    commit_metadata.cache_clear()
    yield
    commit_metadata.cache_clear()


@pytest.mark.parametrize("show_commit", [True, False])
@pytest.mark.parametrize("jurisdiction", [None, "massachusetts"])
@pytest.mark.django_db
def test_about_build_details(client, settings, monkeypatch, show_commit, jurisdiction):
    settings.SHOW_BUILD_COMMIT = show_commit
    settings.DEBUG = False  # Staging also displays the commit with DEBUG disabled.
    monkeypatch.setenv("LITEFILE_COMMIT_SHA", "abc123def4567890")
    monkeypatch.setenv("LITEFILE_COMMIT_DATE", "2026-11-05T09:30:15-05:00")
    url = reverse("jurisdiction_about", args=[jurisdiction]) if jurisdiction else reverse("about")
    with patch("efile.build_info.subprocess.run") as git:
        response = client.get(url)
    assert response.status_code == 200
    content = response.content.decode()
    assert "Version 0.9-rc1" in content
    assert ("abc123def456" in content) is show_commit
    assert ("2026-11-05 14:30:15 UTC" in content) is show_commit
    git.assert_not_called()


def test_local_checkout_reads_git(monkeypatch):
    monkeypatch.delenv("LITEFILE_COMMIT_SHA", raising=False)
    monkeypatch.delenv("LITEFILE_COMMIT_DATE", raising=False)
    with patch("efile.build_info.Path.exists", return_value=True):
        with patch("efile.build_info.subprocess.run") as git:
            git.return_value.stdout = "abc123def4567890\n2026-11-05T09:30:15-05:00\n"
            assert commit_metadata() == ("abc123def456", datetime(2026, 11, 5, 14, 30, 15, tzinfo=UTC))
            commit_metadata()
            git.assert_called_once()


def test_missing_git_is_harmless(monkeypatch):
    monkeypatch.delenv("LITEFILE_COMMIT_SHA", raising=False)
    with patch("efile.build_info.Path.exists", return_value=True):
        with patch("efile.build_info.subprocess.run", side_effect=FileNotFoundError):
            assert commit_metadata() == ("", None)


def test_image_without_metadata_does_not_run_git(monkeypatch):
    monkeypatch.delenv("LITEFILE_COMMIT_SHA", raising=False)
    with patch("efile.build_info.Path.exists", return_value=False):
        with patch("efile.build_info.subprocess.run") as git:
            assert commit_metadata() == ("", None)
            git.assert_not_called()


def test_invalid_timestamp_preserves_commit(monkeypatch):
    monkeypatch.setenv("LITEFILE_COMMIT_SHA", "abc123def4567890")
    monkeypatch.setenv("LITEFILE_COMMIT_DATE", "invalid")
    assert commit_metadata() == ("abc123def456", None)
