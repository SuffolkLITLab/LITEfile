"""Bounded final preparation-capacity check on the local eight-core host."""

from followups import configure, conversion, root, run, scaled

if __name__ == "__main__":
    # Repeat the four-core burst with headroom for the synthetic latency proxy.
    run("scaled4-burst-proxy-headroom", "load.py", "--burst", "150")
    configure(conversion + scaled + ["-f", str(root / "compose-scaled6.yml")])
    run("scaled6-warmup", "load.py", "--burst", "6")
    run(
        "scaled6-interactive-4000h",
        "load.py",
        "--rate",
        "1.111111111",
        "--duration",
        "60",
        "--corpus",
        "interactive",
    )
    print("SIX-CORE COMPARISON COMPLETE", flush=True)
