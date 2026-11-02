"""Bounded open-loop load; latency includes scheduling delay, failures are never successes."""

import argparse
import fcntl
import asyncio
import json
import math
import time
from collections import Counter, defaultdict
from pathlib import Path

import aiohttp

ROOT = Path(__file__).parent
BASE = "http://127.0.0.1:8270"
J = "/jurisdiction/illinois/"


def acquire_run_lock():
    lock = open("/tmp/litefile-capacity-load.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(
            "Another capacity generator is running; shared sessions must not overlap."
        )
    return lock


def percentile(values, p):
    values = sorted(values)
    return round(values[max(0, math.ceil(len(values) * p) - 1)], 4) if values else None


async def main(args):
    identities = json.loads((ROOT / "sessions.json").read_text())
    samples = defaultdict(list)
    errors = Counter()
    examples = {}
    totals = Counter()
    latencies = []
    lag = []
    slots = asyncio.Queue()
    for identity in identities[: args.users]:
        slots.put_nowait(identity)
    pdfs = [(ROOT / f"document-{args.pdf_kb}k.pdf").read_bytes()] * 3
    if args.corpus != "padded":
        pdfs = [
            (
                ROOT.parent / "synthetic" / "filled_pdfs" / args.corpus / f"IL-0{i}.pdf"
            ).read_bytes()
            for i in (1, 2, 3)
        ]
    connector = aiohttp.TCPConnector(limit=1500)

    async def journey(scheduled):
        identity = None
        began = time.perf_counter()
        lag.append(began - scheduled)
        try:
            identity = await asyncio.wait_for(slots.get(), timeout=5)
            jar = aiohttp.CookieJar(unsafe=True)
            async with aiohttp.ClientSession(
                connector=connector,
                connector_owner=False,
                cookie_jar=jar,
                cookies={"sessionid": identity["sessionid"]},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as session:

                async def request(label, method, path, expected=200, **kw):
                    start = time.perf_counter()
                    csrf = next((c.value for c in jar if c.key == "csrftoken"), "")
                    async with session.request(
                        method,
                        BASE + path,
                        allow_redirects=False,
                        headers={"X-CSRFToken": csrf},
                        **kw,
                    ) as response:
                        body = await response.read()
                        samples[label].append(time.perf_counter() - start)
                        totals["requests"] += 1
                        if response.status != expected:
                            raise RuntimeError(
                                f"{label}: HTTP {response.status}: {body[:400]!r}"
                            )
                        if response.content_type == "application/json":
                            data = json.loads(body)
                            if data.get("success") is False:
                                raise RuntimeError(f"{label}: business failure: {data}")
                            return data
                        return body

                await request("upload_page", "GET", J + "upload-documents/")
                await request(
                    "start", "POST", J + "start-filing/", expected=302, data={}
                )
                form = aiohttp.FormData()
                for n, pdf in enumerate(pdfs):
                    form.add_field(
                        "documents",
                        pdf,
                        filename=f"Synthetic-{n}.pdf",
                        content_type="application/pdf",
                    )
                uploaded = await request(
                    "upload_3_pdfs", "POST", J + "upload-documents/", data=form
                )
                assert uploaded["document_count"] == 3, uploaded
                for n in range(6):
                    await request(
                        "extraction_poll", "GET", J + "document-extraction-status/"
                    )
                    if args.think:
                        await asyncio.sleep(args.think)
                for n in range(3):
                    await request(
                        "save",
                        "POST",
                        "/api/save-case-data/",
                        json={
                            "jurisdiction": "illinois",
                            "data": {
                                "case_title": f"Synthetic-{identity['user_id']}-{n}",
                                "existing_case": "new",
                            },
                        },
                    )
                    snapshot = await request("snapshot", "GET", "/api/draft/")
                    current = snapshot["data"]["filing_draft"]
                    assert (
                        current["case_title"] == f"Synthetic-{identity['user_id']}-{n}"
                    ), "Saved title differs"
                    assert current["document_count"] == 3, "Saved documents differ"
                upload_data = await request("documents", "GET", "/api/get-upload-data/")
                for document_index, doc in enumerate(
                    [
                        upload_data["files"]["lead"],
                        *upload_data["files"].get("supporting", []),
                    ]
                ):
                    doc_id = doc.get("document_id") or doc.get("id")
                    if doc_id is None:
                        raise RuntimeError(f"Missing document id: {list(doc)}")
                    content = await request(
                        "preview_pdf", "GET", J + f"documents/{doc_id}/content/"
                    )
                    assert content.startswith(b"%PDF"), "Preview is not PDF"
                    assert len(content) > 100, "Preview is empty"
                    if args.corpus != "interactive":
                        assert content == pdfs[document_index], (
                            "Preview bytes differ from uploaded PDF"
                        )
                totals["completed"] += 1
        except Exception as exc:
            label = str(exc).split(":")[0] or type(exc).__name__
            errors[label] += 1
            examples.setdefault(label, str(exc)[:600])
            totals["failed"] += 1
        finally:
            latencies.append(time.perf_counter() - scheduled)
            if identity:
                slots.put_nowait(identity)

    started = time.perf_counter()
    tasks = []
    due = started
    n = args.burst if args.burst else math.ceil(args.rate * args.duration)
    for i in range(n):
        if not args.burst:
            due = started + i / args.rate
            await asyncio.sleep(max(0, due - time.perf_counter()))
        tasks.append(asyncio.create_task(journey(due)))
    await asyncio.gather(*tasks)
    elapsed = time.perf_counter() - started
    await connector.close()
    result = {
        "scenario": vars(args),
        "elapsed_seconds": elapsed,
        "totals": dict(totals),
        "errors": dict(errors),
        "error_examples": examples,
        "offered_journeys": n,
        "achieved_requests_per_second": totals["requests"] / elapsed,
        "journey_seconds": {
            str(p): percentile(latencies, p) for p in (0.5, 0.95, 0.99, 1)
        },
        "scheduler_lag_p99": percentile(lag, 0.99),
        "endpoints": {
            key: {
                "count": len(v),
                "p50": percentile(v, 0.5),
                "p95": percentile(v, 0.95),
                "p99": percentile(v, 0.99),
                "max": max(v),
            }
            for key, v in samples.items()
        },
    }
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    run_lock = acquire_run_lock()
    parser = argparse.ArgumentParser()
    parser.add_argument("--rate", type=float, default=0.1)
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--users", type=int, default=150)
    parser.add_argument("--burst", type=int, default=0)
    parser.add_argument("--think", type=float, default=0)
    parser.add_argument("--pdf-kb", type=int, choices=[256, 1024, 5120], default=256)
    parser.add_argument(
        "--corpus", choices=["padded", "flattened", "interactive"], default="flattened"
    )
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args()))
