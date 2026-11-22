"""Shut background workers down the same way under Fly's stop signal (#445)."""

import signal


def stop_on_sigterm() -> None:
    """Treat SIGTERM like SIGINT: raise KeyboardInterrupt in the main thread.

    Fly stops machines with SIGTERM so Gunicorn can finish in-flight court
    submissions. Python's default SIGTERM action exits at once, skipping the
    ``finally`` blocks that stop a worker's child process and release the job it
    claimed; the job would then wait for its lease to expire. Raising
    KeyboardInterrupt keeps the cleanup that SIGINT, Fly's former stop signal,
    always ran. Children are started with ``spawn`` and keep default handlers,
    so ``child.terminate()`` still stops them immediately.
    """
    signal.signal(signal.SIGTERM, signal.default_int_handler)
