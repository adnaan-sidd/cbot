"""
Shared Twisted reactor lifecycle for cTrader connections.

Found via testing, not by inspection: Twisted's `reactor` is a
process-wide singleton — `reactor.run()` can only be called once per
process, ever. The first versions of `CTraderExecutor.connect()` and
`CTraderFeed.connect()` each spawned their OWN background thread calling
`reactor.run()`, which crashes with `ReactorAlreadyRunning` the moment
both a feed and an executor exist in the same process (exactly the
normal case — a live/paper trading run needs both). This module makes
starting the reactor idempotent and shared, so any number of
CTraderExecutor/CTraderFeed instances coexist safely.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_started = False


def ensure_reactor_running() -> None:
    """Starts the Twisted reactor in a background thread on first call;
    every subsequent call (from any CTraderExecutor/CTraderFeed
    instance) is a no-op. Thread-safe."""
    global _started
    with _lock:
        if _started:
            return
        from twisted.internet import reactor

        thread = threading.Thread(
            target=lambda: reactor.run(installSignalHandlers=False), daemon=True
        )
        thread.start()
        _started = True
