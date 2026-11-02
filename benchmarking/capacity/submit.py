"""Exercise real submission validation/claim/finalization against delayed local EFSP."""

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path

import aiohttp
from load import BASE, J, ROOT, acquire_run_lock, percentile


async def main(args):
    rows = json.loads((ROOT / "submissions.json").read_text())[
        args.offset : args.offset + args.count
    ]
    assert len(rows) == args.count
    durations = []
    errors = Counter()
    examples = {}
    started = time.perf_counter()
    connector = aiohttp.TCPConnector(limit=1500)

    async def submit(index, row):
        due = started + index / args.rate if args.rate else started
        await asyncio.sleep(max(0, due - time.perf_counter()))
        try:
            jar = aiohttp.CookieJar(unsafe=True)
            async with aiohttp.ClientSession(
                connector=connector,
                connector_owner=False,
                cookie_jar=jar,
                cookies={"sessionid": row["sessionid"]},
                timeout=aiohttp.ClientTimeout(total=45),
            ) as session:
                async with session.get(
                    BASE + J + "upload-documents/", allow_redirects=False
                ) as response:
                    await response.read()
                    assert response.status == 200, f"csrf page: {response.status}"
                csrf = next(c.value for c in jar if c.key == "csrftoken")
                async with session.post(
                    BASE + "/api/submit-final-filing/",
                    json=row["payload"],
                    headers={"X-CSRFToken": csrf, "X-Filing-Draft": str(row["draft"])},
                    allow_redirects=False,
                ) as response:
                    body = await response.text()
                    if (
                        response.status != 200
                        or json.loads(body).get("success") is not True
                    ):
                        raise RuntimeError(f"HTTP {response.status}: {body[:600]}")
        except Exception as exc:
            label = str(exc).split(":")[0] or type(exc).__name__
            errors[label] += 1
            examples.setdefault(label, str(exc))
        durations.append(time.perf_counter() - due)

    await asyncio.gather(*(submit(i, row) for i, row in enumerate(rows)))
    await connector.close()
    result = {
        "scenario": vars(args),
        "elapsed_seconds": time.perf_counter() - started,
        "attempted": len(rows),
        "succeeded": len(rows) - sum(errors.values()),
        "errors": dict(errors),
        "examples": examples,
        "seconds": {str(p): percentile(durations, p) for p in (0.5, 0.95, 0.99, 1)},
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    run_lock = acquire_run_lock()
    parser = argparse.ArgumentParser()
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--rate", type=float, default=1)
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args()))
