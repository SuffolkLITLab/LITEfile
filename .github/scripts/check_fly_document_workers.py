"""Check that each document queue has a running Fly worker that auto-restarts."""

import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--autoscale", action="store_true", help="Validate staging's opt-in worker pool"
    )
    modes.add_argument(
        "--warm-transition",
        action="store_true",
        help="Verify workers are awake before leaving the pilot",
    )
    options = parser.parse_args()
    machines = json.load(sys.stdin)
    if options.autoscale:
        return check_autoscaling(machines)
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
                and (options.warm_transition or policy == "always")
            ):
                running.append(machine["id"])

        if running:
            print(f"{process_group} running: {', '.join(running)}")
        else:
            required_policy = (
                "" if options.warm_transition else " with restart policy 'always'"
            )
            print(f"::error::No {process_group} machine is started{required_policy}.")
            ready = False

    return 0 if ready else 1


def check_autoscaling(machines):
    groups = {}
    for machine in machines:
        config = machine["config"]
        group = (config.get("metadata") or {}).get("fly_process_group")
        policy = (config.get("restart") or {}).get("policy")
        if not config.get("standbys") and machine.get("host_status") in (None, "ok"):
            groups.setdefault(group, []).append((machine["state"], policy))
    coordinators = groups.get("document_worker_coordinator", [])
    if coordinators != [("started", "always")]:
        print(
            "::error::The staging autoscaling pilot requires one running coordinator with restart policy 'always'."
        )
        return 1
    for group in ("preparation_worker", "extraction_worker"):
        candidates = [
            state
            for state, policy in groups.get(group, [])
            if policy == "on-failure"
            and state in {"started", "stopped", "created", "suspended"}
        ]
        if len(candidates) < 2:
            print(
                f"::error::{group} needs two startable Machines with restart policy 'on-failure'."
            )
            return 1
    print(
        "Both staging document worker pools are startable and the coordinator is running."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
