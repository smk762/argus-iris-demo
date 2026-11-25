"""Virtual time: an event loop whose clock jumps to the next timer, and the patches that put argus-iris on it.

Nothing in argus-iris is replaced except where it reads the clock and where it draws random ids. `install()` must run
before any other argus_iris module is imported: they bind `utcnow` by name.
"""

from __future__ import annotations

import asyncio
import os
import random
import selectors
import sys
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

BASE = datetime(2026, 11, 25, 9, 0, tzinfo=UTC)


class _Selector(selectors.DefaultSelector):
    loop: VirtualLoop

    def select(self, timeout=None):
        ready = super().select(0)
        if ready or timeout == 0:
            return ready
        if timeout is None:
            raise RuntimeError("virtual loop has nothing left to wait for")
        self.loop.now += timeout
        return []


class VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(self) -> None:
        self.now = 0.0
        selector = _Selector()
        selector.loop = self
        super().__init__(selector)

    def time(self) -> float:
        return self.now


_current: VirtualLoop | None = None


def elapsed() -> float:
    return _current.now if _current else 0.0


def utcnow() -> datetime:
    return BASE + timedelta(seconds=round(elapsed(), 3))


def install() -> None:
    # Nothing from the recording machine: no tokens, no suite file, no state directory.
    for name in [n for n in os.environ if n.startswith(("ARGUS_", "IRIS_"))]:
        del os.environ[name]
    os.environ["ARGUS_ENV_FILE"] = "off"
    if "argus_iris.broker.state" in sys.modules:
        if sys.modules["argus_iris.models"].utcnow is not utcnow:
            raise RuntimeError("install() must run before argus_iris.broker is imported")
        return
    import argus_iris.models

    argus_iris.models.utcnow = utcnow
    import argus_iris.broker.actions
    import argus_iris.broker.leases

    argus_iris.broker.actions.time = SimpleNamespace(monotonic=elapsed)


def run(coro_fn, seed: int):
    """Run one scene on a fresh loop, with lease ids drawn from *seed*."""
    global _current
    import argus_iris.broker.leases as leases

    rng = random.Random(seed)
    leases.uuid = SimpleNamespace(uuid4=lambda: uuid.UUID(int=rng.getrandbits(128), version=4))
    loop = _current = VirtualLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro_fn())
    finally:
        tasks = asyncio.all_tasks(loop)
        for t in tasks:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
        _current = None
        asyncio.set_event_loop(None)
