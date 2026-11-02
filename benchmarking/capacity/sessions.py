"""Active users with deterministic stagger and a 10-second action cadence."""

import argparse
import asyncio
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

import aiohttp
from load import BASE, J, ROOT, acquire_run_lock, percentile


async def main(args):
    identities = json.loads((ROOT / "sessions.json").read_text())[: args.users]
    samples = defaultdict(list)
    errors = Counter()
    examples = {}
    lateness = []
    started = time.perf_counter()
    end = started + args.duration
    connector = aiohttp.TCPConnector(limit=1500)

    async def user(index, identity):
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(
            connector=connector,
            connector_owner=False,
            cookie_jar=jar,
            cookies={"sessionid": identity["sessionid"], "csrftoken": f"{index:032x}"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            due = started + index * args.cadence / args.users
            n = 0
            while due < end:
                await asyncio.sleep(max(0, due - time.perf_counter()))
                lateness.append(time.perf_counter() - due)
                label, method, path = [
                    ("upload_page", "GET", J + "upload-documents/"),
                    ("snapshot", "GET", "/api/draft/"),
                    ("save", "POST", "/api/save-case-data/"),
                    ("extraction_poll", "GET", J + "document-extraction-status/"),
                    ("my_drafts", "GET", J + "my-drafts/"),
                ][(n + (index if args.mixed else 0)) % 5]
                kwargs = {}
                if method == "POST":
                    csrf = next((c.value for c in jar if c.key == "csrftoken"), "")
                    kwargs = {
                        "headers": {"X-CSRFToken": csrf},
                        "json": {"data": {"case_title": f"Synthetic session {index}"}},
                    }
                try:
                    async with session.request(
                        method, BASE + path, allow_redirects=False, **kwargs
                    ) as response:
                        body = await response.read()
                        if response.status != 200:
                            raise RuntimeError(f"{response.status}: {body[:100]!r}")
                        if (
                            response.content_type == "application/json"
                            and json.loads(body).get("success") is False
                        ):
                            raise RuntimeError(f"Business error: {body[:100]!r}")
                except Exception as exc:
                    errors[label] += 1
                    examples.setdefault(label, str(exc))
                samples[label].append(time.perf_counter() - due)
                n += 1
                due = started + index * args.cadence / args.users + n * args.cadence

    await asyncio.gather(*(user(i, identity) for i, identity in enumerate(identities)))
    await connector.close()
    elapsed = time.perf_counter() - started
    result = {
        "scenario": vars(args),
        "elapsed_seconds": elapsed,
        "requests": sum(map(len, samples.values())),
        "errors": dict(errors),
        "examples": examples,
        "scheduler_lag_p99": percentile(lateness, 0.99),
        "endpoints": {
            k: {
                "count": len(v),
                "p50": percentile(v, 0.5),
                "p95": percentile(v, 0.95),
                "p99": percentile(v, 0.99),
                "max": max(v),
            }
            for k, v in samples.items()
        },
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    run_lock = acquire_run_lock()
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mixed",
        action="store_true",
        help="Spread users across different action phases",
    )
    parser.add_argument("--users", type=int, default=150)
    parser.add_argument("--duration", type=int, default=180)
    parser.add_argument("--cadence", type=float, default=10)
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args()))
