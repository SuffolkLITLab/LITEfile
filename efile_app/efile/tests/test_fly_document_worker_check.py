import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / ".github/scripts/check_fly_document_workers.py"


@pytest.mark.parametrize(
    ("group_to_check", "other_group"),
    [("preparation_worker", "extraction_worker"), ("extraction_worker", "preparation_worker")],
)
@pytest.mark.parametrize(
    ("state", "policy", "expected_code"),
    [
        ("started", "always", 0),
        ("stopped", "always", 1),
        ("started", "on-failure", 1),
        (None, None, 1),
    ],
)
def test_deploy_requires_started_document_workers_with_restart_policy(
    group_to_check, other_group, state, policy, expected_code
):
    machines = [
        {
            "id": group,
            "state": worker_state,
            "config": {"metadata": {"fly_process_group": group}, "restart": {"policy": worker_policy}},
        }
        for group, worker_state, worker_policy in (
            (other_group, "started", "always"),
            (group_to_check, state, policy),
        )
        if worker_state is not None
    ]
    result = subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(machines), text=True, capture_output=True)
    assert result.returncode == expected_code, result.stdout + result.stderr
    if expected_code:
        assert f"::error::No {group_to_check} machine is started" in result.stdout
