"""Same-resource A/B experiment; requires a freshly seeded capacity stack."""

import os
import subprocess
import time
from urllib.request import urlopen

from followups import base, root, run


def configure(size):
    subprocess.run(
        base
        + ["-f", str(root / "compose-pool-micro.yml"), "up", "-d", "--no-deps", "web"],
        env={**os.environ, "MICRO_POOL_SIZE": str(size)},
        check=True,
    )
    subprocess.run(base + ["restart", "gateway"], check=True)
    for _ in range(60):
        try:
            with urlopen("http://127.0.0.1:8270/about/", timeout=5) as response:
                if response.status == 200:
                    time.sleep(2)
                    return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Web service did not become ready")


if __name__ == "__main__":
    for size, name, offset in [(0, "off", 0), (8, "on", 150)]:
        configure(size)
        run(f"pool-micro-{name}-warmup", "load.py", "--burst", "2")
        run(
            f"pool-micro-{name}-mixed",
            "sessions.py",
            "--users",
            "150",
            "--duration",
            "60",
            "--mixed",
        )
        run(f"pool-micro-{name}-burst", "load.py", "--burst", "150")
        run(
            f"pool-micro-{name}-submit",
            "submit.py",
            "--offset",
            str(offset),
            "--count",
            "150",
            "--rate",
            "0",
        )
    # Repeat the burst after both configurations have warmed the same database.
    for size, name in [(0, "off-repeat"), (8, "on-repeat")]:
        configure(size)
        run(f"pool-micro-{name}-burst", "load.py", "--burst", "150")
    subprocess.run(
        [
            "uv",
            "run",
            "--with",
            "aiohttp==3.14.5",
            "python",
            str(root / "double_submit.py"),
            "--output",
            str(root / "results" / "pool-micro-double-submit.json"),
        ],
        check=True,
    )
    subprocess.run(
        base
        + [
            "exec",
            "-T",
            "web",
            ".venv/bin/python",
            "/bench/capacity/audit.py",
            "--output",
            "/bench/capacity/results/pool-micro-database-audit.json",
        ],
        check=True,
    )
