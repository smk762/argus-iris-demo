"""Simulated GPU hosts: what the node agents and the services would answer over HTTP.

Only the far side of the broker's HTTP calls is simulated. Memory moves the way the real services move it: a load ramps
up, an Ollama unload leaves /api/ps at once but gives its memory back seconds later, an unloaded engine keeps a context.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx

from record.virtual import BASE, elapsed, utcnow

MIB = 1024 * 1024


@dataclass
class Proc:
    container: str
    service: str
    vram: int
    pid: int
    touched: float  # when it last did something
    busy: bool = False

    def held(self) -> int:
        """Working processes do not hold a perfectly still amount: the allocator moves a little under load."""
        return self.vram + (int(elapsed() * 7) % 48 if self.busy else 0)

    def idle(self) -> float:
        return 0.0 if self.busy else round(elapsed() - self.touched, 1)


@dataclass
class Host:
    name: str
    gpu: str
    total: int
    cc: str
    bf16: bool
    other_mb: int = 0  # used by things no process listing shows (a display)
    ram: tuple[int, int] = (65536, 41000)
    procs: dict[str, Proc] = field(default_factory=dict)
    containers: list[dict] = field(default_factory=list)
    used_ollama: dict[str, int] = field(default_factory=dict)  # loaded model -> MB
    pulled: list[str] = field(default_factory=list)

    def used(self) -> int:
        return self.other_mb + sum(p.held() for p in self.procs.values())


class World:
    def __init__(self) -> None:
        self._pid = 4000
        self.hosts = {
            "rtx3090": Host("rtx3090", "NVIDIA GeForce RTX 3090", 24576, "8.6", True, other_mb=620),
            "rtx4060": Host("rtx4060", "NVIDIA GeForce RTX 4060", 8192, "8.9", True, ram=(32768, 18200)),
            "gtx1070": Host("gtx1070", "NVIDIA GeForce GTX 1070 Ti", 8192, "6.1", False, ram=(32768, 24100)),
        }
        self.engines: dict[str, dict] = {}  # "host:port" -> {host, container, state, loaded_mb, residue}
        self.comfy_queue = 0
        self.calls: list[str] = []
        self._containers()

    # --- set-up -------------------------------------------------------------------------------------------------

    def _add(self, host: str, name: str, port: int, peer: str | None = None, service: str | None = None) -> None:
        h = self.hosts[host]
        cid = f"{len(h.containers) + 1:02d}{host.encode().hex()}".ljust(64, "0")[:64]
        labels = {"argus.peer": peer, "argus.service": service or peer} if peer else {}
        h.containers.append(
            {
                "id": cid, "name": name, "service": service or name, "project": "demo", "running": True,
                "managed": bool(peer), "labels": labels,
                "ports": [{"private": port, "public": port, "proto": "tcp", "ips": ["0.0.0.0"]}],
            }
        )  # fmt: skip

    def _containers(self) -> None:
        for host in self.hosts:
            self._add(host, "iris-agent", 9110, "iris-agent", "argus-iris")
        self._add("rtx3090", "comfyui", 8188)
        self._add("rtx3090", "argus-lens", 8100, "lens", "argus-lens")
        self._add("rtx3090", "argus-echo-xtts", 8141, "xtts", "argus-echo")
        self._add("rtx4060", "argus-echo-xtts", 8141, "xtts", "argus-echo")
        self._add("rtx4060", "argus-echo-whisper", 8144, "whisper", "argus-echo")
        self._add("rtx4060", "ollama", 11434)
        self._add("gtx1070", "ollama", 11434)
        self.engine("rtx3090", 8141, "argus-echo-xtts", loaded_mb=1960, residue=146)
        self.engine("rtx3090", 8100, "argus-lens", loaded_mb=5400, residue=0)
        self.engine("rtx4060", 8141, "argus-echo-xtts", loaded_mb=1960, residue=146)
        self.engine("rtx4060", 8144, "argus-echo-whisper", loaded_mb=980, residue=0)
        self.proc("rtx3090", "comfyui", 410, idle=1500)
        self.hosts["rtx4060"].pulled = ["qwen3:8b", "gemma3:4b"]
        self.hosts["gtx1070"].pulled = ["qwen3:8b", "nomic-embed-text:latest"]
        self.load_now("gtx1070", "nomic-embed-text:latest", 610, idle=340)

    def engine(self, host: str, port: int, container: str, *, loaded_mb: int, residue: int) -> None:
        self.engines[f"{host}:{port}"] = {
            "host": host, "container": container, "state": "unloaded", "loaded_mb": loaded_mb, "residue": residue,
        }  # fmt: skip

    def proc(self, host: str, container: str, vram: int, *, idle: float = 0, service: str | None = None) -> Proc:
        self._pid += 1
        p = Proc(container, service or container, vram, self._pid, elapsed() - idle)
        self.hosts[host].procs[container] = p
        return p

    def engine_up(self, host: str, container: str, *, idle: float = 0, busy: bool = False) -> None:
        e = next(e for e in self.engines.values() if e["host"] == host and e["container"] == container)
        e["state"] = "gpu"
        self.proc(host, container, e["loaded_mb"], idle=idle).busy = busy

    def load_now(self, host: str, model: str, mb: int, *, idle: float = 0) -> None:
        h = self.hosts[host]
        h.used_ollama[model] = mb
        have = h.procs.get("ollama")
        if have:
            have.vram += mb
            have.touched = elapsed() - idle
        else:
            self.proc(host, "ollama", mb, idle=idle)

    # --- things that take time ------------------------------------------------------------------------------------

    async def ramp(self, host: str, container: str, to: int, seconds: float, *, service: str | None = None) -> Proc:
        """A process's memory moving to *to*, in steps, as a model loads or an allocator grows."""
        p = self.hosts[host].procs.get(container) or self.proc(host, container, 0, service=service)
        start, steps = p.vram, max(1, int(seconds))
        for i in range(1, steps + 1):
            await asyncio.sleep(seconds / steps)
            if self.hosts[host].procs.get(container) is not p:
                return p  # killed while loading
            p.vram = start + (to - start) * i // steps
            p.touched = elapsed()
        return p

    def kill(self, host: str, container: str) -> None:
        self.hosts[host].procs.pop(container, None)

    async def ollama_load(self, host: str, model: str, mb: int, seconds: float = 8) -> None:
        h = self.hosts[host]
        have = h.procs["ollama"].vram if "ollama" in h.procs else 0
        await self.ramp(host, "ollama", have + mb, seconds)
        h.used_ollama[model] = mb
        h.procs["ollama"].busy = True

    async def _ollama_free(self, host: str, mb: int) -> None:
        await asyncio.sleep(6)  # the runner exits after Ollama has already stopped listing the model
        p = self.hosts[host].procs.get("ollama")
        if p:
            p.vram -= mb
            p.touched = elapsed()
            if p.vram <= 0:
                self.kill(host, "ollama")

    async def _later(self, seconds: float, fn) -> None:
        await asyncio.sleep(seconds)
        fn()

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks = [*getattr(self, "_tasks", []), task]

    # --- HTTP -----------------------------------------------------------------------------------------------------

    def snapshot(self, h: Host) -> dict:
        used = h.used()
        busy = any(p.busy for p in h.procs.values())
        return {
            "version": "1", "node": h.name, "taken_at": utcnow().isoformat(), "agent_version": "0.2.1",
            "gpus": [{"index": 0, "name": h.gpu, "uuid": f"GPU-{h.name}", "total_mb": h.total, "free_mb": h.total - used,
                      "used_mb": used, "util_pct": 87 if busy else 0, "compute_capability": h.cc, "bf16": h.bf16}],
            "processes": [{"pid": p.pid, "gpu": 0, "vram_mb": p.held(), "container": p.container, "container_id": None,
                           "service": p.service, "first_seen": BASE.isoformat(), "idle_s": p.idle()}
                          for p in h.procs.values() if p.vram > 0],
            "host": {"total_mb": h.ram[0], "available_mb": h.ram[1]},
            "containers": [], "inventory": h.containers,
        }  # fmt: skip

    async def handle(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        host, port, path = request.url.host, request.url.port, request.url.path
        h = self.hosts.get(host)
        if h is None:
            raise httpx.ConnectError(f"no such host {host}", request=request)
        if request.method == "POST":
            self.calls.append(f"POST {host}:{port}{path}")
        if port == 9110:
            if path == "/snapshot":
                return httpx.Response(200, json=self.snapshot(h))
            return self._service_health("argus-iris")
        if port == 11434:
            return self._ollama(h, request)
        if port == 8188:
            return self._comfy(h, request)
        return self._engine(h, f"{host}:{port}", request)

    def _service_health(self, service: str, **extra) -> httpx.Response:
        return httpx.Response(200, json={"service": service, "version": "simulated", "status": "ok",
                                         "started_at": BASE.isoformat(), "wire": {}, "checks": {}, **extra})  # fmt: skip

    def _ollama(self, h: Host, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in h.pulled]})
        if path == "/api/ps":
            models = [{"name": m, "size": mb * MIB, "size_vram": mb * MIB, "context_length": 8192}
                      for m, mb in h.used_ollama.items()]  # fmt: skip
            return httpx.Response(200, json={"models": models})
        if path == "/api/generate":
            body = json.loads(request.content)
            if body.get("keep_alive") == 0 and (mb := h.used_ollama.pop(body["model"], None)):
                self._spawn(self._ollama_free(h.name, mb))
            return httpx.Response(200, json={"done": True})
        return httpx.Response(404)

    def _comfy(self, h: Host, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/queue":
            return httpx.Response(200, json={"queue_running": [1] * min(1, self.comfy_queue),
                                             "queue_pending": [1] * max(0, self.comfy_queue - 1)})  # fmt: skip
        if path == "/system_stats":
            return httpx.Response(200, json={"system": {}, "devices": []})
        if path == "/free":
            p = h.procs["comfyui"]

            def freed() -> None:
                p.vram, p.touched = 410, elapsed()

            self._spawn(self._later(4, freed))
            return httpx.Response(200)
        return httpx.Response(404)

    def _engine(self, h: Host, key: str, request: httpx.Request) -> httpx.Response:
        e, path = self.engines.get(key), request.url.path
        if e is None:
            raise httpx.ConnectError("connection refused", request=request)
        p = h.procs.get(e["container"])
        if path == "/health":
            service = next(c["labels"].get("argus.service", "") for c in h.containers if c["name"] == e["container"])
            return self._service_health(
                service, model_state=e["state"], busy=bool(p and p.busy), idle_s=p.idle() if p and not p.busy else None
            )
        if path == "/admin/unload":
            e["state"] = "unloaded"

            def freed() -> None:
                if p:
                    p.vram, p.touched = e["residue"], elapsed()

            self._spawn(self._later(3, freed))
            return httpx.Response(200, json={"status": "ok", "model_state": "unloaded", "unloaded": True})
        return httpx.Response(404)
