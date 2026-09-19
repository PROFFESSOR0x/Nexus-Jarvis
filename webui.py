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
from fastapi import FastAPI, File, Form, HTTPException, UploadFile, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import main as nexus
from main import CFG, LOG_FILE, LLMProvider, NexusAgent, SessionManager, get_log_tail, log_event

WEB_DIR = Path(__file__).parent / "web"
UPLOAD_MAX_BYTES = 50_000_000  # per file


def safe_upload_name(raw: str) -> str:
    """Basename-only sanitizer: kills traversal (../../x) and absolute paths."""
    name = Path(raw or "").name.strip().strip(".")
    return name[:120]


def _unique_path(ws: Path, name: str) -> Path:
    p = ws / name
    if not p.exists():
        return p
    stem, suffix = Path(name).stem, Path(name).suffix
    i = 1
    while True:
        cand = ws / f"{stem}_{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


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
    sid: str = ""


class SidIn(BaseModel):
    sid: str = ""


class SessionNewIn(BaseModel):
    title: str = "web"


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
    hub = Hub()

    def make_agent(sid_, stop_ev):
        ag = NexusAgent(sid_, sm, LLMProvider())
        ag.stop_event = stop_ev
        tag = sid_
        ag.add_listener(lambda e, d, _s=tag: hub.listener(e, {**(d or {}), "sid": _s}))
        return ag

    async def _on_round_done(summary: dict):
        """Background round closed: notify + auto-synthesize if idle, else pend.
        The next/ongoing turn drains pending notes into the leader. Never raises."""
        try:
            sid_ = summary.get("sid", "")
            rec2 = app.state.sessions.get(sid_)
            if rec2 is None:
                return
            note = summary.get("summary", "") or ""
            if note:
                rec2["agent"].pending_round_notes.append(note)
            hub.listener("round_done", {"sid": sid_, "round": summary.get("round"),
                                        "ok": summary.get("ok"),
                                        "summary": note[:600]})
            if rec2["lock"].locked():
                return  # busy: current/next turn drains the notes
            async with rec2["lock"]:
                if not rec2["agent"].pending_round_notes:
                    return  # a turn that started meanwhile already drained them
                rec2["stop"].clear()
                try:
                    await rec2["agent"].run("[background] A background round finished — "
                                            "synthesize its results and report to the user now.")
                except Exception as e:
                    log_event("SYSTEM", f"auto-synthesis EXCEPTION sid={sid_}: {e}",
                              level="ERROR")
        except Exception as e:
            log_event("SYSTEM", f"on_round_done EXCEPTION: {e}", level="ERROR")

    def get_record(sid_) -> dict:
        """Per-session runtime: agent + lock + message queue + stop flag.
        Validates the session exists (404 otherwise). Never raises otherwise."""
        try:
            sm.load(sid_)
        except Exception:
            raise HTTPException(404, f"unknown session {sid_}")
        rec = app.state.sessions.get(sid_)
        if not rec:
            stop_ev = asyncio.Event()
            rec = {"agent": make_agent(sid_, stop_ev), "lock": asyncio.Lock(),
                   "queue": [], "stop": stop_ev}
            rec["agent"].on_round_done = _on_round_done
            app.state.sessions[sid_] = rec
        return rec

    app.state.sm = sm
    app.state.sid = sid  # ACTIVE (focused) session for hologram + state
    app.state.sessions = {}
    app.state.hub = hub
    app.state.t0 = time.time()
    app.state.last_sys = None
    app.state.net_prev = None
    get_record(sid)

    def state_snapshot(sid_=None) -> dict:
        csid = sid_ or app.state.sid
        # model/effort read live from CFG so /model /thinking /provider reflect instantly
        model = CFG.ollama_model if CFG.provider == "ollama" else CFG.model
        try:
            meta = sm.load(csid)
            n_msg = len(meta.get("messages", []))
            n_tools = len(meta.get("tool_calls", []))
        except Exception:
            n_msg, n_tools = 0, 0
        try:
            rec = app.state.sessions.get(csid) or {}
            busy = bool(rec.get("lock") and rec["lock"].locked())
            queued = len(rec.get("queue", []))
        except Exception:
            busy, queued = False, 0
        return {
            "sid": csid,
            "provider": CFG.provider,
            "model": model,
            "thinking": CFG.think_effort,
            "base_url": CFG.base_url,
            "ollama_host": CFG.ollama_host,
            "platform": f"{platform.system()} {platform.release()}",
            "uptime": round(time.time() - app.state.t0),
            "busy": busy,
            "queued": queued,
            "messages": n_msg,
            "tool_calls": n_tools,
            "workspace": str(sm.workspace(csid)),
            "recursive": bool(getattr((app.state.sessions.get(csid) or {}).get("agent"),
                                      "recursive_mode", False)),
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
    async def api_state(sid: str = ""):
        return state_snapshot((sid or "").strip() or None)

    @app.get("/api/deck")
    async def api_deck(sid: str = ""):
        """Authoritative deck truth: running rounds + per-worker states + busy.

        The UI reconciles stuck visuals (working/thinking…) against this —
        anything the backend already finished gets settled locally."""
        tsid = (sid or "").strip() or app.state.sid
        try:
            sm.load(tsid)
        except Exception:
            raise HTTPException(404, f"unknown session {tsid}")
        rec = app.state.sessions.get(tsid)
        if not rec:
            return {"sid": tsid, "busy": False, "running": {}}
        try:
            busy = bool(rec.get("lock") and rec["lock"].locked())
        except Exception:
            busy = False
        running: Dict[str, Any] = {}
        try:
            ag = rec.get("agent")
            for rid, record in (getattr(ag, "_bg_rounds", {}) or {}).items():
                rnd = (record or {}).get("rnd")
                try:
                    states = dict(getattr(rnd, "states", {}) or {})
                except Exception:
                    states = {}
                running[rid] = {"workers": states,
                                "done": bool(getattr(rnd, "done", False))}
        except Exception as e:
            log_event("SYSTEM", f"deck EXCEPTION sid={tsid}: {e}", level="ERROR")
        return {"sid": tsid, "busy": busy, "running": running}

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
        return {"sessions": out, "active": app.state.sid}

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

    @app.get("/api/models")
    async def api_models():
        """Live model lists for both providers (for dropdowns / /models UI).

        Never raises 500 for a down provider — returns per-provider error
        strings so the UI can show diagnostics.
        """
        import asyncio as _aio
        oll_c, oai_c = await _aio.gather(
            nexus._fetch_ollama_models(),
            nexus._fetch_openai_models(),
        )
        cur = CFG.ollama_model if CFG.provider == "ollama" else CFG.model
        return {
            "provider": CFG.provider,
            "current": cur,
            "ollama": {"host": oll_c.get("host"), "models": oll_c.get("models", []),
                       "error": oll_c.get("error")},
            "openai": {"base_url": oai_c.get("base_url"), "models": oai_c.get("models", []),
                       "error": oai_c.get("error"), "status": oai_c.get("status")},
        }

    @app.post("/api/upload")
    async def api_upload(files: List[UploadFile] = File(...), sid: str = Form("")):
        """Button + drag-and-drop uploads. Saved into the target session's
        workspace (its own folder; agents are not confined to it)."""
        tsid = (sid or "").strip() or app.state.sid
        rec = get_record(tsid)
        ws = sm.workspace(tsid)
        try:
            ws.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(500, f"workspace unavailable: {e}")
        saved, errors = [], []
        for f in files or []:
            name = safe_upload_name(f.filename or "")
            if not name:
                errors.append({"name": f.filename or "?", "error": "empty filename"})
                continue
            try:
                dest = _unique_path(ws, name)
                size = 0
                with dest.open("wb") as out:
                    while True:
                        chunk = await f.read(1024 * 1024)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > UPLOAD_MAX_BYTES:
                            raise ValueError(f"over {UPLOAD_MAX_BYTES // 1000000}MB cap")
                        out.write(chunk)
                saved.append({"name": dest.name, "size": size,
                              "path": str(dest.relative_to(ws))})
                try:
                    rec["agent"].pending_files.append({"name": dest.name, "size": size})
                except Exception:
                    pass
            except Exception as e:
                errors.append({"name": name, "error": str(e)[:200]})
            finally:
                try:
                    await f.close()
                except Exception:
                    pass
        log_event("SAVE", f"upload sid={tsid} saved={len(saved)} errors={len(errors)} "
                          f"files={[s['name'] for s in saved]}")
        hub.listener("upload", {"sid": tsid, "workspace": str(ws),
                                "saved": saved, "errors": errors})
        return {"saved": saved, "errors": errors, "workspace": str(ws)}

    @app.get("/api/evolution")
    async def api_evolution():
        try:
            import evolution
            h = evolution.history()
            return {"version": h.get("current", "?"),
                    "rules": h.get("rules", []),
                    "transitions": h.get("transitions", [])[-8:],
                    "recent_cycles": h.get("recent_cycles", []),
                    "recent_runs": evolution.recent_runs(),
                    "repo_files": evolution.repo_file_count(),
                    "defects": evolution.aggregate_defects()[:5],
                    "state": evolution.runtime_state()}
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.post("/api/session/new")
    async def api_session_new(body: SessionNewIn):
        """Create a session + runtime for an extra floating chat window."""
        nsid = sm.create((body.title or "web").strip()[:40] or "web")
        get_record(nsid)
        log_event("SYSTEM", f"web session new sid={nsid}")
        return {"sid": nsid, "state": state_snapshot(nsid)}

    class RecursiveIn(BaseModel):
        sid: str = ""
        enabled: bool = False

    @app.post("/api/recursive")
    async def api_recursive(body: RecursiveIn):
        """NEST MODE toggle: allow this session's workers to spawn sub-workers."""
        nsid = (body.sid or "").strip() or app.state.sid
        rec = get_record(nsid)  # 404 if unknown
        rec["agent"].recursive_mode = bool(body.enabled)
        try:
            from main import log_event as _le
            _le("SYSTEM", f"recursive mode {'ON' if body.enabled else 'OFF'} sid={nsid}")
        except Exception:
            pass
        return {"sid": nsid, "recursive": bool(body.enabled)}

    @app.post("/api/focus")
    async def api_focus(body: SidIn):
        """Focus a session: hologram + state follow it."""
        nsid = (body.sid or "").strip()
        get_record(nsid)  # 404 if unknown
        app.state.sid = nsid
        return state_snapshot(nsid)

    @app.post("/api/stop")
    async def api_stop(body: SidIn):
        """STOP button: halt the running turn (cooperative: finishes the
        current model call, then stops) and drop queued messages."""
        nsid = (body.sid or "").strip() or app.state.sid
        try:
            sm.load(nsid)
        except Exception:
            raise HTTPException(404, f"unknown session {nsid}")
        rec = app.state.sessions.get(nsid)
        if not rec or not rec["lock"].locked():
            return {"stopped": False, "sid": nsid, "reason": "idle"}
        rec["stop"].set()
        n = len(rec["queue"])
        rec["queue"].clear()
        log_event("SYSTEM", f"web stop sid={nsid} cleared={n}")
        hub.listener("stop", {"sid": nsid, "cleared": n})
        return {"stopped": True, "sid": nsid, "cleared": n}

    @app.post("/api/chat")
    async def api_chat(body: ChatIn):
        msg = (body.message or "").strip()
        if not msg:
            raise HTTPException(400, "empty message")
        # target session: explicit per-window sid, else the focused one
        tsid = (body.sid or "").strip() or app.state.sid
        if msg.startswith("/"):
            # slash commands resolve instantly (never queued, never blocked)
            res = await nexus.handle_slash(msg, {"sm": sm, "sid": tsid})
            action = res.get("action")
            if action == "new":
                # handle_slash already created the session — bind a runtime.
                # The CALLING window rebinds itself (returned sid); other
                # windows are untouched. No hub session event (no races).
                nsid = res.get("sid") or sm.create("web")
                get_record(nsid)
                return {"final": res.get("reply") or "(done)", "slash": True,
                        "action": action, "sid": nsid}
            elif action == "switch":
                nsid = res["sid"]
                rec = get_record(nsid)
                n = rec["agent"].restore_history()
                res["reply"] = (res.get("reply") or "") + f"\n({n} messages restored)"
                return {"final": res.get("reply") or "(done)", "slash": True,
                        "action": action, "sid": nsid}
            elif action == "clear":
                return {"final": res.get("reply") or "", "slash": True,
                        "action": "clear", "sid": tsid}
            elif action == "recursive":
                rec = get_record(tsid)
                rec["agent"].recursive_mode = bool(res.get("enabled"))
                return {"final": res.get("reply") or "(done)", "slash": True,
                        "action": "recursive", "sid": tsid,
                        "recursive": rec["agent"].recursive_mode}
            elif action == "layout":
                # pure frontend visual: the hologram switches law + persists it
                return {"final": res.get("reply") or "(done)", "slash": True,
                        "action": "layout", "sid": tsid,
                        "layout": res.get("mode") or "flow"}
            return {"final": res.get("reply") or "(done)", "slash": True,
                    "action": action, "sid": tsid}
        rec = get_record(tsid)
        if rec["lock"].locked():
            # busy: QUEUE instead of 409 — runs automatically after this turn
            if len(rec["queue"]) >= 25:
                raise HTTPException(429, "message queue full (25) — wait or STOP")
            rec["queue"].append(msg)
            hub.listener("queued", {"sid": tsid, "text": msg,
                                    "position": len(rec["queue"])})
            return {"queued": True, "position": len(rec["queue"]), "sid": tsid}
        agent = rec["agent"]
        async with rec["lock"]:
            rec["stop"].clear()
            hub.listener("user", {"text": msg, "sid": tsid})
            try:
                final = await agent.run(msg)
            except Exception as e:
                log_event("SYSTEM", f"web chat EXCEPTION sid={tsid}: {e}", level="ERROR")
                raise HTTPException(500, str(e))
            # drain the queue sequentially as follow-up turns (each announces
            # itself over WS with user/final events for its own window)
            while rec["queue"]:
                if rec["stop"].is_set():
                    rec["queue"].clear()
                    break
                nxt = rec["queue"].pop(0)
                hub.listener("user", {"text": nxt, "sid": tsid})
                try:
                    await agent.run(nxt)
                except Exception as e:
                    log_event("SYSTEM", f"web drain EXCEPTION sid={tsid}: {e}",
                              level="ERROR")
                    break
            return {"final": final, "sid": tsid}

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
        # Log-found fix: browsers cached stale index.html (old ?v= pins) so
        # users kept running old CSS/JS after an update ("interface stopped").
        # Never cache the shell — the ?v= assets themselves stay cacheable.
        return FileResponse(str(WEB_DIR / "index.html"),
                            headers={"Cache-Control": "no-store, must-revalidate"})

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
