"""Summarize raw observations without conflating successful HTTP with AI completion."""

import json
from pathlib import Path

root = Path(__file__).parent / "results"
root.mkdir(parents=True, exist_ok=True)
qualifications = (
    json.loads((root / "qualifications.json").read_text())
    if (root / "qualifications.json").exists()
    else {}
)
rows = []
for file in sorted(root.glob("*.json")):
    data = json.loads(file.read_text())
    if (
        not isinstance(data, dict)
        or "scenario" not in data
        or file.name.startswith("smoke")
        or file.name == "submit-smoke.json"
        or "warmup" in file.stem
    ):
        continue
    telemetry = root / (file.stem + "-telemetry.json")
    peaks = {}
    if telemetry.exists():
        monitor = json.loads(telemetry.read_text())
        peaks["monitor_errors"] = sum("monitor_error" in s for s in monitor["samples"])
        for sample in monitor["samples"]:
            pg = sample.get("postgres", {})
            for key in (
                "connections",
                "active",
                "lock_waiters",
                "deadlocks",
                "pending_extractions",
            ):
                peaks[key] = max(peaks.get(key, 0), pg.get(key, 0))
            for container in sample.get("containers", []):
                name = (
                    container["Name"]
                    .removeprefix("litefile-capacity-270-")
                    .removesuffix("-1")
                )
                key = name + "_cpu_percent"
                peaks[key] = max(
                    peaks.get(key, 0), float(container["CPUPerc"].removesuffix("%"))
                )
                amount = container["MemUsage"].split("/")[0].strip()
                unit = next(u for u in ("GiB", "MiB", "KiB", "B") if amount.endswith(u))
                mib = (
                    float(amount.removesuffix(unit))
                    * {"GiB": 1024, "MiB": 1, "KiB": 1 / 1024, "B": 1 / 1048576}[unit]
                )
                key = name + "_memory_mib"
                peaks[key] = round(max(peaks.get(key, 0), mib), 2)
    endpoints = data.get("endpoints", {})
    ordinary = [
        value["p95"] for key, value in endpoints.items() if key != "upload_3_pdfs"
    ]
    errors = sum(data.get("errors", {}).values())
    row = {
        "scenario": file.stem,
        "errors": errors,
        "requests": data.get("requests", data.get("totals", {}).get("requests")),
        "completed_upload_journeys": data.get("totals", {}).get("completed"),
        "completed_submissions": data.get("succeeded"),
        "ordinary_p95_max_seconds": max(ordinary) if ordinary else None,
        "upload_p95_seconds": endpoints.get("upload_3_pdfs", {}).get("p95"),
        "submission_p95_seconds": data.get("seconds", {}).get("0.95"),
        "scheduler_lag_p99_seconds": data.get("scheduler_lag_p99"),
        "peaks": peaks,
    }
    row["http_slo_pass"] = errors == 0 and all(
        [
            row["ordinary_p95_max_seconds"] is None
            or row["ordinary_p95_max_seconds"] < 2,
            row["upload_p95_seconds"] is None or row["upload_p95_seconds"] < 5,
            row["submission_p95_seconds"] is None or row["submission_p95_seconds"] < 10,
        ]
    )
    if file.stem in qualifications:
        row["qualification"] = qualifications[file.stem]
    rows.append(row)
(root / "summary.json").write_text(json.dumps(rows, indent=2) + "\n")
print(json.dumps(rows, indent=2))
