"""One scene's stage: the real broker and its HTTP API over a World, the event stream, the log, and the captions."""

from __future__ import annotations

import asyncio
import json

import httpx
import structlog
from argus_iris.broker.config import BrokerConfig
from argus_iris.broker.policy import Policy
from argus_iris.broker.state import Broker
from argus_iris.server.app import create_app

from record.virtual import BASE, elapsed
from record.world import World

NODES = {
    "rtx3090": {"agent_url": "http://rtx3090:9110", "reserve_mb": 3500},
    "rtx4060": {"agent_url": "http://rtx4060:9110"},
    "gtx1070": {"agent_url": "http://gtx1070:9110"},
}


def _engine(sid: str, node: str, port: int, container: str, **more) -> dict:
    return {
        "id": sid,
        "node": node,
        "adapter": "argus-http",
        "base_url": f"http://{node}:{port}",
        "container": container,
    } | more


SERVICES = [
    {
        "id": "comfyui",
        "node": "rtx3090",
        "adapter": "comfyui",
        "base_url": "http://rtx3090:8188",
        "container": "comfyui",
    },
    {
        "id": "ollama-rtx4060",
        "node": "rtx4060",
        "adapter": "ollama",
        "base_url": "http://rtx4060:11434",
        "container": "ollama",
    },
    {
        "id": "ollama-gtx1070",
        "node": "gtx1070",
        "adapter": "ollama",
        "base_url": "http://gtx1070:11434",
        "container": "ollama",
    },
    _engine("xtts", "rtx4060", 8141, "argus-echo-xtts"),
    _engine("whisper", "rtx4060", 8144, "argus-echo-whisper"),
    _engine("echo-xtts", "rtx3090", 8141, "argus-echo-xtts", serves=["xtts"]),
]
# argus-lens is not configured: it registers itself, and iris finds its container in the agent's inventory.
LENS = {
    "tenant": "argus-lens",
    "kinds": [{"kind": "caption", "vram_mb": 5400}],
    "residents": [{"name": "lens-captioner", "adapter": "argus-http", "container": "argus-lens", "port": 8100}],
}
POLICY = {"residents": [{"match": {"node": "gtx1070", "ollama": "nomic-embed-text:*"}, "keepalive": "always"}]}


class SceneError(AssertionError):
    pass


class Lease:
    """A client of the broker's HTTP API: asks, long-polls while queued, then heartbeats until released or killed."""

    def __init__(self, stage: Stage, **request) -> None:
        self.stage, self.request = stage, {"wait_s": 20} | request
        self.id: str | None = None
        self.progress: float | None = None
        self.label: str | None = None
        self.replies: list[dict] = []
        self._beats: asyncio.Task | None = None

    @property
    def record(self):
        # By tenant (one lease each per scene): the broker holds the POST while it is queued, so the id comes late.
        return next((r for r in self.stage.broker.leases.leases.values() if r.tenant == self.request["tenant"]), None)

    @property
    def state(self) -> str | None:
        return self.record.state if self.record else None

    async def run(self, *, beat: bool = True) -> Lease:
        r = await self.stage.api.post("/leases", json=self.request)
        r.raise_for_status()
        body = r.json()
        self.id = body["lease_id"]
        while body["state"] == "queued":
            r = await self.stage.api.get(f"/leases/{self.id}", params={"wait_s": 20})
            body = r.json()
        if body["state"] == "granted" and beat:
            self._beats = self.stage.spawn(self._heartbeat())
        return self

    async def _heartbeat(self) -> None:
        while True:
            body = {k: v for k, v in (("progress", self.progress), ("stage", self.label)) if v is not None}
            r = await self.stage.api.post(f"/leases/{self.id}/heartbeat", json=body)
            if r.status_code != 200:
                return
            self.replies.append(r.json())
            if r.json()["state"] != "granted":
                return
            await asyncio.sleep(20)

    def die(self) -> None:
        if self._beats:
            self._beats.cancel()

    async def release(self, outcome: str = "ok") -> None:
        self.die()
        r = await self.stage.api.post(f"/leases/{self.id}/release", json={"outcome": outcome})
        r.raise_for_status()


class Stage:
    def __init__(self, **config) -> None:
        self.world = World()
        self.events: list[dict] = []
        self.init: list[dict] = []
        self.logs: list[dict] = []
        self.beats: list[dict] = []
        self.http: dict[str, object] = {}
        self.zero = 0.0
        self._tasks: list[asyncio.Task] = []
        self.recording = False
        structlog.reset_defaults()
        structlog.configure(processors=[self._log], cache_logger_on_first_use=False)
        cfg = BrokerConfig.model_validate({"nodes": NODES, "services": SERVICES, "admission": "enforce"} | config)
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.world.handle))
        self.broker = Broker(cfg, client=client, policy=Policy.model_validate(POLICY))
        self.app = create_app(broker=self.broker, poll=False)
        self.api = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://broker:8120", timeout=None
        )

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.get_running_loop().create_task(coro)
        self._tasks.append(t)
        return t

    @property
    def t(self) -> float:
        return round(elapsed() - self.zero, 2)

    def _log(self, _logger, method, event) -> dict:
        if self.recording:
            row = {"t": self.t, "level": method} | {k: v for k, v in event.items() if v is not None}
            self.logs.append(json.loads(json.dumps(row, default=str)))
        raise structlog.DropEvent

    async def _collect(self) -> None:
        async with self.broker.subscribe() as queue:
            while True:
                kind, item = await queue.get()
                if self.recording:
                    self.events.append({"t": self.t, "e": kind, "d": json.loads(item.model_dump_json())})

    async def open(self) -> None:
        """Start the broker, let it settle, then start recording from what a fresh /events client would get."""
        self.spawn(self._collect())
        self.spawn(self.broker.run())
        (await self.api.post("/tenants", json=LENS)).raise_for_status()
        await asyncio.sleep(21)
        for path in ("health", "config", "services", "registry", "directory"):
            r = await self.api.get(f"/{path}")
            r.raise_for_status()
            self.http[path] = r.json()
        self.http["health"]["started_at"] = BASE.isoformat()
        self.zero, self.recording = elapsed(), True
        self.init = [{"e": "node", "d": json.loads(s.model_dump_json())} for s in self.broker.nodes.values()]

    def lease(self, **request) -> Lease:
        return Lease(self, **request)

    def note(self, title: str, text: str, focus: str | None = None) -> None:
        self.beats.append({"t": self.t, "title": title, "text": text, "focus": focus})

    async def until(self, what: str, test, timeout: float = 120) -> None:
        """Wait for the broker to do something. A scene whose story does not happen fails to record."""
        end = elapsed() + timeout
        while not test():
            if elapsed() > end:
                raise SceneError(f"{what}: did not happen within {timeout:g}s")
            await asyncio.sleep(0.25)

    def node(self, name: str):
        return self.broker.nodes[name]

    def logged(self, event: str, **fields) -> dict | None:
        return next(
            (r for r in self.logs if r["event"] == event and all(r.get(k) == v for k, v in fields.items())), None
        )
