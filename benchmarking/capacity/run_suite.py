"""Sequential scenarios: shared sessions must never be used by overlapping runs."""

import subprocess
import sys
from pathlib import Path

root = Path(__file__).parent
results = root / "results"
scenarios = [
    ("launch-1000h", "load.py", ["--rate", "0.277777778", "--duration", "300"]),
    ("sessions-150", "sessions.py", ["--users", "150", "--duration", "180"]),
    ("illinois-4000h", "load.py", ["--rate", "1.111111111", "--duration", "300"]),
    ("sessions-600", "sessions.py", ["--users", "600", "--duration", "180"]),
    ("simultaneous-150", "load.py", ["--burst", "150"]),
    ("recovery", "load.py", ["--rate", "0.277777778", "--duration", "120"]),
    (
        "large-15mb",
        "load.py",
        [
            "--rate",
            "0.277777778",
            "--duration",
            "120",
            "--corpus",
            "padded",
            "--pdf-kb",
            "5120",
        ],
    ),
    (
        "submit-4000h",
        "submit.py",
        ["--count", "200", "--offset", "1", "--rate", "1.111111111"],
    ),
    (
        "submit-simultaneous-150",
        "submit.py",
        ["--count", "150", "--offset", "201", "--rate", "0"],
    ),
]
for name, script, arguments in scenarios:
    print(f"START {name}", flush=True)
    command = [
        sys.executable,
        str(root / "monitor.py"),
        str(results / f"{name}-telemetry.json"),
        "uv",
        "run",
        "--with",
        "aiohttp==3.14.5",
        "python",
        str(root / script),
        *arguments,
        "--output",
        str(results / f"{name}.json"),
    ]
    process = subprocess.Popen(command)
    try:
        returncode = process.wait(timeout=720)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=30)
    if returncode:
        raise SystemExit(f"Harness failed: {name}, exit {returncode}")
    print(f"FINISH {name}", flush=True)
