"""A real HTTP double click must yield exactly one accepted submission."""

import asyncio
import json
from pathlib import Path

import aiohttp
from load import BASE, J, ROOT, acquire_run_lock


async def main():
    row = json.loads((ROOT / "submissions.json").read_text())[-1]
    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(
        cookie_jar=jar,
        cookies={"sessionid": row["sessionid"]},
        timeout=aiohttp.ClientTimeout(total=45),
    ) as session:
        async with session.get(BASE + J + "upload-documents/") as response:
            assert response.status == 200
            await response.read()
        csrf = next(c.value for c in jar if c.key == "csrftoken")

        async def submit():
            async with session.post(
                BASE + "/api/submit-final-filing/",
                json=row["payload"],
                headers={"X-CSRFToken": csrf, "X-Filing-Draft": str(row["draft"])},
                allow_redirects=False,
            ) as response:
                return {"status": response.status, "body": await response.json()}

        responses = await asyncio.gather(submit(), submit())
    accepted = sum(
        r["status"] == 200 and r["body"].get("success") is True for r in responses
    )
    result = {"draft": row["draft"], "accepted": accepted, "responses": responses}
    output = ROOT / "results" / "double-submit.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2) + "\n"
    )
    print(json.dumps(result, indent=2))
    assert accepted == 1, result
    assert sum(r["status"] in (400, 404, 409, 412) for r in responses) == 1, result


if __name__ == "__main__":
    run_lock = acquire_run_lock()
    asyncio.run(main())
