"""Test the next bounded candidate after the two-core comparison has completed."""

import subprocess
from followups import base, configure, conversion, root, run, scaled

if __name__ == "__main__":
    configure(conversion + scaled + ["-f", str(root / "compose-scaled4.yml")])
    run("scaled4-warmup", "load.py", "--burst", "4")
    run(
        "scaled4-sessions-600-mixed",
        "sessions.py",
        "--users",
        "600",
        "--duration",
        "120",
        "--mixed",
    )
    run("scaled4-simultaneous-150", "load.py", "--burst", "150")
    run(
        "scaled4-interactive-4000h",
        "load.py",
        "--rate",
        "1.111111111",
        "--duration",
        "60",
        "--corpus",
        "interactive",
    )
    # Prior fixture attempts are never retried after an ambiguous outcome.
    # Prepare fresh drafts and preserve the earlier fixture inventory locally.
    (root / "submissions.json").rename(root / "submissions-initial.json")
    subprocess.run(
        base
        + [
            "exec",
            "-T",
            "-e",
            "CAPACITY_SUBMISSIONS=150",
            "web",
            ".venv/bin/python",
            "/bench/capacity/seed_submissions.py",
        ],
        check=True,
    )
    run("scaled4-submit-simultaneous-150", "submit.py", "--count", "150", "--rate", "0")
    subprocess.run(
        base + ["exec", "-T", "web", ".venv/bin/python", "/bench/capacity/audit.py"],
        check=True,
    )
    print("FOUR-CORE COMPARISON COMPLETE", flush=True)
