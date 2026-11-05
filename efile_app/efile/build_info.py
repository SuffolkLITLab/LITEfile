"""Release version and source revision for the About page."""

import os

# Only used for a fixed local Git command.
import subprocess  # nosec B404
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

import tomllib
from django.conf import settings

PROJECT_DIR = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def release_version() -> str:
    with (PROJECT_DIR / "pyproject.toml").open("rb") as project:
        return tomllib.load(project)["project"]["version"]


@lru_cache(maxsize=1)
def commit_metadata() -> tuple[str, datetime | None]:
    """Prefer image metadata; inspect Git only in a local source checkout."""
    sha = os.environ.get("LITEFILE_COMMIT_SHA", "").strip()
    date = os.environ.get("LITEFILE_COMMIT_DATE", "").strip()
    if not sha and (PROJECT_DIR.parent / ".git").exists():
        try:
            # Fixed arguments, with no user input or shell execution.
            result = subprocess.run(  # nosec B603, B607
                ["git", "show", "-s", "--format=%H%n%cI", "HEAD"],
                cwd=PROJECT_DIR.parent,
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            )
            sha, date = result.stdout.strip().splitlines()
        except (OSError, subprocess.SubprocessError, ValueError):
            return "", None
    committed_at = None
    if sha and date:
        try:
            parsed = datetime.fromisoformat(date)
            if parsed.tzinfo is not None:
                committed_at = parsed.astimezone(UTC)
        except ValueError:
            pass
    return sha[:12], committed_at


def get_build_info() -> dict:
    show_commit = settings.SHOW_BUILD_COMMIT
    sha, date = commit_metadata() if show_commit else ("", None)
    return {"version": release_version(), "show_commit": show_commit, "commit_sha": sha, "commit_date": date}
