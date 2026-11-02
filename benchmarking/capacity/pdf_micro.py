# ruff: noqa: E402
"""Compare preparation CPU with fixed converter responses, including byte equality."""

import argparse
import hashlib
import importlib.util
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import django
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings

django.setup()
from efile.services import document_preparation as candidate

parser = argparse.ArgumentParser()
parser.add_argument(
    "--baseline-ref", default="0ff456b8247ed5459d9cab3f426955d09572cbda"
)
parser.add_argument("--output", required=True)
parser.add_argument("--candidate-path")
args = parser.parse_args()
if args.candidate_path:
    candidate_spec = importlib.util.spec_from_file_location(
        "preparation_candidate", args.candidate_path
    )
    candidate = importlib.util.module_from_spec(candidate_spec)
    sys.modules[candidate_spec.name] = candidate
    candidate_spec.loader.exec_module(candidate)
root = Path(
    subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip()
)
source = subprocess.check_output(
    [
        "git",
        "show",
        f"{args.baseline_ref}:efile_app/efile/services/document_preparation.py",
    ],
    text=True,
)
with tempfile.TemporaryDirectory() as tmp:
    path = Path(tmp) / "baseline.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("preparation_baseline", path)
    baseline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = baseline
    spec.loader.exec_module(baseline)
    rows = []
    for corpus in ("flattened", "interactive"):
        for index in (1, 2, 3):
            name = f"IL-0{index}.pdf"
            data = (
                root / "benchmarking/synthetic/filled_pdfs" / corpus / name
            ).read_bytes()
            responses = []
            actual = baseline._gotenberg

            def capture(*args, **kwargs):
                value = actual(*args, **kwargs)
                responses.append(value)
                return value

            with (
                override_settings(
                    GOTENBERG_URL="http://127.0.0.1:8300",
                    GOTENBERG_USERNAME="",
                    GOTENBERG_PASSWORD="",
                ),
                patch.object(baseline, "_gotenberg", side_effect=capture),
            ):
                baseline.prepare_document(SimpleUploadedFile(name, data), "illinois")
            assert len(responses) <= 1
            converted = responses[0] if responses else data
            times = {"baseline": [], "candidate": []}
            counts = {}
            outputs = {}
            converter_inputs = {}
            for repeat in range(7):
                order = [("baseline", baseline), ("candidate", candidate)]
                if repeat % 2:
                    order.reverse()
                for label, module in order:
                    with (
                        patch.object(
                            module, "_gotenberg", return_value=converted
                        ) as converter,
                        patch.object(
                            module, "inspect_pdf", wraps=module.inspect_pdf
                        ) as inspect,
                    ):
                        started = time.process_time()
                        result = module.prepare_document(
                            SimpleUploadedFile(name, data), "illinois"
                        )
                        elapsed = time.process_time() - started
                    if repeat:
                        times[label].append(elapsed)
                    counts[label] = inspect.call_count
                    outputs[label] = result
                    converter_inputs[label] = [
                        {
                            "sha256": hashlib.sha256(call.args[0]).hexdigest(),
                            "arguments": list(call.args[1:]),
                            "kwargs": call.kwargs,
                        }
                        for call in converter.call_args_list
                    ]
            assert converter_inputs["baseline"] == converter_inputs["candidate"]
            a, b = outputs["baseline"], outputs["candidate"]
            assert (a.content, a.filename, a.operation, a.upload_has_form_fields) == (
                b.content,
                b.filename,
                b.operation,
                b.upload_has_form_fields,
            )
            rows.append(
                {
                    "corpus": corpus,
                    "name": name,
                    "bytes": len(data),
                    "identical_output": True,
                    "identical_converter_requests": True,
                    "inspection_count": counts,
                    "cpu_seconds": times,
                    "median_cpu_seconds": {
                        label: statistics.median(t) for label, t in times.items()
                    },
                }
            )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(
        json.dumps(
            {
                "baseline_ref": args.baseline_ref,
                "candidate_sha256": hashlib.sha256(
                    Path(candidate.__file__).read_bytes()
                ).hexdigest(),
                "converter": "one real local Gotenberg response per input, then fixed responses for CPU comparison",
                "rounds": 6,
                "alternating_order": True,
                "results": rows,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps(rows, indent=2))
