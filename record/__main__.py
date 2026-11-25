"""Record every scene to tape/: `python -m record [--check]`. --check records again and fails if a tape would change."""

from __future__ import annotations

import importlib
import inspect
import json
import sys
from pathlib import Path

from record import virtual

virtual.install()

import argus_iris  # noqa: E402

from record.scenes import SCENES, SETUP, Scene  # noqa: E402
from record.stage import Stage  # noqa: E402

TAPE = Path(__file__).parent.parent / "tape"
REPO = "https://github.com/smk762/argus-iris/blob"


def code_link(spec: str) -> str:
    module, _, name = spec.partition(":")
    obj = importlib.import_module(module)
    for part in name.split("."):
        obj = getattr(obj, part)
    line = inspect.getsourcelines(obj)[1]
    return f"{REPO}/v{argus_iris.__version__}/src/{module.replace('.', '/')}.py#L{line}"


def record(scene: Scene, seed: int) -> dict:
    async def play() -> Stage:
        stage = Stage(**scene.config)
        if scene.key in SETUP:
            SETUP[scene.key](stage)
        await stage.open()
        await scene.play(stage)
        stage.recording = False
        return stage

    s = virtual.run(play, seed)
    end = max([s.t, *(e["t"] for e in s.events)])
    return {
        "key": scene.key, "title": scene.title, "lede": scene.lede, "why": scene.why,
        "code": [{"label": label, "url": code_link(spec)} for label, spec in scene.code],
        "iris": argus_iris.__version__, "start": (virtual.BASE.timestamp() + s.zero) * 1000, "duration": end,
        "http": s.http, "init": s.init, "events": s.events, "logs": s.logs, "beats": s.beats,
    }  # fmt: skip


def record_all() -> dict[str, dict]:
    return {scene.key: record(scene, i + 1) for i, scene in enumerate(SCENES)}


def dumps(tape: dict) -> str:
    return json.dumps(tape, separators=(",", ":"), ensure_ascii=False) + "\n"


def main() -> int:
    check = "--check" in sys.argv
    TAPE.mkdir(exist_ok=True)
    stale = []
    for key, tape in record_all().items():
        path, text = TAPE / f"{key}.json", dumps(tape)
        if check:
            if not path.exists() or path.read_text() != text:
                stale.append(key)
        else:
            path.write_text(text)
        print(
            f"{key}: {tape['duration']:.0f}s, {len(tape['events'])} events, {len(tape['beats'])} captions, {len(text) // 1024} KB"
        )
    if stale:
        print(f"stale tapes: {', '.join(stale)} (run `make tape`)", file=sys.stderr)
    return bool(stale)


if __name__ == "__main__":
    raise SystemExit(main())
