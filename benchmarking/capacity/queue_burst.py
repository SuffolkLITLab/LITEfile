"""Loopback-only burst acceptance; completion audited separately in PostgreSQL."""

import argparse
import asyncio
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent


async def main(args):
    sessions = json.loads((ROOT / "sessions.json").read_text())[
        args.offset : args.offset + args.users
    ]
    assert len(sessions) == args.users
    corpus = [
        ROOT.parent / "synthetic/filled_pdfs/interactive" / f"IL-{i:02}.pdf"
        for i in (1, 2, 3)
    ]
    sources = [(p.name, p.read_bytes()) for p in corpus]
    ready = asyncio.Event()
    all_ready = asyncio.Event()
    preloaded = 0
    rows = []

    async def user(index, record):
        nonlocal preloaded
        cookie = aiohttp.CookieJar(unsafe=True)
        cookie.update_cookies({"sessionid": record["sessionid"]})
        async with aiohttp.ClientSession(
            cookie_jar=cookie, timeout=aiohttp.ClientTimeout(total=180)
        ) as client:
            async with client.get(
                "http://127.0.0.1:8270/jurisdiction/illinois/upload-documents/"
            ) as response:
                html = await response.text()
                assert response.status == 200
                csrf = re.search(
                    r'name="csrfmiddlewaretoken" value="([^"]+)"', html
                ).group(1)
            preloaded += 1
            if preloaded == len(sessions):
                all_ready.set()
            await ready.wait()
            form = aiohttp.FormData()
            form.add_field("csrfmiddlewaretoken", csrf)
            form.add_field("upload_request_token", str(uuid.uuid4()))
            for name, content in sources:
                form.add_field(
                    "documents", content, filename=name, content_type="application/pdf"
                )
            start = time.monotonic()
            row = {"user_id": record["user_id"]}
            try:
                async with client.post(
                    "http://127.0.0.1:8270/jurisdiction/illinois/upload-documents/",
                    data=form,
                ) as response:
                    row["status"] = response.status
                    result = await response.json()
                    row["accepted"] = (
                        response.status in (200, 202) and result.get("success") is True
                    )
                    row["queued"] = result.get("preparation_pending", False)
                    row["job_id"] = result.get("preparation_job_id")
            except Exception as error:
                row["accepted"] = False
                row["error_type"] = type(error).__name__
            row["seconds"] = time.monotonic() - start
            rows.append(row)

    tasks = [asyncio.create_task(user(i, row)) for i, row in enumerate(sessions)]
    # Synchronize arrivals only after every authenticated page is loaded.
    await asyncio.wait_for(all_ready.wait(), timeout=120)
    started = time.monotonic()
    ready.set()
    await asyncio.gather(*tasks)
    elapsed = time.monotonic() - started
    delays = sorted(r["seconds"] for r in rows)
    result = {
        "users": args.users,
        "offset": args.offset,
        "accepted": sum(r["accepted"] for r in rows),
        "acceptance_elapsed_seconds": elapsed,
        "acceptance_p95_seconds": delays[int(0.95 * (len(delays) - 1))],
        "timeout_seconds": 180,
        "files": {n: hashlib.sha256(b).hexdigest() for n, b in sources},
        "rows": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k not in {"rows", "files"}}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=150)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    assert 1 <= args.users <= 150
    asyncio.run(main(args))
