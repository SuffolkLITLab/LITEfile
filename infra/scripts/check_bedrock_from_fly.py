"""Two fabricated endpoint probes from an existing Fly machine; no deployment.

Run using the application environment. Receipts never contain OIDC or STS tokens.
"""

import argparse
import base64
import hashlib
import io
import json
import shlex
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    image = Image.new("RGB", (600, 120), "white")
    ImageDraw.Draw(image).text(
        (20, 30), "Case No. 2026 CV 1234", fill="black", font_size=30
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    (args.out / "caption.png").write_bytes(buffer.getvalue())
    source = (ROOT / "efile_app/efile/utils/bedrock_auth.py").read_bytes()
    source64 = base64.b64encode(source).decode()
    image64 = base64.b64encode(buffer.getvalue()).decode()
    code = f"""import base64,json,time
exec(base64.b64decode({source64!r}))
from openai import OpenAI
http_client=httpx.Client(auth=BedrockAuth({args.role!r},"us-east-1"),follow_redirects=False,timeout=60)
client=OpenAI(api_key="sigv4",base_url="https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1/",http_client=http_client,max_retries=0)
results=[]
for model,effort in [("us.openai.gpt-6-luna","none"),("us.openai.gpt-6.1-terra","low")]:
 content="Case No. 2026 CV 1234" if "luna" in model else [{{"type":"text","text":"Read the case number from this caption."}},{{"type":"image_url","image_url":{{"url":"data:image/png;base64,{image64}"}}}}]
 start=time.perf_counter()
 try:
  r=client.chat.completions.create(model=model,messages=[{{"role":"system","content":"Extract the literal docket number. Return only a JSON object with key docket number."}},{{"role":"user","content":content}}],response_format={{"type":"json_object"}},reasoning_effort=effort,max_completion_tokens=512)
  results.append({{"model":model,"returned_model":r.model,"response_id":r.id,"content":r.choices[0].message.content,"finish_reason":r.choices[0].finish_reason,"usage":r.usage.model_dump() if r.usage else None,"seconds":time.perf_counter()-start}})
 except Exception as e:
  results.append({{"model":model,"error_type":type(e).__name__,"status_code":getattr(e,"status_code",None)}})
client.close()
print(json.dumps(results))
"""
    (args.out / "registration.json").write_text(
        json.dumps(
            {
                "purpose": "endpoint compatibility, not accuracy benchmarking",
                "app": args.app,
                "role": args.role,
                "expected": {"docket number": "2026 CV 1234"},
                "max_requests": 2,
                "budget_estimate_usd": 0.05,
                "max_completion_tokens": 512,
                "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                "auth_sha256": hashlib.sha256(source).hexdigest(),
            },
            indent=2,
        )
        + "\n"
    )
    (args.out / "probe.py").write_text(code)
    # Fixed CLI; user arguments are argv entries, and remote Python uses shlex quoting.
    result = subprocess.run(
        [
            "fly",
            "ssh",
            "console",
            "-a",
            args.app,
            "--machine",
            args.machine,
            "-C",
            shlex.join(["/app/efile_app/.venv/bin/python", "-c", code]),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode:
        raise SystemExit(
            f"Fly smoke execution failed with status {result.returncode}; no raw error logged"
        )
    rows = json.loads(result.stdout)
    (args.out / "results.json").write_text(json.dumps(rows, indent=2) + "\n")
    print(json.dumps(rows))
    if any(
        r.get("error_type")
        or json.loads(r.get("content") or "{}") != {"docket number": "2026 CV 1234"}
        for r in rows
    ):
        raise SystemExit("Bedrock smoke did not pass")


if __name__ == "__main__":
    main()
