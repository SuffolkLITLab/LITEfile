"""Check that each document queue has a running Fly worker that auto-restarts."""

import json
import sys


def main():
    machines = json.load(sys.stdin)
    ready = True
    for process_group in ("preparation_worker", "extraction_worker"):
        running = []
        for machine in machines:
            config = machine["config"]
            group = (config.get("metadata") or {}).get("fly_process_group")
            policy = (config.get("restart") or {}).get("policy")
            if (
                group == process_group
                and machine["state"] == "started"
                and policy == "always"
            ):
                running.append(machine["id"])

        if running:
            print(
                f"{process_group} running with automatic restart: {', '.join(running)}"
            )
        else:
            print(
                f"::error::No {process_group} machine is started with restart policy 'always'."
            )
            ready = False

    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
