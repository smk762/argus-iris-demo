"""The five scenes. Captions are raised only after the broker has done what they say (Stage.until)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from record.stage import Stage
from record.virtual import utcnow


def gb(mb: int) -> str:
    return f"{mb / 1024:.1f} GB"


@dataclass
class Scene:
    key: str
    title: str
    lede: str
    why: str
    play: Any
    code: list[tuple[str, str]]  # (label, "module:qualname") resolved to a line in the tagged source
    config: dict = field(default_factory=dict)


# ── 1 ────────────────────────────────────────────────────────────────────────


async def contention(s: Stage) -> None:
    proof = s.lease(tenant="argus-proof", kind="run", vram_mb=14000, priority="batch", preemptible=True,
                    constraints={"node": "rtx3090"}, caller="evaluate a trained LoRA")  # fmt: skip
    await asyncio.sleep(4)
    await proof.run()
    s.note("A batch job takes most of the card",
           f"argus-proof asks for {gb(14000)} on the RTX 3090 and gets it at once. This is a reservation: the model has "
           "not loaded yet, but nobody else can be promised that memory.", "rtx3090")  # fmt: skip
    proof.label, proof.progress = "loading", 0.0
    await s.world.ramp("rtx3090", "argus-proof", 13200, 10)
    s.world.hosts["rtx3090"].procs["argus-proof"].busy = True
    proof.label, proof.progress = "sample grid", 0.2
    await asyncio.sleep(8)

    forge = s.lease(tenant="argus-forge", kind="train", vram_mb=16000, priority="background",
                    constraints={"node": "rtx3090"}, caller="train a LoRA")  # fmt: skip
    s.spawn(forge.run())
    await s.until("forge queues", lambda: forge.state == "queued" and forge.record.reason)
    s.note("The next one does not fit, so it waits",
           f"argus-forge wants {gb(16000)}. Without a broker it would start, and one of the two would likely run out of memory. "
           f"iris queues it and says why: “{forge.record.reason}”.", "rtx3090")  # fmt: skip
    await asyncio.sleep(16)

    curator = s.lease(tenant="argus-curator", kind="faces", vram_mb=5000, priority="interactive",
                      constraints={"node": "rtx3090"}, caller="a person is waiting")  # fmt: skip
    await curator.run()
    if forge.state != "queued" or curator.state != "granted":
        raise AssertionError("curator should pass forge")
    s.note("A person does not wait behind a training run",
           "argus-curator is interactive and fits beside the batch job, so it is granted straight away, ahead of the "
           "background job that asked first. Queued jobs gain a class every five minutes, so the training run cannot starve.",
           "rtx3090")  # fmt: skip
    await s.world.ramp("rtx3090", "argus-curator", 4700, 5)
    proof.progress = 0.6
    await asyncio.sleep(22)
    s.world.kill("rtx3090", "argus-curator")
    await curator.release()
    proof.progress = 0.9
    await asyncio.sleep(20)
    s.world.kill("rtx3090", "argus-proof")
    await proof.release()
    await s.until("forge is granted", lambda: forge.state == "granted")
    waited = round((forge.record.granted_at - forge.record.created_at).total_seconds())
    s.note("Released, and the queue moves",
           f"proof finishes and gives its lease back. Once the agent sees the memory free, forge is granted, "
           f"{waited} seconds after it asked.", "rtx3090")  # fmt: skip
    await s.world.ramp("rtx3090", "argus-forge", 15400, 12)
    s.world.hosts["rtx3090"].procs["argus-forge"].busy = True
    forge.label, forge.progress = "step 40 of 2000", 0.02
    await asyncio.sleep(25)


# ── 2 ────────────────────────────────────────────────────────────────────────


async def eviction(s: Stage) -> None:
    await asyncio.sleep(4)
    chat = s.lease(tenant="chat", kind="generate", target="ollama:gemma3:4b", vram_mb=4500, model_mb=3800,
                   priority="interactive", caller="one chat reply")  # fmt: skip
    s.note("Eight gigabytes, all spoken for",
           "On the RTX 4060, Ollama still holds a language model nobody has used for 22 minutes, XTTS is speaking, and "
           "Whisper is loaded. A chat request now needs a different model.", "rtx4060")  # fmt: skip
    await asyncio.sleep(9)
    s.spawn(chat.run())
    await s.until("an eviction plan", lambda: s.logged("admission_evicting"))
    plan = s.logged("admission_evicting")
    if plan["residents"] != ["rtx4060/ollama-rtx4060/qwen3:8b"]:
        raise AssertionError(plan)
    s.note("Pick the fewest, idlest victims",
           f"The request is {gb(plan['short_mb'])} short. XTTS is busy, so it is never touched. The idle qwen3:8b alone "
           "covers it, so Whisper stays loaded too.", "rtx4060")  # fmt: skip
    await asyncio.sleep(1.5)
    ollama = lambda: next(x for x in s.node("rtx4060").residents if x.service == "ollama-rtx4060")  # noqa: E731
    await s.until("Ollama stops listing it", lambda: ollama().model is None)
    if chat.state != "queued":
        raise AssertionError("granted on the service's word")
    s.note("The service says it is gone. The GPU disagrees.",
           f"Ollama has already dropped qwen3 from its list, but NVML still shows {gb(ollama().vram_mb)} held. Granting "
           "now would run straight into an out-of-memory error, so the request stays queued.", "rtx4060")  # fmt: skip
    await s.until("the eviction is confirmed", lambda: s.logged("resident_evicted", confirmed=True))
    done = s.logged("resident_evicted", confirmed=True)
    await s.until("chat is granted", lambda: chat.state == "granted")
    s.note("Confirmed by the GPU, then granted",
           f"After {done['waited_s']:g} seconds the card's free memory really rises. Only then does the chat request get "
           "its lease.", "rtx4060")  # fmt: skip
    await s.world.ollama_load("rtx4060", "gemma3:4b", 3800)
    await asyncio.sleep(14)
    s.world.hosts["rtx4060"].procs["ollama"].busy = False
    s.world.hosts["rtx4060"].procs["ollama"].touched = s.zero + s.t
    await chat.release()
    s.note("The model stays for the next request",
           "The lease ends, but gemma3 stays loaded until something needs the room. With no "
           "idle rule set for it, iris evicts a model only to make room.", "rtx4060")  # fmt: skip
    await asyncio.sleep(12)


def eviction_setup(s: Stage) -> None:
    s.world.load_now("rtx4060", "qwen3:8b", 4600, idle=1320)
    s.world.engine_up("rtx4060", "argus-echo-xtts", busy=True)
    s.world.engine_up("rtx4060", "argus-echo-whisper", idle=400)


# ── 3 ────────────────────────────────────────────────────────────────────────


async def spill(s: Stage) -> None:
    await asyncio.sleep(4)
    chat = s.lease(tenant="chat", kind="generate", target="ollama:qwen3:8b", vram_mb=6000, model_mb=5200,
                   priority="interactive", caller="long answer")  # fmt: skip
    await chat.run()
    if chat.record.node != "rtx4060":
        raise AssertionError(chat.record)
    s.note("The first choice gets the work",
           "A chat request names a model, not a machine. Both small cards have qwen3:8b on disk, so configuration order "
           "decides: the RTX 4060.", "rtx4060")  # fmt: skip
    await s.world.ollama_load("rtx4060", "qwen3:8b", 5200, 9)
    await asyncio.sleep(10)
    second = s.lease(tenant="rag-ingest", kind="summarise", target="ollama:qwen3:8b", vram_mb=6000, model_mb=5200,
                     priority="batch", caller="index new documents")  # fmt: skip
    await second.run()
    if second.record.node != "gtx1070":
        raise AssertionError(second.record)
    s.note("Same model, second machine",
           "A second job asks for the same model. The RTX 4060 is already serving one lease, so this one goes to the GTX "
           "1070 Ti: loading a model takes seconds, waiting behind a job can take minutes.", "gtx1070")  # fmt: skip
    s.spawn(s.world.ollama_load("gtx1070", "qwen3:8b", 5200, 9))
    await asyncio.sleep(16)
    speak = s.lease(tenant="argus-mask", kind="voice", target="xtts", vram_mb=2000, priority="interactive",
                    caller="speak a line")  # fmt: skip
    await speak.run()
    if speak.record.node != "rtx3090":
        raise AssertionError(speak.record)
    s.note("No room here, so it spills",
           f"Speech normally runs on the RTX 4060, but a lease is using that card. echo-xtts on the RTX 3090 declares that "
           f"it serves the same target, so the grant comes back with its address: {speak.record.base_url}. The caller "
           "never had to know.", "rtx3090")  # fmt: skip
    e = s.world.engines["rtx3090:8141"]
    e["state"] = "gpu"
    await s.world.ramp("rtx3090", "argus-echo-xtts", 1960, 5)
    s.world.hosts["rtx3090"].procs["argus-echo-xtts"].busy = True
    await asyncio.sleep(22)
    s.world.hosts["rtx3090"].procs["argus-echo-xtts"].busy = False
    s.world.hosts["rtx3090"].procs["argus-echo-xtts"].touched = s.zero + s.t
    await speak.release()
    s.note("Three cards, one queue",
           "Three callers, three different GPUs, and none of them picked a host.", None)  # fmt: skip
    await asyncio.sleep(14)


# ── 4 ────────────────────────────────────────────────────────────────────────


async def crash(s: Stage) -> None:
    await asyncio.sleep(4)
    curator = s.lease(tenant="argus-curator", kind="scan", vram_mb=12000, priority="batch",
                      constraints={"node": "rtx3090"}, caller="scan a folder")  # fmt: skip
    await curator.run()
    curator.label, curator.progress = "embedding", 0.1
    s.note("A normal job, checking in",
           "argus-curator holds a lease and sends a heartbeat every 20 seconds. Each one extends the lease by its "
           "60-second lifetime.", "rtx3090")  # fmt: skip
    await s.world.ramp("rtx3090", "argus-curator", 11300, 8)
    s.world.hosts["rtx3090"].procs["argus-curator"].busy = True
    curator.progress = 0.3
    await asyncio.sleep(14)
    proof = s.lease(tenant="argus-proof", kind="run", vram_mb=14000, priority="batch", constraints={"node": "rtx3090"},
                    caller="evaluate a trained LoRA")  # fmt: skip
    s.spawn(proof.run())
    await s.until("proof queues", lambda: proof.state == "queued")
    await asyncio.sleep(10)
    curator.die()
    s.world.kill("rtx3090", "argus-curator")
    killed = utcnow()
    last = curator.record.last_heartbeat
    s.note("The client is killed",
           "No release, no goodbye. The process is gone and its memory is free, but the reservation stands: a missed "
           "heartbeat could also be a slow network.", "rtx3090")  # fmt: skip
    await s.until("curator expires", lambda: curator.state == "expired")
    ended = round((curator.record.ended_at - last).total_seconds())
    s.note("Silence has a limit",
           f"{ended} seconds after the last heartbeat the lease expires: “{curator.record.reason}”. Nobody had to notice, "
           "log in or clean up.", "rtx3090")  # fmt: skip
    await s.until("proof granted", lambda: proof.state == "granted")
    await asyncio.sleep(3)
    s.note("The next job starts by itself",
           f"argus-proof is granted {round((proof.record.granted_at - killed).total_seconds())} seconds after the crash.", "rtx3090")  # fmt: skip
    await s.world.ramp("rtx3090", "argus-proof", 13200, 8)
    proof.label, proof.progress = "sample grid", 0.1
    await asyncio.sleep(14)
    ghost = s.lease(tenant="script", kind="adhoc", vram_mb=3000, priority="batch", constraints={"node": "gtx1070"},
                    caller="forgot to heartbeat")  # fmt: skip
    await ghost.run(beat=False)
    await s.until("ghost is reclaimed", lambda: ghost.state == "expired")
    held = round((ghost.record.ended_at - ghost.record.granted_at).total_seconds())
    s.note("A grant nobody picks up",
           f"A script on the GTX 1070 Ti took a lease and never sent a first heartbeat. It is taken back after {held} "
           "seconds rather than 60.", "gtx1070")  # fmt: skip
    await asyncio.sleep(10)


# ── 5 ────────────────────────────────────────────────────────────────────────


async def stall(s: Stage) -> None:
    await asyncio.sleep(4)
    proof = s.lease(tenant="argus-proof", kind="eval", target="comfyui", vram_mb=17000, priority="batch",
                    caller="proof-eval keepalive")  # fmt: skip
    await proof.run()
    s.note("A big job on ComfyUI", f"An evaluation run reserves {gb(17000)} and starts rendering.", "rtx3090")
    s.world.comfy_queue = 3
    comfy = s.world.hosts["rtx3090"].procs["comfyui"]
    comfy.busy = True
    await s.world.ramp("rtx3090", "comfyui", 12400, 15)
    await asyncio.sleep(160)
    s.world.comfy_queue, comfy.busy, comfy.touched = 0, False, s.zero + s.t
    await asyncio.sleep(10)
    s.note("The work stops. The heartbeats do not.",
           "The job died, but the loop that sends its heartbeats lives on. To a simple broker this lease looks perfectly "
           "healthy. iris also watches the GPU, and ComfyUI's queue is empty.", "rtx3090")  # fmt: skip
    await s.until("the lease stalls", lambda: proof.record.stalled_at is not None, 2400)
    quiet = round((proof.record.stalled_at - proof.record.active_at).total_seconds() / 60)
    s.note("Flagged as stalled",
           f"After {quiet} minutes with no sign of GPU work, the lease is marked stalled. Nothing is taken away yet: "
           "nobody else wants the card.", "rtx3090")  # fmt: skip
    await asyncio.sleep(120)
    atlas = s.lease(tenant="argus-atlas", kind="bake", target="comfyui", vram_mb=16000, priority="batch",
                    caller="bake map tiles")  # fmt: skip
    s.spawn(atlas.run())
    await s.until("a yield is requested", lambda: proof.record.yield_requested)
    await s.until("the client hears it", lambda: proof.replies[-1]["yield_requested"])
    s.note("Now it is in the way, so it is asked to leave",
           "argus-atlas queues behind the stalled lease. Every heartbeat reply now carries yield_requested. A live client "
           "would let go at its next safe point. iris does not kill running GPU work.", "rtx3090")  # fmt: skip
    await s.until("it is reclaimed", lambda: proof.state == "refused", 1200)
    s.note("Asked for 15 minutes, then reclaimed",
           f"The request was ignored, so the reservation ends: “{proof.record.reason}”. Resuming that lease is refused "
           "too, so a zombie loop cannot take it back.", "rtx3090")  # fmt: skip
    await s.until("atlas granted", lambda: atlas.state == "granted", 120)
    if not s.logged("resident_evicted", service="comfyui", confirmed=True):
        raise AssertionError("ComfyUI's cached model should have been evicted")
    s.note("And the card goes back to work",
           "iris unloads the model ComfyUI had cached, waits for the GPU to confirm, and grants the bake. In the real "
           "incident behind this scene, a dead job's 17 GB lease kept beating for 1 h 38 min and blocked a map bake. That is why the watchdog exists.", "rtx3090")  # fmt: skip
    s.world.comfy_queue, comfy.busy = 4, True
    await s.world.ramp("rtx3090", "comfyui", 11800, 12)
    atlas.label, atlas.progress = "tile 3 of 64", 0.05
    await asyncio.sleep(30)


SCENES = [
    Scene("contention", "Two jobs, one card", "Two jobs want more memory than the card has.",
          "A free-memory check is a snapshot: two jobs can both pass it, then both load. iris keeps a reservation ledger "
          "per card, so it does not grant leases that add up to more than the card, and it checks NVML as well for "
          "anything it was not told about.",
          contention, [("Admission", "argus_iris.broker.leases:LeaseBook._admit"), ("The fit test", "argus_iris.broker.leases:_fit"),
                       ("Queue order and aging", "argus_iris.broker.leases:LeaseBook._queue")]),
    Scene("eviction", "Trust the GPU", "Making room, and proving it.",
          "A service saying “unloaded” is a claim. Free memory on the card is a fact. An eviction counts only when the "
          "GPU's free memory has risen by at least half of what the model held.",
          eviction, [("Choosing victims", "argus_iris.broker.leases:LeaseBook._plan"), ("Confirmation by NVML", "argus_iris.broker.actions:_freed"),
                     ("Evict and wait", "argus_iris.broker.actions:evict_resident")]),
    Scene("spill", "Anywhere it fits", "Callers name a job, not a machine.",
          "Hard-coding hosts means idle cards beside overloaded ones. A lease names a target and the grant returns the "
          "address, so placement can change without touching a single caller.",
          spill, [("Placement order", "argus_iris.broker.leases:candidates"), ("Granting", "argus_iris.broker.leases:LeaseBook._grant")]),
    Scene("crash", "When a client dies", "Nobody cleans up, and it does not matter.",
          "Anything that needs a clean shutdown to stay correct will break. Leases expire on their own, so a crash costs "
          "a minute of waiting rather than a manual fix.",
          crash, [("Expiry", "argus_iris.broker.leases:LeaseBook.tick"), ("Heartbeats", "argus_iris.broker.leases:LeaseBook.heartbeat")]),
    Scene("stall", "Alive, but not working", "A heartbeat proves the client is alive, not that it works.",
          "Preemption is cooperative. iris does not kill running GPU work: it asks first, waits, "
          "and only then withdraws the reservation. Reclaiming is off by default; this recording turns it on.",
          stall, [("The stall watchdog", "argus_iris.broker.leases:LeaseBook._watch"), ("What counts as working", "argus_iris.broker.leases:LeaseBook._working")],
          {"lease_stall_reclaim": True}),
]  # fmt: skip
SETUP = {"eviction": eviction_setup}
