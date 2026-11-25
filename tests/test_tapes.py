"""The tapes are what the published broker does, and nothing from the machine that recorded them."""

from __future__ import annotations

import json
import re

import pytest

from record.__main__ import TAPE, dumps, record_all

KEYS = ["contention", "eviction", "spill", "crash", "stall"]


@pytest.fixture(scope="module")
def fresh() -> dict[str, dict]:
    return record_all()  # raises if any scene's story did not happen


@pytest.mark.parametrize("key", KEYS)
def test_committed_tape_is_what_the_broker_does(fresh, key):
    assert (TAPE / f"{key}.json").read_text() == dumps(fresh[key])


def test_two_runs_agree(fresh):
    again = record_all()
    assert {k: dumps(v) for k, v in again.items()} == {k: dumps(v) for k, v in fresh.items()}


@pytest.mark.parametrize("key", KEYS)
def test_captions_and_events_are_in_order(key):
    tape = json.loads((TAPE / f"{key}.json").read_text())
    for rows in (tape["events"], tape["logs"], tape["beats"]):
        times = [r["t"] for r in rows]
        assert times == sorted(times) and times[0] >= 0 and times[-1] <= tape["duration"]
    assert len({b["t"] for b in tape["beats"]}) == len(tape["beats"])  # each caption is its own step


def test_nothing_private_in_the_tapes():
    text = "".join(p.read_text() for p in TAPE.glob("*.json"))
    assert not re.search(r"\b(192\.168|10\.\d+|172\.(1[6-9]|2\d|3[01]))\.\d+", text)
    assert not re.search(r"/home/|[\w.]+@[\w-]+\.\w+|Bearer ", text)


def test_memory_never_exceeds_the_card():
    for path in TAPE.glob("*.json"):
        tape = json.loads(path.read_text())
        for e in tape["init"] + tape["events"]:
            if e["e"] == "node" and e["d"]["gpu"]:
                assert 0 <= e["d"]["gpu"]["free_mb"] <= e["d"]["gpu"]["total_mb"], (path.name, e.get("t"))


def test_a_slow_machine_records_the_same(fresh, monkeypatch):
    import time

    real, calls = time.perf_counter, iter(range(1, 10**9))
    monkeypatch.setattr(time, "perf_counter", lambda: real() + next(calls))  # every real reading a second apart
    assert {k: dumps(v) for k, v in record_all().items()} == {k: dumps(v) for k, v in fresh.items()}
