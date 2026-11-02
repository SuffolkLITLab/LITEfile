"""Focused repeats and a locally tested scaling candidate; run after run_suite.py."""

import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

root = Path(__file__).parent.resolve()
base = ["docker", "compose", "-f", str(root / "compose.yml")]
conversion = ["-f", str(root / "compose-conversion.yml")]
scaled = ["-f", str(root / "compose-scaled.yml")]


def run(name, script, *arguments):
    print("START " + name, flush=True)
    command = [
        sys.executable,
        str(root / "monitor.py"),
        str(root / "results" / f"{name}-telemetry.json"),
        "uv",
        "run",
        "--with",
        "aiohttp==3.14.5",
        "python",
        str(root / script),
        *arguments,
        "--output",
        str(root / "results" / f"{name}.json"),
    ]
    process = subprocess.Popen(command)
    try:
        code = process.wait(timeout=600)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
    if code:
        raise SystemExit(f"Harness failed: {name}")
    print("FINISH " + name, flush=True)


def configure(overlays):
    subprocess.run(
        base + overlays + ["up", "-d", "--no-deps", "web", "converter"], check=True
    )
    subprocess.run(base + ["restart", "gateway"], check=True)
    for _ in range(60):
        try:
            with urlopen("http://127.0.0.1:8270/about/", timeout=5) as response:
                if response.status == 200:
                    time.sleep(3)
                    return
        except Exception:
            time.sleep(1)
    raise RuntimeError("Web service did not become ready")


if __name__ == "__main__":
    # Give the S3 simulator headroom; this is not an application resource change.
    # Update in place so the disposable stored objects survive between scenarios.
    subprocess.run(
        [
            "docker",
            "update",
            "--memory",
            "2g",
            "--memory-swap",
            "2g",
            "litefile-capacity-270-storage-1",
        ],
        check=True,
    )

    # A fixed amount of extra load, rather than an open-ended search for failure.
    run(
        "sessions-600-fixed-gateway",
        "sessions.py",
        "--users",
        "600",
        "--duration",
        "60",
    )
    run(
        "sessions-150-mixed",
        "sessions.py",
        "--users",
        "150",
        "--duration",
        "60",
        "--mixed",
    )
    configure(conversion)
    run("interactive-warmup", "load.py", "--burst", "1", "--corpus", "interactive")
    warmup = json.loads((root / "results/interactive-warmup.json").read_text())
    if warmup.get("totals", {}).get("completed") != 1:
        raise SystemExit(
            "Interactive fixture failed; inspect before running conversion load"
        )
    run(
        "interactive-launch",
        "load.py",
        "--rate",
        "0.277777778",
        "--duration",
        "60",
        "--corpus",
        "interactive",
    )
    print("BUILD pooled candidate", flush=True)
    subprocess.run(
        [
            "docker",
            "build",
            "-t",
            "litefile-capacity:pooled",
            "-f",
            str(root / "Dockerfile.pool"),
            str(root),
        ],
        check=True,
    )
    configure(conversion + scaled)
    run("scaled-warmup", "load.py", "--burst", "2")
    run(
        "scaled-sessions-600-mixed",
        "sessions.py",
        "--users",
        "600",
        "--duration",
        "120",
        "--mixed",
    )
    run("scaled-sessions-150", "sessions.py", "--users", "150", "--duration", "60")
    run("scaled-simultaneous-150", "load.py", "--burst", "150")
    run(
        "scaled-submit-simultaneous-150",
        "submit.py",
        "--offset",
        "351",
        "--count",
        "150",
        "--rate",
        "0",
    )
    run(
        "scaled-interactive-4000h",
        "load.py",
        "--rate",
        "1.111111111",
        "--duration",
        "60",
        "--corpus",
        "interactive",
    )
    subprocess.run(
        [
            "uv",
            "run",
            "--with",
            "aiohttp==3.14.5",
            "python",
            str(root / "double_submit.py"),
        ],
        check=True,
    )
    subprocess.run(
        base + ["exec", "-T", "web", ".venv/bin/python", "/bench/capacity/audit.py"],
        check=True,
    )
    print("FOLLOWUPS COMPLETE", flush=True)
