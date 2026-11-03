"""Each screen links to its page in the public user guide."""

from pathlib import Path

import pytest
from django.conf import settings
from django.urls import get_resolver, reverse

from efile.context_processors import USER_GUIDE_PAGES

GUIDE_SOURCE = Path(settings.BASE_DIR).parent / "docs" / "docs" / "user-guide"


def test_every_mapped_screen_is_a_real_url_name():
    names = {name for name in get_resolver().reverse_dict if isinstance(name, str)}
    assert set(USER_GUIDE_PAGES) <= names


@pytest.mark.skipif(not GUIDE_SOURCE.is_dir(), reason="Documentation source is not in this checkout")
def test_every_linked_guide_page_and_heading_exists():
    """A renamed page or heading would otherwise send filers to a 404 or the wrong place."""

    for target in set(USER_GUIDE_PAGES.values()):
        page, _, anchor = target.partition("#")
        source = GUIDE_SOURCE / f"{page}.md"
        assert source.is_file(), target
        if anchor:
            headings = [
                line.lstrip("#").strip().lower() for line in source.read_text().splitlines() if line.startswith("##")
            ]
            slugs = {"-".join("".join(c for c in h if c.isalnum() or c in " -").split()) for h in headings}
            assert anchor in slugs, target


@pytest.mark.django_db
def test_sign_in_page_links_to_its_guide(client):
    response = client.get(reverse("efile_login", kwargs={"jurisdiction": "illinois"}))

    expected = settings.LITEFILE_USER_GUIDE_URL.rstrip("/") + "/sign-in"
    assert f'href="{expected}"' in response.content.decode()
    assert "Read the guide for this page" in response.content.decode()


@pytest.mark.django_db
def test_guide_links_can_be_turned_off(client, settings):
    settings.LITEFILE_USER_GUIDE_URL = ""

    response = client.get(reverse("efile_login", kwargs={"jurisdiction": "illinois"}))

    assert "Read the guide for this page" not in response.content.decode()
