"""Opt-in fabricated deployment canary. Run inside the app environment.

No filing submission, database writes, or private source documents.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "efile_app"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--aws-profile", help="Local calibration only; omit on Fly")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    import django

    django.setup()
    from django.conf import settings
    from PIL import Image, ImageDraw

    from efile.services.evidence_ensemble import Readers, extract_ensemble, image_url
    from efile.utils import bedrock_auth
    from efile.utils.llms import chat_completion

    if (
        settings.AI_PROVIDER != "bedrock"
        or settings.DOCUMENT_EVIDENCE_STRATEGY != "bedrock_ensemble"
    ):
        raise RuntimeError("Canary requires Bedrock-only configuration")
    if args.aws_profile:
        from botocore.credentials import Credentials

        c = json.loads(
            subprocess.check_output(
                [
                    "aws",
                    "configure",
                    "export-credentials",
                    "--profile",
                    args.aws_profile,
                    "--format",
                    "process",
                ],
                text=True,
            )
        )
        credentials = Credentials(
            c["AccessKeyId"], c["SecretAccessKey"], c.get("SessionToken")
        )
        bedrock_auth.FlyCredentials.get = lambda self: credentials
    expected = {
        "docket number": "2026 CV 1234",
        "plaintiff or petitioner names": "Avery Morgan",
        "defendant or respondent names": "Jordan Hayes",
    }
    registration = {
        "expected": expected,
        "models": {
            k: v["deployment"] for k, v in settings.DOCUMENT_ENSEMBLE_MODELS.items()
        },
        "source_sha256": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                ROOT / "efile_app/efile/services/evidence_ensemble.py",
                ROOT / "efile_app/efile/utils/bedrock_auth.py",
                ROOT / "efile_app/efile/utils/llms.py",
                Path(__file__),
            ]
        },
        "purpose": "deployment compatibility and controlled-value check, not corpus benchmark",
        "maximum_requests": 12,
        "maximum_output_tokens_per_evidence_call": 8192,
        "budget_estimate_usd": 1,
    }
    (args.out / "registration.json").write_text(
        json.dumps(registration, indent=2) + "\n"
    )
    caption = Image.new("RGB", (900, 180), "white")
    ImageDraw.Draw(caption).text(
        (20, 40), "Case No. 2026 CV 1234", fill="black", font_size=40
    )
    readers = Readers("illinois", None)
    rows = []
    try:
        for role in ["luna", "haiku", "terra"]:
            value = readers.read(
                role, "Read the case number.", [image_url(caption)], docket_only=True
            )
            rows.append({"role": role, "value": value, "receipt": readers.calls[-1]})
    finally:
        readers.close()
    (args.out / "model-probes.json").write_text(json.dumps(rows, indent=2) + "\n")
    if any(
        r["value"].get("docket number") != expected["docket number"]
        or r["receipt"]["status"] != "complete"
        for r in rows
    ):
        raise RuntimeError("Model compatibility probe failed; inspect safe receipts")
    pdf = args.out / "synthetic-motion.pdf"
    page = Image.new("RGB", (1224, 1584), "white")
    draw = ImageDraw.Draw(page)
    lines = [
        "Circuit Court of Cook County, Illinois",
        "Avery Morgan, Plaintiff",
        "v.",
        "Jordan Hayes, Defendant",
        "Case No. 2026 CV 1234",
        "Motion to continue",
        "Plaintiff requests a fourteen-day continuance to obtain records.",
    ]
    for i, line in enumerate(lines):
        draw.text((70, 100 + i * 65), line, fill="black", font_size=24)
    page.save(pdf, "PDF", resolution=144)
    evidence, metadata = extract_ensemble(pdf, "illinois", "\n".join(lines))
    (args.out / "ensemble.json").write_text(
        json.dumps({"evidence": evidence, "metadata": metadata}, indent=2) + "\n"
    )
    if (
        any(evidence.get(k) != v for k, v in expected.items())
        or metadata["manual_review"]
    ):
        raise RuntimeError("Ensemble controlled-value check failed")
    start = time.perf_counter()
    classified = chat_completion(
        system_message="Return only JSON with key choice. Allowed choices: motion, complaint. Select the document type.",
        user_message="Motion to continue. Plaintiff requests a continuance.",
        json_mode=True,
        model="us.openai.gpt-6-luna",
        max_output_tokens=128,
    )
    (args.out / "generic-classifier.json").write_text(
        json.dumps(
            {
                "response": classified,
                "seconds": time.perf_counter() - start,
                "provider": "bedrock",
            },
            indent=2,
        )
        + "\n"
    )
    if classified != {"choice": "motion"}:
        raise RuntimeError("Generic classifier route failed")
    print(
        json.dumps(
            {
                "passed": True,
                "models": [r["role"] for r in rows],
                "policy": metadata["policy"],
                "evidence_calls": len(metadata["calls"]),
                "estimated_evidence_usd": metadata["estimated_usd"],
            }
        )
    )


if __name__ == "__main__":
    main()
