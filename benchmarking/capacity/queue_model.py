"""Illustrative FIFO capacity model from measured service times, not a forecast."""

import argparse
import heapq
import json
import math
from pathlib import Path


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def arrivals(count, two_zones):
    # Half the launch-day traffic arrives in two deadline peaks one hour apart.
    # Illinois is modeled separately with one Central-time 25% deadline peak.
    peak = count // 4
    windows = [(7.75 * 3600, 900, peak)]
    if two_zones:
        windows.append((8.75 * 3600, 900, peak))
    regular = count - sum(n for _, _, n in windows)
    windows.append((0, (9 if two_zones else 8) * 3600, regular))
    return sorted(
        start + duration * (i + 0.5) / n
        for start, duration, n in windows
        for i in range(n)
    )


def drain(offered, workers, service_times):
    free = [0.0] * workers
    completed, waits = [], []
    for index, arrival in enumerate(offered):
        start = max(arrival, heapq.heappop(free))
        finish = start + service_times[index % len(service_times)]
        heapq.heappush(free, finish)
        completed.append(finish)
        waits.append(finish - arrival)
    return sorted(completed), waits


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bedrock-results", type=Path, required=True)
    parser.add_argument("--preparation-seconds", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evidence = json.loads(args.bedrock_results.read_text())
    services = [job["seconds"] for job in evidence["jobs"] if job.get("completed")]
    assert services and args.preparation_seconds > 0
    scenarios = []
    for label, count, two_zones in [
        ("10000_per_month_on_22_weekdays", 455, True),
        ("1000_busy_launch_day", 1000, True),
        ("4000_illinois_weekday", 4000, False),
    ]:
        offered = arrivals(count, two_zones)
        for preparation_workers, ai_workers in [
            (1, 1),
            (1, 2),
            (2, 2),
            (2, 4),
            (4, 4),
            (4, 8),
            (8, 8),
        ]:
            prepared, prep_waits = drain(
                offered, preparation_workers, [args.preparation_seconds]
            )
            complete, ai_waits = drain(prepared, ai_workers, services)
            scenarios.append(
                {
                    "scenario": label,
                    "filings": count,
                    "preparation_workers": preparation_workers,
                    "ai_workers": ai_workers,
                    "preparation_p95_minutes": percentile(prep_waits, 0.95) / 60,
                    "ai_p95_minutes_after_preparation": percentile(ai_waits, 0.95) / 60,
                    "ai_exceeds_manual_fallback_fraction": sum(
                        wait > 120 for wait in ai_waits
                    )
                    / count,
                    "last_ai_completion_hours_after_9am": max(complete) / 3600,
                    "ai_worker_service_hours": sum(
                        services[i % len(services)] for i in range(count)
                    )
                    / 3600,
                }
            )
    cost = evidence["estimated_usd"] / len(services)
    result = {
        "assumptions": "Three measured AI samples cycled deterministically; fixed preparation mean; no retries, throttling, contention, AI worker startup, multiday carry or seasonal variation. The two-zone launch has 25% in each of two 15-minute peaks one hour apart; Illinois has one 25% Central-time peak. Not a tail-latency guarantee.",
        "ai_sample_seconds": services,
        "preparation_seconds": args.preparation_seconds,
        "estimated_ai_usd_per_filing": cost,
        "estimated_ai_usd_10000_month": cost * 10000,
        "estimated_ai_usd_4000_times_22_weekdays": cost * 4000 * 22,
        "scenarios": scenarios,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != "scenarios"}))


if __name__ == "__main__":
    main()
