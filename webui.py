#!/usr/bin/env python3
"""
NEXUS web backend — JARVIS command deck API + WebSocket push.

Run:  .venv\\Scripts\\python.exe main.py --web [--host 127.0.0.1] [--port 8777]
Open: http://127.0.0.1:8777/
"""

import asyncio
import json
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import main as nexus
from main import CFG, LOG_FILE, LLMProvider, NexusAgent, SessionManager, get_log_tail, log_event

WEB_DIR = Path(__file__).parent / "web"


def _safe(obj: Any) -> Any:
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        try:
            return {"raw": repr(obj)[:2000]}
        except Exception:
            return {"raw": "?"}


class Hub:
    """Fan-out of agent events to websocket clients + replay buffer."""
    def __init__(self, keep: int = 200):
        self.clients: set = set()
        self.keep = keep
        self.history: List[dict] = []

    def listener(self, event: str, data: dict, keep: bool = True):
        env = {"type": event, "ts": datetime.now().strftime("%H:%M:%S"),
               "data": _safe(data or {})}
        if keep:
            self.history.append(env)
            self.history = self.history[-self.keep:]
        for q in list(self.clients):
            try:
                q.put_nowait(env)
            except Exception:
                pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=300)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self.clients.discard(q)


def _prime_processes():
    try:
        for p in psutil.process_iter():
            try:
                p.cpu_percent()
            except Exception:
                pass
    except Exception:
        pass


def system_snapshot(prev: Optional[dict]) -> tuple:
    """psutil snapshot. Returns (snapshot, new_prev). Never raises."""
    snap: Dict[str, Any] = {"ts": datetime.now().strftime("%H:%M:%S")}
    try:
        snap["cpu"] = psutil.cpu_percent(interval=None)
    except Exception:
        snap["cpu"] = 0.0
    try:
        snap["cores"] = psutil.cpu_percent(interval=None, percpu=True)
    except Exception:
        snap["cores"] = []
    try:
        m = psutil.virtual_memory()
        snap["mem"] = {"pct": m.percent, "used": m.used, "total": m.total}
    except Exception:
        snap["mem"] = {"pct": 0, "used": 0, "total": 1}
    try:
        d = psutil.disk_usage(str(Path.home().anchor))
        snap["disk"] = {"pct": d.percent, "used": d.used, "total": d.total}
    except Exception:
        snap["disk"] = {"pct": 0, "used": 0, "total": 1}
    now = time.time()
    try:
        n = psutil.net_io_counters()
        sent, recv = n.bytes_sent, n.bytes_recv
    except Exception:
        sent = recv = 0
    if prev and prev.get("t"):
        dt = max(now - prev["t"], 0.01)
        snap["net"] = {"up": (sent - prev.get("sent", sent)) / dt,
                       "down": (recv - prev.get("recv", recv)) / dt}
    else:
        snap["net"] = {"up": 0, "down": 0}
    new_prev = {"sent": sent, "recv": recv, "t": now}
    procs: List[dict] = []
    try:
        cand = []
        for p in psutil.process_iter(["name", "memory_percent"]):
            try:
                name = (p.info.get("name") or "?")[:22]
                if name in ("System Idle Process", "System"):
                    continue
                cand.append((p.cpu_percent(), name,
                             float(p.info.get("memory_percent") or 0)))
            except Exception:
                pass
        cand.sort(reverse=True)
        for cpu, name, mem in cand[:7]:
            procs.append({"name": name, "cpu": round(cpu, 1), "mem": round(mem, 1)})
    except Exception:
        pass
    snap["procs"] = procs
    try:
        temps = psutil.sensors_temperatures() or {}
        vals = []
        for entries in temps.values():
            for e in entries:
                if getattr(e, "current", None):
                    vals.append(e.current)
        snap["temp"] = round(sum(vals) / len(vals), 1) if vals else None
    except Exception:
        snap["temp"] = None
    return snap, new_prev


class ChatIn(BaseModel):
    message: str


def build_app(session_id: Optional[str] = None) -> FastAPI:
    app = FastAPI(title="NEXUS web")
    sm = SessionManager()
    try:
        if session_id:
            sm.load(session_id)
            sid = session_id
        else:
            raise ValueError("new")
    except Exception:
        sid = sm.create("web")
    agent = NexusAgent(sid, sm, LLMProvider())
    hub = Hub()
    agent.add_listener(hub.listener)

    def make_agent(sid_):
        ag = NexusAgent(sid_, sm, LLMProvider())
        ag.add_listener(hub.listener)
        return ag

    app.state.sm = sm
    app.state.sid = sid
    app.state.agent = agent
    app.state.hub = hub
    app.state.lock = asyncio.Lock()
    app.state.t0 = time.time()
    app.state.last_sys = None
    app.state.net_prev = None

    def state_snapshot() -> dict:
        csid = app.state.sid
        # model/effort read live from CFG so /model /thinking reflect instantly
        model = CFG.ollama_model if CFG.provider == "ollama" else CFG.model
        try:
            meta = sm.load(csid)
            n_msg = len(meta.get("messages", []))
            n_tools = len(meta.get("tool_calls", []))
        except Exception:
            n_msg, n_tools = 0, 0
        return {
            "sid": csid,
            "provider": CFG.provider,
            "model": model,
            "thinking": CFG.think_effort,
            "platform": f"{platform.system()} {platform.release()}",
            "uptime": round(time.time() - app.state.t0),
            "busy": app.state.lock.locked(),
            "messages": n_msg,
            "tool_calls": n_tools,
            "workspace": str(sm.workspace(csid)),
        }

    @app.on_event("startup")
    async def _startup():
        _prime_processes()
        snap, app.state.net_prev = system_snapshot(None)
        app.state.last_sys = snap
        log_event("SYSTEM", f"web backend up sid={sid}")
        hub.listener("hello", state_snapshot())
        asyncio.create_task(_sampler())

    async def _sampler():
        while True:
            try:
                await asyncio.sleep(2.0)
                snap, app.state.net_prev = system_snapshot(app.state.net_prev)
                app.state.last_sys = snap
                hub.listener("system", snap, keep=False)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    @app.get("/api/state")
    async def api_state():
        return state_snapshot()

    @app.get("/api/system")
    async def api_system():
        if app.state.last_sys is None:
            snap, app.state.net_prev = system_snapshot(app.state.net_prev)
            app.state.last_sys = snap
        return app.state.last_sys

    @app.get("/api/logs")
    async def api_logs(lines: int = 120):
        return {"lines": get_log_tail(max(1, min(lines, 500)))}

    @app.get("/api/sessions")
    async def api_sessions():
        out = []
        for s in sm.list_sessions()[:20]:
            out.append({"id": s.get("id"), "title": s.get("title"),
                        "messages": len(s.get("messages", [])),
                        "tool_calls": len(s.get("tool_calls", [])),
                        "updated": s.get("updated_at")})
        return {"sessions": out, "active": sid}

    @app.get("/api/memory")
    async def api_memory():
        try:
            data = nexus.MEM.read("")
            return {"profile": data.get("profile", {}), "facts": data.get("facts", []),
                    "total": data.get("total_facts", 0)}
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.get("/api/commands")
    async def api_commands():
        return {"commands": nexus.SLASH_COMMANDS}

    @app.post("/api/chat")
    async def api_chat(body: ChatIn):
        nonlocal sid, agent
        msg = (body.message or "").strip()
        if not msg:
            raise HTTPException(400, "empty message")
        if msg.startswith("/"):
            res = await nexus.handle_slash(msg, {"sm": sm, "sid": sid})
            action = res.get("action")
            if action == "new":
                sid = sm.create("web")
                agent = make_agent(sid)
                app.state.sid, app.state.agent = sid, agent
                hub.listener("session", {"sid": sid})
            elif action == "switch":
                sid = res["sid"]
                agent = make_agent(sid)
                n = agent.restore_history()
                app.state.sid, app.state.agent = sid, agent
                hub.listener("session", {"sid": sid, "restored": n})
                res["reply"] = (res.get("reply") or "") + f"\n({n} messages restored)"
            return {"final": res.get("reply") or "(done)", "slash": True}
        if app.state.lock.locked():
            raise HTTPException(409, "agent is busy with another turn")
        async with app.state.lock:
            hub.listener("user", {"text": msg})
            try:
                final = await agent.run(msg)
            except Exception as e:
                log_event("SYSTEM", f"web chat EXCEPTION: {e}", level="ERROR")
                raise HTTPException(500, str(e))
            return {"final": final}

    @app.websocket("/ws")
    async def ws(ws: WebSocket):
        await ws.accept()
        q = hub.subscribe()
        try:
            await ws.send_json({"type": "hello", "ts": datetime.now().strftime("%H:%M:%S"),
                                "data": state_snapshot()})
            for env in hub.history[-60:]:
                await ws.send_json(env)
            if app.state.last_sys is not None:
                await ws.send_json({"type": "system", "ts": app.state.last_sys.get("ts", ""),
                                    "data": app.state.last_sys})

            async def sender():
                while True:
                    env = await q.get()
                    await ws.send_json(env)

            async def receiver():
                while True:
                    await ws.receive_text()

            st = asyncio.ensure_future(sender())
            rt = asyncio.ensure_future(receiver())
            try:
                done, pending = await asyncio.wait({st, rt}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (st, rt):
                    try:
                        t.cancel()
                    except Exception:
                        pass
        except Exception:
            pass
        finally:
            hub.unsubscribe(q)
            try:
                await ws.close()
            except Exception:
                pass

    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    async def index():
        return FileResponse(str(WEB_DIR / "index.html"))

    return app


def serve(host: str = "127.0.0.1", port: int = 8777, session_id: Optional[str] = None):
    import socket
    import uvicorn
    # Split-brain guard: two backends on one port silently steal each other's
    # requests (Windows allows the double bind) — refuse to start instead.
    try:
        probe = socket.create_connection((host, port), timeout=1.5)
        probe.close()
        print(f"PORT BUSY: another NEXUS backend already owns http://{host}:{port}/ — aborting.")
        log_event("SYSTEM", f"web serve ABORTED, port {port} busy", level="ERROR")
        raise SystemExit(2)
    except SystemExit:
        raise
    except Exception:
        pass
    url = f"http://{host}:{port}/"
    print(f"NEXUS web UI -> {url}")
    log_event("SYSTEM", f"web serve {url}")
    uvicorn.run(build_app(session_id), host=host, port=port, log_level="warning")
