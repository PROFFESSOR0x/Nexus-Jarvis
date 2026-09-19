#!/usr/bin/env python3
"""
NEXUS — Multi-Agent System with Parallel Tool Execution
Production-ready prototype. No placeholders, no mocks, no hardcoded stubs.
"""

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable, Awaitable

# ── Third-party ──
import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

# Textual TUI
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Header, Footer, Static, Input, RichLog, TabbedContent, TabPane,
    Label, ProgressBar, DataTable, Tree,
)
from textual.reactive import reactive
from textual.theme import Theme
from textual.binding import Binding
from textual.message import Message

# NEXUS browser tool (full Playwright). Import is lightweight: playwright
# itself is imported lazily inside nexus_browser on first browser action.
try:
    from nexus_browser import tool_browser, TOOL_SPEC_BROWSER
except Exception as _e:  # pragma: no cover - missing file should not kill NEXUS
    async def tool_browser(action: str = "", **kw) -> dict:  # type: ignore
        return {"success": False, "action": action or "?",
                "error": f"browser tool unavailable (nexus_browser import failed: {_e})"}
    TOOL_SPEC_BROWSER = {  # type: ignore
        "type": "function",
        "function": {"name": "browser",
                     "description": "UNAVAILABLE (nexus_browser failed to import).",
                     "parameters": {"type": "object", "properties": {}}},
    }

# ── Load environment ──
load_dotenv()

console = Console()

# ============================================================
#  LOGGER — nexus.log (every tool / model / save / result)
# ============================================================
LOG_FILE = Path(__file__).parent / "nexus.log"

_nexus_logger = logging.getLogger("nexus")
_nexus_logger.setLevel(logging.DEBUG)
_nexus_logger.propagate = False
if not _nexus_logger.handlers:
    try:
        _file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
    except Exception:
        _file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _nexus_logger.addHandler(_file_handler)


def _short(s: Any, n: int = 4000) -> str:
    try:
        s = str(s)
    except Exception:
        s = "<unprintable>"
    if len(s) <= n:
        return s
    return s[:n] + f"...[truncated {len(s) - n} chars]"


def log_event(component: str, message: str, level: str = "INFO", extra: Any = None):
    """Single choke-point for every nexus.log line. Never raises."""
    try:
        line = f"[{component}] {message}"
        if extra is not None:
            try:
                extra_s = json.dumps(extra, ensure_ascii=False, default=str)
            except Exception:
                extra_s = str(extra)
            line += f" | {_short(extra_s, 4000)}"
        lvl = getattr(logging, level.upper(), logging.INFO)
        _nexus_logger.log(lvl, line)
    except Exception:
        pass


def log_tool_call(name: str, params: dict, result: dict, elapsed: float):
    ok = bool(result.get("success")) if isinstance(result, dict) else False
    log_event(
        "TOOL",
        f"{name} {'OK' if ok else 'FAIL'} in {elapsed:.3f}s",
        level="INFO" if ok else "WARNING",
        extra={"params": params, "result": result, "elapsed": elapsed},
    )


def log_llm_call(provider: str, model: str, n_messages: int,
                 n_tools: Optional[int], latency: float,
                 content_preview: str, n_tool_calls: int, error: Optional[str] = None):
    if error:
        log_event(
            "LLM",
            f"{provider}/{model} ERROR after {latency:.2f}s: {_short(error, 500)}",
            level="ERROR",
            extra={"messages": n_messages, "tools": n_tools, "latency": latency, "error": error},
        )
    else:
        log_event(
            "LLM",
            f"{provider}/{model} {latency:.2f}s msgs={n_messages} tools={n_tools} "
            f"-> tool_calls={n_tool_calls} reply={_short(content_preview, 300)!r}",
            extra={"messages": n_messages, "tools": n_tools, "latency": latency,
                   "preview": _short(content_preview, 1000), "tool_calls": n_tool_calls},
        )


def log_save(action: str, sid: str, detail: Any = None):
    log_event("SAVE", f"{action} sid={sid} {_short(detail or '', 300)}",
              extra={"sid": sid, "action": action, "detail": detail})


def log_result(kind: str, summary: str, payload: Any = None, level: str = "INFO"):
    log_event("RESULT", f"{kind}: {_short(summary, 500)}", level=level, extra=payload)


def get_log_tail(n: int = 40) -> List[str]:
    """Last n lines of nexus.log for the TUI logs panel. Never raises."""
    try:
        if not LOG_FILE.exists():
            return ["(nexus.log empty — nothing logged yet)"]
        with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-n:]] or ["(nexus.log empty)"]
    except Exception as e:
        return [f"(log read error: {e})"]

def _normalize_ollama_host(raw: str) -> str:
    """Turn server listen values like '0.0.0.0' into a connectable client URL.

    OLLAMA_HOST is also the server's bind address, so a global
    OLLAMA_HOST=0.0.0.0 (valid for `ollama serve`) shadows the .env client
    URL and breaks the Python client with 'Failed to connect'. Never raises.
    """
    try:
        h = (raw or "").strip().strip('"').strip("'")
        if not h:
            return "http://127.0.0.1:11434"
        # bare '0.0.0.0' means 'listening on all interfaces' -> dial localhost
        if h == "0.0.0.0":
            return "http://127.0.0.1:11434"
        if h.startswith("0.0.0.0:"):
            return "http://127.0.0.1:" + h[len("0.0.0.0:"):]
        if h.startswith("http://0.0.0.0") or h.startswith("https://0.0.0.0"):
            h = h.replace("0.0.0.0", "127.0.0.1", 1)
            return h.rstrip("/")
        if not h.startswith(("http://", "https://")):
            # 'host:port' or 'hostname' -> prepend scheme, default port 11434
            h = "http://" + h
            if h.count(":") == 1:  # only scheme colon -> no port given
                h += ":11434"
            return h.rstrip("/")
        return h.rstrip("/")
    except Exception:
        return "http://127.0.0.1:11434"


# ============================================================
#  CONFIG
# ============================================================
@dataclass
class Config:
    provider: str = os.getenv("LLM_PROVIDER", "openai")          # openai | ollama
    model: str = os.getenv("LLM_MODEL", "gpt-4o")
    api_key: str = os.getenv("OPENAI_API_KEY", "")
    base_url: str = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "llama3.1")
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.7"))
    max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "8192"))
    max_iterations: int = int(os.getenv("MAX_ITERATIONS", "100"))
    think_effort: str = os.getenv("THINK_EFFORT", "medium").strip().lower() or "medium"
    max_subagents: int = int(os.getenv("MAX_SUBAGENTS", "100"))
    recursive_max_depth: int = int(os.getenv("RECURSIVE_MAX_DEPTH", "3"))
    recursive_max_tasks: int = int(os.getenv("RECURSIVE_MAX_TASKS", "10"))
    recursive_max_tree: int = int(os.getenv("RECURSIVE_MAX_TREE", "100"))
    recursive_tree_ceiling: int = int(os.getenv("RECURSIVE_TREE_CEILING", "600"))
    subagent_max_iterations: int = int(os.getenv("SUBAGENT_MAX_ITERATIONS", "100"))
    delegate_max_rounds: int = int(os.getenv("DELEGATE_MAX_ROUNDS", "100"))
    round_quiet_sec: float = float(os.getenv("ROUND_QUIET_SEC", "8"))
    round_timeout_sec: float = float(os.getenv("ROUND_TIMEOUT_SEC", "600"))
    worker_linger_sec: float = float(os.getenv("WORKER_LINGER_SEC", "240"))
    workspace_root: Path = Path(os.getenv("WORKSPACE_ROOT", Path.home() / ".nexus" / "sessions"))
    system_prompt_path: Path = Path(os.getenv("SYSTEM_PROMPT", Path(__file__).parent / "system_prompt.txt"))
    memory_path: Path = Path(os.getenv("MEMORY_PATH", Path.home() / ".nexus" / "memory.json"))

    def __post_init__(self):
        raw = self.ollama_host
        fixed = _normalize_ollama_host(raw)
        if fixed != raw:
            log_event("SYSTEM", f"OLLAMA_HOST normalized {raw!r} -> {fixed!r} "
                                f"(0.0.0.0 is a listen address, not dialable)")
        self.ollama_host = fixed
        if self.think_effort not in ("low", "medium", "high"):
            self.think_effort = "medium"
        # Log-found fix: .env had LLM_MAX_TOKENS=10000000 — Ollama cloud
        # rejects anything over the model's max (65536) and every turn
        # failed/retried. Clamp so a bad env can't wedge the interface.
        try:
            if self.max_tokens > 65536:
                log_event("SYSTEM", f"LLM_MAX_TOKENS clamped {self.max_tokens} -> 65536 "
                                    f"(cloud max)", level="WARNING")
                self.max_tokens = 65536
            elif self.max_tokens < 256:
                self.max_tokens = 256
        except Exception:
            pass

    @property
    def ollama_think(self):
        # low = light trace, medium = default full, high = deepest
        return {"low": "low", "medium": True, "high": "high"}.get(self.think_effort, True)

CFG = Config()


# ============================================================
#  LONG-TERM MEMORY (persists across sessions)
# ============================================================
MEMORY_FACT_CAP = 200

class MemoryManager:
    """User profile + durable facts in ~/.nexus/memory.json.

    The leader reads it (context + memory.read) and writes it
    (memory.remember / memory.profile / memory.forget). Workers get
    memory.read only — they must not pollute long-term state.
    """
    def __init__(self, path: Path = CFG.memory_path):
        self.path = path
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        self.data = {"profile": {}, "facts": []}
        self._load()

    def _load(self):
        try:
            if self.path.exists():
                raw = self.path.read_text(encoding="utf-8")
                if raw.strip():
                    d = json.loads(raw)
                    if isinstance(d, dict):
                        self.data["profile"] = d.get("profile", {}) or {}
                        self.data["facts"] = d.get("facts", []) or []
        except Exception as e:
            log_event("MEMORY", f"load failed: {e}", level="WARNING")

    def _save(self):
        try:
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as e:
            log_event("MEMORY", f"save failed: {e}", level="ERROR")
            raise

    def profile(self) -> dict:
        return dict(self.data.get("profile", {}))

    def set_profile(self, key: str, value: str) -> dict:
        key = (key or "").strip().lower().replace(" ", "_")[:40]
        value = _short((value or "").strip(), 300)
        if not key:
            return {"success": False, "error": "empty profile key"}
        self.data["profile"][key] = value
        self._save()
        log_event("MEMORY", f"profile set {key}={value!r}")
        return {"success": True, "key": key, "value": value}

    def remember(self, text: str) -> dict:
        text = _short((text or "").strip(), 500)
        if not text:
            return {"success": False, "error": "empty fact — nothing stored"}
        if any(f.get("text", "").lower() == text.lower() for f in self.data["facts"]):
            return {"success": True, "duplicate": True, "count": len(self.data["facts"])}
        self.data["facts"].append({"text": text, "ts": datetime.now().isoformat(timespec="seconds")})
        self.data["facts"] = self.data["facts"][-MEMORY_FACT_CAP:]
        self._save()
        log_event("MEMORY", f"remembered: {text!r}")
        return {"success": True, "count": len(self.data["facts"])}

    def forget(self, text: str) -> dict:
        needle = (text or "").strip().lower()
        if not needle:
            return {"success": False, "error": "empty search — nothing removed"}
        before = len(self.data["facts"])
        self.data["facts"] = [f for f in self.data["facts"] if needle not in f.get("text", "").lower()]
        removed = before - len(self.data["facts"])
        if removed:
            self._save()
        log_event("MEMORY", f"forgot {removed} fact(s) matching {text!r}")
        return {"success": True, "removed": removed}

    def read(self, query: str = "") -> dict:
        q = (query or "").strip().lower()
        facts = self.data.get("facts", [])
        if q:
            words = [w for w in q.split() if len(w) > 2]
            scored = []
            for f in facts:
                t = f.get("text", "").lower()
                score = sum(1 for w in words if w in t) + (2 if q in t else 0)
                if score:
                    scored.append((score, f))
            scored.sort(key=lambda x: -x[0])
            facts = [f for _, f in scored[:15]]
        else:
            facts = facts[-15:]
        return {"success": True, "profile": self.profile(), "facts": facts,
                "total_facts": len(self.data.get("facts", []))}

    def context_block(self) -> str:
        """Injected into the leader system prompt."""
        prof = self.profile()
        facts = self.data.get("facts", [])[-20:]
        if not prof and not facts:
            return ("(empty — you don't know the user yet. Early in the conversation, "
                    "ask their name + what they work on, then store it with memory.profile "
                    "and memory.remember so you remember them across sessions.)")
        lines = []
        if prof:
            lines.append("Profile: " + "; ".join(f"{k}={v}" for k, v in prof.items()))
        for f in facts:
            lines.append(f"- {f.get('text', '')}")
        return "\n".join(lines)


MEM = MemoryManager()


async def tool_memory_read(query: str = "") -> dict:
    """Read long-term memory: user profile + matching facts."""
    try:
        return MEM.read(query)
    except Exception as e:
        return {"success": False, "error": str(e)}


async def tool_memory_remember(text: str = "") -> dict:
    """Store one durable fact about the user/preferences/learnings."""
    try:
        return MEM.remember(text)
    except Exception as e:
        return {"success": False, "error": str(e)}


async def tool_memory_forget(text: str = "") -> dict:
    """Delete facts containing the given text."""
    try:
        return MEM.forget(text)
    except Exception as e:
        return {"success": False, "error": str(e)}


async def tool_memory_profile(key: str = "", value: str = "") -> dict:
    """Set one user-profile field (e.g. name, role, timezone, language). Empty value reads the profile."""
    try:
        if not (value or "").strip():
            return {"success": True, "profile": MEM.profile()}
        return MEM.set_profile(key, value)
    except Exception as e:
        return {"success": False, "error": str(e)}


# ============================================================
#  SESSION MANAGEMENT
# ============================================================
class SessionManager:
    """
    Session-centric storage (MAMS-inspired).
    Each session: ~/.nexus/sessions/{session_id}/
      ├── session.json      — metadata + message history
      ├── context.json      — agent context / memory
      └── workspace/        — primary working directory
    """
    def __init__(self, root: Path = CFG.workspace_root):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, title: str = "untitled") -> str:
        sid = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        d = self.root / sid
        (d / "workspace").mkdir(parents=True, exist_ok=True)
        meta = {
            "id": sid,
            "title": title,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "messages": [],
            "tool_calls": [],
        }
        (d / "session.json").write_text(json.dumps(meta, indent=2))
        (d / "context.json").write_text(json.dumps({"memory": [], "facts": {}}, indent=2))
        log_event("SESSION", f"created sid={sid} title={title!r}")
        log_save("create", sid, f"title={title!r}")
        return sid

    def save(self, sid: str, meta: dict):
        d = self.root / sid
        meta["updated_at"] = datetime.now().isoformat()
        n_msg = len(meta.get("messages", []))
        n_tools = len(meta.get("tool_calls", []))
        # Atomic write (tmp + os.replace): a crash/kill mid-write can never
        # leave a truncated session.json behind (truncated files broke load()
        # with JSONDecodeError and took down the input handler).
        try:
            d.mkdir(parents=True, exist_ok=True)
            tmp = d / "session.json.tmp"
            tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, d / "session.json")
        except Exception as e:
            log_event("SAVE", f"save FAILED sid={sid}: {e}", level="ERROR")
            raise
        log_save("save", sid, f"messages={n_msg} tool_calls={n_tools}")

    def load(self, sid: str) -> dict:
        d = self.root / sid
        try:
            raw = (d / "session.json").read_text(encoding="utf-8")
            if not raw.strip():
                raise ValueError("session.json is empty")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("session.json is not a JSON object")
            data.setdefault("messages", [])
            data.setdefault("tool_calls", [])
            return data
        except Exception as e:
            log_event("SAVE", f"load FAILED sid={sid}: {e}", level="ERROR")
            raise

    def list_sessions(self) -> List[dict]:
        sessions = []
        try:
            dirs = sorted(
                [d for d in self.root.iterdir() if d.is_dir()],
                key=lambda d: d.stat().st_mtime if d.exists() else 0,
                reverse=True,
            )
        except Exception:
            try:
                dirs = sorted(self.root.iterdir(), reverse=True)
            except Exception:
                return []
        for d in dirs:
            if (d / "session.json").exists():
                try:
                    meta = json.loads((d / "session.json").read_text())
                    sessions.append(meta)
                except Exception:
                    pass
        return sessions

    def workspace(self, sid: str) -> Path:
        return self.root / sid / "workspace"

    def add_message(self, sid: str, role: str, content: str, tool_calls=None):
        meta = self.load(sid)
        msg = {"role": role, "content": content, "ts": datetime.now().isoformat()}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        meta["messages"].append(msg)
        self.save(sid, meta)
        log_event("SAVE", f"add_message sid={sid} role={role} "
                           f"len={len(content or '')} tool_calls={len(tool_calls or [])} "
                           f"preview={_short(content or '', 200)!r}")

    def add_tool_call(self, sid: str, name: str, params: dict, result: dict, elapsed: float):
        meta = self.load(sid)
        meta["tool_calls"].append({
            "name": name,
            "params": params,
            "result": result,
            "elapsed": elapsed,
            "ts": datetime.now().isoformat(),
        })
        self.save(sid, meta)
        ok = bool(result.get("success")) if isinstance(result, dict) else False
        log_event("SAVE", f"persist_tool_call sid={sid} tool={name} "
                           f"{'OK' if ok else 'FAIL'} elapsed={elapsed:.3f}s")


# ============================================================
#  SYSTEM INFO (auto-detected)
# ============================================================
def detect_environment() -> dict:
    os_name = platform.system()
    os_release = platform.release()
    os_version = platform.version()
    arch = platform.machine()
    py = platform.python_version()
    shells = []
    for sh in ["bash", "sh", "zsh", "fish", "cmd.exe", "powershell.exe", "pwsh.exe", "pwsh"]:
        path = shutil.which(sh)
        if path:
            shells.append({"name": sh, "path": path})
    return {
        "os": os_name,
        "release": os_release,
        "version": os_version,
        "arch": arch,
        "python": py,
        "shells": shells,
        "cwd": str(Path.cwd()),
        "home": str(Path.home()),
    }


# ============================================================
#  TOOL IMPLEMENTATIONS
# ============================================================

async def tool_exec_shell(command: str, shell_type: Optional[str] = None,
                          timeout: int = 120, cwd: Optional[str] = None) -> dict:
    """Execute a shell command. Auto-detects shell if not specified."""
    env = detect_environment()
    if shell_type:
        sh = next((s for s in env["shells"] if s["name"] == shell_type), None)
        if not sh:
            return {"success": False, "error": f"Shell '{shell_type}' not available. Available: {[s['name'] for s in env['shells']]}"}
        executable = sh["path"]
    else:
        # Prefer bash on Unix, pwsh/powershell on Windows
        if env["os"] == "Windows":
            executable = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("cmd.exe")
        else:
            executable = shutil.which("bash") or shutil.which("sh")
    if not executable:
        return {"success": False, "error": "No shell found"}

    try:
        proc = await asyncio.create_subprocess_exec(
            executable, "-c" if executable.endswith(("bash", "sh", "zsh")) else "/c",
            command,
            stdin=asyncio.subprocess.DEVNULL,  # log-found fix: commands waiting
            stdout=asyncio.subprocess.PIPE,    # on stdin (bare `grep`, `cat`)
            stderr=asyncio.subprocess.PIPE,    # used to hang until timeout
            cwd=cwd or str(Path.cwd()),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"success": False, "error": f"Timeout after {timeout}s"}
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


LANG_MAP = {
    "python": (".py", ["python", "-c"]),
    "javascript": (".js", ["node", "-e"]),
    "js": (".js", ["node", "-e"]),
    "c": (".c", None),
    "cpp": (".cpp", None),
    "csharp": (".cs", None),
    "cs": (".cs", None),
    "pwsh": (".ps1", None),
}

async def tool_exec_code(code: str, language: str = "python",
                         timeout: int = 120, session_workspace: Optional[str] = None) -> dict:
    """Execute code in the specified language. Writes to workspace, runs, returns output."""
    lang = language.lower()
    ext, runner = LANG_MAP.get(lang, (".txt", None))

    workdir = Path(session_workspace) if session_workspace else Path(tempfile.mkdtemp())
    workdir.mkdir(parents=True, exist_ok=True)
    fname = workdir / f"nexus_{uuid.uuid4().hex[:8]}{ext}"
    fname.write_text(code, encoding="utf-8")

    try:
        if lang == "python":
            cmd = [sys.executable, str(fname)]
        elif lang in ("javascript", "js"):
            cmd = ["node", str(fname)]
        elif lang == "c":
            out = fname.with_suffix("")
            compile_proc = await asyncio.create_subprocess_exec(
                "gcc", str(fname), "-o", str(out),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, cerr = await asyncio.wait_for(compile_proc.communicate(), timeout=30)
            if compile_proc.returncode != 0:
                return {"success": False, "error": f"C compile failed: {cerr.decode()}"}
            cmd = [str(out)]
        elif lang in ("cpp", "c++"):
            out = fname.with_suffix("")
            compile_proc = await asyncio.create_subprocess_exec(
                "g++", str(fname), "-o", str(out), "-std=c++17",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, cerr = await asyncio.wait_for(compile_proc.communicate(), timeout=30)
            if compile_proc.returncode != 0:
                return {"success": False, "error": f"C++ compile failed: {cerr.decode()}"}
            cmd = [str(out)]
        elif lang in ("csharp", "cs"):
            out = fname.with_suffix(".exe")
            compile_proc = await asyncio.create_subprocess_exec(
                "dotnet", "script", str(fname),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                stdout, stderr = await asyncio.wait_for(compile_proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                compile_proc.kill()
                return {"success": False, "error": "dotnet script timeout"}
            return {
                "success": compile_proc.returncode == 0,
                "exit_code": compile_proc.returncode,
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
            }
        elif lang == "pwsh":
            pwsh = shutil.which("pwsh") or shutil.which("powershell.exe")
            if not pwsh:
                return {"success": False, "error": "PowerShell not found"}
            cmd = [pwsh, "-File", str(fname)]
        else:
            return {"success": False, "error": f"Unsupported language: {language}"}

        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir))
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"success": False, "error": f"Timeout after {timeout}s"}
        return {
            "success": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
            "file": str(fname),
        }
    except FileNotFoundError as e:
        return {"success": False, "error": f"Runtime not found: {e}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


async def tool_web_search(queries: List[str], max_results: int = 8,
                          region: str = "wt-wt", safesearch: str = "moderate") -> dict:
    """DuckDuckGo search via ddgs (no API key). Multiple queries in parallel."""
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            return {"success": False, "error": "Install 'ddgs': pip install ddgs"}

    async def _one(q: str):
        loop = asyncio.get_event_loop()
        try:
            # Log-found fix: ddgs has no internal timeout — a dead backend
            # (refused startpage, hanging DDG) stalled workers until the round
            # died. Fail fast so workers move on instead of burning iterations.
            results = await asyncio.wait_for(loop.run_in_executor(
                None,
                lambda: list(DDGS().text(q, max_results=max_results, region=region, safesearch=safesearch))
            ), timeout=25)
            return {"query": q, "results": results}
        except asyncio.TimeoutError:
            return {"query": q, "error": "search backend timed out after 25s "
                    "(no route to DDG/startpage?) — try fewer queries or web.fetch a direct URL"}
        except Exception as e:
            return {"query": q, "error": str(e)}

    gathered = await asyncio.gather(*[_one(q) for q in queries])
    return {"success": True, "queries": list(gathered)}


async def tool_web_fetch(url: str, fmt: str = "markdown", timeout: int = 30) -> dict:
    """Fetch URL content. Formats: html, txt, markdown, js, css."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (NEXUS/1.0)"})
        raw = resp.text
        meta = {"status": resp.status_code, "size": len(raw), "content_type": resp.headers.get("content-type", "")}

        if fmt == "html":
            content = raw
        elif fmt == "txt":
            from html.parser import HTMLParser
            class T(HTMLParser):
                def __init__(self): super().__init__(); self.parts = []
                def handle_data(self, d): self.parts.append(d)
            p = T(); p.feed(raw)
            content = "\n".join(x.strip() for x in p.parts if x.strip())
        elif fmt == "markdown":
            try:
                import html2text
                h = html2text.HTML2Text()
                h.ignore_links = False
                h.body_width = 0
                content = h.handle(raw)
            except ImportError:
                from html.parser import HTMLParser
                class T(HTMLParser):
                    def __init__(self): super().__init__(); self.parts = []
                    def handle_data(self, d): self.parts.append(d)
                p = T(); p.feed(raw)
                content = "\n".join(x.strip() for x in p.parts if x.strip())
        elif fmt == "js":
            import re
            scripts = re.findall(r"<script[^>]*>(.*?)</script>", raw, re.DOTALL | re.IGNORECASE)
            content = "\n\n// ── script block ──\n\n".join(s.strip() for s in scripts if s.strip())
        elif fmt == "css":
            import re
            styles = re.findall(r"<style[^>]*>(.*?)</style>", raw, re.DOTALL | re.IGNORECASE)
            links = re.findall(r'<link[^>]+href=["\']([^"\']+\.css[^"\']*)["\']', raw, re.IGNORECASE)
            content = "\n\n/* ── inline style ── */\n\n".join(s.strip() for s in styles)
            if links:
                content += "\n\n/* Linked stylesheets: " + ", ".join(links) + " */"
        else:
            content = raw
        if resp.status_code >= 400:
            return {"success": False, "url": url,
                    "error": f"HTTP {resp.status_code} for {url}",
                    "meta": meta}
        return {"success": True, "url": url, "format": fmt, "meta": meta, "content": content}
    except Exception as e:
        # Log-found fix: httpx timeouts often stringify to "" — include the
        # exception class so logs/UI show *what* failed instead of blank.
        msg = f"{type(e).__name__}: {e}".strip()
        if not msg or msg.endswith(":"):
            msg = f"{type(e).__name__} (no detail — likely timeout after {timeout}s for {url})"
        return {"success": False, "url": url, "error": msg}


APP_ROOT = Path(__file__).parent.resolve()  # the running NEXUS runtime (code+web UI)


def _is_runtime_path(p) -> bool:
    """True when path p resolves inside the NEXUS runtime tree."""
    try:
        return Path(str(p)).expanduser().resolve().is_relative_to(APP_ROOT)
    except Exception:
        return False


def _runtime_ref_scan(text: str):
    """Best-effort scan of shell/code text for runtime-scope references.
    Returns the matched path token, or None. Covers absolute APP_ROOT,
    ~-expanded, and bare-repo-dirname tokens (e.g. `cd Nexus-Jarvis`)."""
    if not text:
        return None
    s = str(text)
    root = str(APP_ROOT)
    if root in s:
        return root
    try:
        home_root = str(Path("~").expanduser() / APP_ROOT.name)
        if home_root in s or ("~/" + APP_ROOT.name) in s:
            return "~/" + APP_ROOT.name
    except Exception:
        pass
    import re as _re2
    m = _re2.search(r"(^|[\s '\";=]|\./)(" + _re2.escape(APP_ROOT.name) + r")(/|\s|$)", s)
    if m:
        return m.group(2) + m.group(3).strip() or m.group(2)
    return None


def _runtime_touched(name: str, params: dict):
    """Which runtime path (if any) does this call target? None = clear."""
    params = params or {}
    if name == "exec.shell":
        cwd = params.get("cwd")
        if cwd and _is_runtime_path(cwd):
            return str(cwd)
        return _runtime_ref_scan(params.get("command", ""))
    if name == "exec.code":
        return _runtime_ref_scan(params.get("code", ""))
    return None


# ============================================================
#  TOOL REGISTRY & PARALLEL EXECUTOR
# ============================================================
TOOL_REGISTRY: Dict[str, Callable[..., Awaitable[dict]]] = {
    "exec.shell": tool_exec_shell,
    "exec.code": tool_exec_code,
    "web.search": tool_web_search,
    "web.fetch": tool_web_fetch,
    "browser": tool_browser,
    "memory.read": tool_memory_read,
    "memory.remember": tool_memory_remember,
    "memory.forget": tool_memory_forget,
    "memory.profile": tool_memory_profile,
}


async def execute_tool(name: str, params: dict, session_workspace: str = None,
                   allow_code_edit: bool = False, actor: str = "leader") -> dict:
    """Execute a single tool by name. allow_code_edit/actor are TRUSTED
    (caller-side): any model-supplied copies in params are stripped."""
    params = dict(params or {})
    params.pop("allow_code_edit", None)
    params.pop("actor", None)
    if name in ("exec.shell", "exec.code") and not allow_code_edit:
        hit = _runtime_touched(name, params)
        if hit:
            log_event("TOOL", f"runtime code-edit DENIED actor={actor} tool={name} ref={hit!r}",
                      level="WARNING")
            return {"success": False,
                    "error": f"runtime code-edit denied: {hit!r} is NEXUS runtime scope. "
                             f"Workers need an explicit code_edit grant on their task; "
                             f"only the leader edits the runtime freely."}
    if allow_code_edit and name in ("exec.shell", "exec.code"):
        hit = _runtime_touched(name, params)
        if hit:
            log_event("TOOL", f"runtime code-edit actor={actor} tool={name} ref={hit!r}")
    fn = TOOL_REGISTRY.get(name)
    if not fn:
        # Log-found fix: models hallucinate near-miss names (web_find x10,
        # None, web_fetch...). Answer with closest matches + the valid list
        # so the next iteration self-corrects instead of looping failures.
        try:
            import difflib
            valid = sorted(TOOL_REGISTRY.keys()) + ["parallel"]
            guess = difflib.get_close_matches(str(name or ""), valid, n=3, cutoff=0.5)
        except Exception:
            valid, guess = sorted(TOOL_REGISTRY.keys()), []
        hint = f" Did you mean: {', '.join(guess)}?" if guess else ""
        result = {"success": False,
                  "error": f"Unknown tool: {name}.{hint} Valid tools: {', '.join(valid)}"}
        log_tool_call(name, params or {}, result, 0.0)
        log_result("tool", f"{name} FAIL unknown-tool", result, level="WARNING")
        return result
    log_event("TOOL", f"start {name}", extra={"params": params})
    t0 = time.time()
    try:
        if name == "parallel":
            params = {**params, "allow_code_edit": bool(allow_code_edit),
                      "actor": actor}
        if name == "exec.code":
            params = {**params, "session_workspace": session_workspace}
        if name == "browser":
            params = {**params, "session_workspace": session_workspace}
        if name == "exec.shell" and session_workspace and not (params or {}).get("cwd"):
            # Session workspace is the home base (agents may still use absolute paths).
            params = {**(params or {}), "cwd": session_workspace}
        if name == "exec.shell" and (params or {}).get("cwd"):
            # Log-found fix: models pass phantom dirs ("/workspace") which
            # hard-fail the spawn with Errno 2. Fall back instead of failing.
            try:
                if not Path(str(params["cwd"])).is_dir():
                    fb = session_workspace if session_workspace and Path(session_workspace).is_dir() \
                        else str(Path.cwd())
                    log_event("TOOL", f"exec.shell bad cwd {params['cwd']!r} -> fallback {fb!r}",
                              level="WARNING")
                    params = {**(params or {}), "cwd": fb}
            except Exception:
                pass
        result = await fn(**params)
    except TypeError as e:
        result = {"success": False, "error": f"Invalid params for {name}: {e}"}
    except Exception as e:
        result = {"success": False, "error": f"{type(e).__name__}: {e}"}
    elapsed = round(time.time() - t0, 3)
    log_tool_call(name, params, result, elapsed)
    # Log a compact result summary (full blob already in TOOL line)
    try:
        if isinstance(result, dict) and result.get("success"):
            summary = _short(json.dumps(result, ensure_ascii=False, default=str), 300)
            log_result("tool", f"{name} OK in {elapsed:.3f}s {summary!r}")
        else:
            log_result("tool", f"{name} FAIL in {elapsed:.3f}s", result, level="WARNING")
    except Exception:
        pass
    return result


async def tool_parallel(calls: List[dict], session_workspace: str = None,
                    allow_code_edit: bool = False, actor: str = "leader") -> dict:
    """Execute multiple tool calls concurrently via asyncio.gather."""
    log_event("TOOL", f"parallel start fanout={len(calls)}",
              extra={"tools": [c.get("tool") for c in calls]})
    async def _one(call: dict):
        t0 = time.time()
        name = call.get("tool")
        params = call.get("params", {})
        result = await execute_tool(name, params, session_workspace,
                                allow_code_edit=allow_code_edit, actor=actor)
        return {"tool": name, "params": params, "result": result, "elapsed": round(time.time() - t0, 3)}

    results = await asyncio.gather(*[_one(c) for c in calls], return_exceptions=True)
    out = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            out.append({"tool": calls[i].get("tool"), "error": str(r)})
            log_event("TOOL", f"parallel item {calls[i].get('tool')} EXCEPTION: {r}", level="ERROR")
        else:
            out.append(r)
    n_ok = sum(1 for r in out if isinstance(r, dict) and r.get("result", {}).get("success"))
    log_event("TOOL", f"parallel done fanout={len(calls)} ok={n_ok} fail={len(calls) - n_ok}")
    log_result("tool", f"parallel fanout={len(calls)} ok={n_ok} fail={len(calls) - n_ok}", out)
    return {"success": True, "results": out}


# Register parallel after definition
TOOL_REGISTRY["parallel"] = tool_parallel


async def tool_runtime_restart(reason: str = "", delay_sec: int = 5,
                               _exec=None) -> dict:
    """Restart the NEXUS process (web backend or TUI) via os.execv so verified
    code edits take effect. Sessions are file-backed and survive; in-flight
    work is dropped. Leader-only (never offered to workers)."""
    import sys as _sys
    import threading as _th
    reason = (reason or "").strip()
    if not reason:
        return {"success": False,
                "error": "runtime.restart needs a non-empty 'reason' (what changed + how verified)."}
    try:
        delay = max(2, min(30, int(delay_sec or 5)))
    except Exception:
        delay = 5
    exe = _sys.executable
    argv = [_sys.executable] + list(_sys.argv)
    log_event("SYSTEM", f"runtime.restart in {delay}s pid={__import__('os').getpid()} "
                         f"reason={reason!r}", level="WARNING")

    def _do():
        try:
            (_exec or __import__("os").execv)(exe, argv)
        except Exception as e:
            log_event("SYSTEM", f"runtime.restart failed: {e}", level="ERROR")

    _th.Timer(delay, _do).start()
    return {"success": True, "restart_in_sec": delay, "pid": __import__("os").getpid(),
            "argv": argv, "reason": reason}


TOOL_SPEC_RUNTIME_RESTART = {
    "type": "function",
    "function": {
        "name": "runtime.restart",
        "description": (
            "LEADER-ONLY: restart the NEXUS process (web backend or TUI) so your "
            "VERIFIED code edits take effect. Sessions persist on disk; in-flight "
            "work is dropped. Rules: reason REQUIRED (what changed + how verified: "
            "py_compile / node --check / smokes); NEVER restart mid-round — wait "
            "for rounds to settle and warn the user first; workers can NEVER call "
            "this (it is not in their tools)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string",
                           "description": "REQUIRED: what changed + how it was verified"},
                "delay_sec": {"type": "integer",
                              "description": "Seconds before restart (2-30, default 5)",
                              "default": 5},
            },
            "required": ["reason"],
        },
    },
}
TOOL_REGISTRY["runtime.restart"] = tool_runtime_restart


# ============================================================
#  TOOL SCHEMAS FOR THE LLM (native function calling)
# ============================================================
# Property names match the Python parameter names exactly so the parsed
# arguments can be passed straight into execute_tool().
TOOL_SPEC_EXEC_SHELL = {
    "type": "function",
    "function": {
        "name": "exec.shell",
        "description": "Run a shell command. Auto-detects shell (bash/pwsh). Returns exit code, stdout, stderr.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to run"},
                "shell_type": {"type": "string", "description": "Optional shell name, e.g. bash, pwsh"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 120},
                "cwd": {"type": "string", "description": "Working directory (optional)"},
            },
            "required": ["command"],
        },
    },
}

TOOL_SPEC_EXEC_CODE = {
    "type": "function",
    "function": {
        "name": "exec.code",
        "description": "Write source code to the workspace and run it. Returns stdout, stderr, exit code.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Full source code to execute"},
                "language": {"type": "string", "description": "python, javascript, c, cpp, csharp, pwsh",
                             "default": "python"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 120},
            },
            "required": ["code"],
        },
    },
}

TOOL_SPEC_WEB_SEARCH = {
    "type": "function",
    "function": {
        "name": "web.search",
        "description": "Web search (no API key). Pass MULTIPLE queries at once; they run concurrently.",
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {"type": "array", "items": {"type": "string"},
                            "description": "List of search queries"},
                "max_results": {"type": "integer", "description": "Results per query", "default": 8},
            },
            "required": ["queries"],
        },
    },
}

TOOL_SPEC_WEB_FETCH = {
    "type": "function",
    "function": {
        "name": "web.fetch",
        "description": "Fetch a URL. Formats: markdown (default), txt, html, js, css.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL to fetch"},
                "fmt": {"type": "string", "description": "markdown, txt, html, js or css",
                        "default": "markdown"},
                "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 30},
            },
            "required": ["url"],
        },
    },
}

TOOL_SPEC_PARALLEL = {
    "type": "function",
    "function": {
        "name": "parallel",
        "description": "Run several tool calls concurrently. Use for independent operations.",
        "parameters": {
            "type": "object",
            "properties": {
                "calls": {
                    "type": "array",
                    "description": "Tool calls to run in parallel",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tool": {"type": "string", "description": "Tool name, e.g. exec.shell"},
                            "params": {"type": "object", "description": "Tool parameters"},
                        },
                        "required": ["tool"],
                    },
                },
            },
            "required": ["calls"],
        },
    },
}

class TreeBudget:
    """Shared cap for one delegation tree (depth>=2 workers only — the
    leader's own explicitly-requested fan-out is never truncated by it).
    Also carries the session-wide used-name registry (shared set object):
    hologram/radio identity IS the name, so Gatherer1 in two sub-rounds
    would overwrite one body — duplicates are rejected, never merged.
    Same event-loop thread: check+claim never splits across an await."""
    def __init__(self, limit: int, names=None):
        self.limit = max(1, int(limit))
        self.used = 0
        self.names = names if names is not None else set()
    def room(self) -> int:
        return max(0, self.limit - self.used)
    def claim_names(self, ids: list) -> list:
        """Reserve worker names session-wide. Returns dupes (already used);
        clean ids are registered. Case-insensitive (matches validation)."""
        seen = {str(n).lower() for n in self.names}
        dupes = [i for i in ids if str(i).lower() in seen]
        if not dupes:
            for i in ids:
                self.names.add(str(i))
        return dupes
    def claim(self, n: int) -> int:
        """Claim up to n slots. Returns granted count (0 = exhausted)."""
        g = max(0, min(int(n), self.limit - self.used))
        self.used += g
        return g


def resolve_tree_budget(params: dict) -> int:
    """Leader-declared nested budget for one tree (10x10 hierarchies need it).
    Falls back to the configured default; clamped to the hard ceiling so one
    call can never promise an unbounded tree."""
    try:
        want = int((params or {}).get("tree_budget") or 0)
    except Exception:
        want = 0
    if want <= 0:
        return CFG.recursive_max_tree
    return max(1, min(CFG.recursive_tree_ceiling, want))


def scaled_round_timeout(n_workers: int) -> float:
    """Log-found rule shared by top-level and nested supervisors: a fixed
    timeout massacres big rounds (20 workers x ~30s/iter x 12 iters /
    6 LLM slots ~= 1200s). Scale with size, floor at the configured base."""
    return max(CFG.round_timeout_sec, 60.0 * max(1, int(n_workers)))


def recursive_guidance(enabled: bool) -> str:
    """GENERATED leader guidance for recursive (nested) delegation — built
    from the live CFG caps so code and prompt can never drift apart.
    Injected into the leader system prompt every turn (mode-aware)."""
    d, t, tree = CFG.recursive_max_depth, CFG.recursive_max_tasks, CFG.recursive_max_tree
    if not enabled:
        return (
            "# ── RECURSIVE DELEGATION: OFF ──\n"
            "NESTED agents exist, but NOT in this session: workers CANNOT call "
            "agent.delegate here (the depth guard rejects it — tell the user to "
            "flip the NEST toggle next to the composer, or /recursive on, to allow "
            "workers that spawn their own sub-workers). Never promise nesting "
            "while it is off; offer it when a task would genuinely fan out twice.\n")
    return (
        "# ── RECURSIVE DELEGATION: ON (NEST MODE — you are the expert) ──\n"
        f"Workers in this session CAN call agent.delegate themselves (nesting to "
        f"depth {d}: your workers are depth 1, their children depth 2, "
        f"grandchildren depth 3 = max, depth-{d} workers get the call REJECTED).\n"
        "EFFICIENCY (you know the mode is on — use it surgically, not by default): "
        "prefer flat 4–6 workers; nest ONLY when a subtask itself holds 2+ "
        "independent workstreams. Announce the depth plan with counts "
        "(e.g. \"6 miners, 2 of them nesting 3 diggers each\").\n"
        f"CAPS (hard, enforced): ≤{t} tasks per nested call; ≤{tree} nested "
        "workers per tree (over-budget calls are TRUNCATED and the result names "
        "the REMAINDER with a chain-or-report order — obey it literally).\n"
        "PRE-FLIGHT MATH (mandatory before any nested plan): multiply parents × "
        f"children-per-parent yourself; pass tree_budget=product+margin on the TOP "
        f"delegate call (default covers {tree} nested, ceiling {CFG.recursive_tree_ceiling}) — "
        f"an undeclared 10×10 against a dry budget fails the last parents, who then "
        f"work solo. Over ceiling: narrow the scope or say so BEFORE launching. "
        "NAMES are session-unique across ALL rounds (a reused name overwrites a "
        "hologram body and is REJECTED): order every worker to prefix children "
        "with its own id (e.g. SubAgent5 spawns 'S5-G1'…'S5-G10').\n"
        "BRIEF ORDER / NEST-FIRST (enforce it in every nesting brief): step 1 = call "
        "agent.delegate for the children; step 2 = solo checks WHILE they run; "
        "step 3 = verify + fold. A worker that solos first burns its round clock "
        "and may never nest.\n"
        "SYNC SEMANTICS: a nested round runs INSIDE its parent worker and returns "
        "as that tool call's result — no background inside nesting; the parent "
        "waits. Sub-round ids look like R7/D03#1 (parent round / parent worker / "
        "nest counter).\n"
        "ISOLATION: each sub-round has its OWN radio + plan. You cannot hear "
        "sub-round radio, and siblings in different sub-rounds cannot hear each "
        "other — put cross-cutting facts in shared_context, and make each brief "
        "fully self-contained.\n"
        "SYNTHESIS DUTY: verify every child against its OUTPUT contract, then "
        "fold children into YOUR result (the leader never sees raw child output). "
        "Count/honesty rules apply recursively: report ok/failed per level, "
        "never stall, never claim nested work is \"still running\" once its "
        "round result is in your history.\n")


def selfedit_guidance() -> str:
    """GENERATED leader guidance for SELF-EVOLUTION (edit own runtime code).
    Injected into the leader system prompt every turn. evolution.py stays the
    prompt/memory layer (rules, memories, /evolve versions) — THIS block is the
    code-editing mandate: the leader IS the running NEXUS process."""
    r = str(APP_ROOT)
    return (
        "# ── SELF-EVOLUTION: YOU ARE THE RUNNING CODE ──\n"
        f"You are the live NEXUS process itself, running from {r}. You can read "
        "and edit EVERY file of your own runtime — backend AND frontend:\n"
        f"- backend: {r}/main.py (agent, tools, rounds), {r}/webui.py (web server), "
        f"{r}/evolution.py (prompt/memory evolution layer — stays as-is, use it, "
        "do not replace it), {r}/nexus_browser.py, {r}/system_prompt.txt (this file)\n"
        f"- frontend: {r}/web/index.html, {r}/web/app.js, {r}/web/entity3d.js, "
        f"{r}/web/styles.css (bump ?v= versions in index.html after editing)\n"
        f"- state (read mostly, never corrupt): ~/.nexus/sessions/, ~/.nexus/memory.json, "
        "~/.nexus/evolution/\n"
        "HOW TO EVOLVE YOURSELF: 1) READ the file first (exec.shell sed/cat or "
        "Read-equivalent). 2) Make MINIMAL diffs (python scripts editing exact "
        "strings, never blind rewrites). 3) VERIFY every change: "
        "`.venv/bin/python -m py_compile main.py webui.py` for backend, "
        "`node --check web/entity3d.js web/app.js` for frontend, plus the repo "
        "smoke/unit tests in /tmp when they touch your change. 4) Only then call "
        "runtime.restart with reason=what+how-verified — never mid-round (wait "
        "for rounds to settle, warn the user: in-flight work is dropped, "
        "sessions persist). 5) Report what changed + verification evidence.\n"
        "EVOLUTION LAYER (stays): keep using memory.read/remember for durable "
        "lessons and /evolve (history/rollback/why/revalidate) for prompt "
        "generations — code edits are for behavior the prompt layer cannot fix.\n"
        "SUBAGENTS: they CANNOT touch the runtime by default (denied + logged). "
        "Grant surgically with task code_edit:true ONLY when the mission needs "
        "it (per task, never blanket, never inherited). THEY CAN NEVER RESTART "
        "— runtime.restart is not in their tools; only you restart, after YOU "
        "verify their diffs. Never exfiltrate secrets (.env keys stay secret).\n"
        + _self_knowledge_live())


def _self_knowledge_live() -> str:
    """Deep self-knowledge with LIVE values (budgets, caps, identity) so the
    leader can plan precisely and edit itself accurately. Generated from CFG +
    code constants — never hardcode these numbers anywhere else."""
    try:
        import os as _os
        model = CFG.ollama_model if CFG.provider == "ollama" else CFG.model
        conc = _os.getenv("LLM_MAX_CONCURRENCY", "6")
        lines = [
            "# ── DEEP SELF-KNOWLEDGE (live — plan with these exact numbers) ──",
            f"IDENTITY: provider={CFG.provider} model={model} think={CFG.think_effort} "
            f"temp={CFG.temperature} max_tokens={CFG.max_tokens} (cloud clamps >65536).",
            f"BUDGETS: leader_iters={CFG.max_iterations} worker_iters={CFG.subagent_max_iterations} "
            f"delegate_rounds/turn={CFG.delegate_max_rounds} llm_parallel={conc} "
            f"(bursts beyond this queue — big rounds take waves).",
            f"ROUND PHYSICS: quiet_close={CFG.round_quiet_sec}s idle+silent "
            f"timeout=max({CFG.round_timeout_sec}s, 60s×workers) linger={CFG.worker_linger_sec}s "
            f"radio_cap={RADIO_MAX_MSGS}x{RADIO_MAX_LEN}chars plan_cap={PLAN_MAX_STEPS} "
            "drain=8msgs/step summaries=800chars tool_blobs=20000chars.",
            "SESSIONS: history_restore=40msgs switch wipes UI hologram; workspace="
            "~/.nexus/sessions/<sid>/workspace; memory_facts_cap=200 (workers read-only).",
            "HOLOGRAM KEYS: worker_key=sid[-6:]+name labels=1-2words/≤16chars colors=#rrggbb "
            "session-unique (dupes rejected); retired bodies linger, oldest evicted past 12.",
            "FRONTEND: web/entity3d.js + web/app.js + web/styles.css served via web/index.html "
            "— ALWAYS bump ?v= after editing or browsers run stale code.",
            "TEXT-CALL FALLBACK: native tool_calls preferred; Hermes-style <tool_call> text "
            "is adopted+executed automatically (truncated JSON salvaged) — never paste raw calls as answer.",
            "LOGS: nexus.log (2MB×3 rotated) is your flight recorder — diagnose yourself there first.",
            "DEAD KEYS (do not rely): STEP_LIMIT/TOOL_LIMIT exist in .env but NOTHING reads them.",
            "RESTART: os.execv(same argv) — sessions survive on disk, in-flight work does NOT; "
            "web (uvicorn 127.0.0.1:8777) and TUI both re-enter via main.py.",
        ]
        return "\n".join(lines) + "\n"
    except Exception:
        return ""


def worker_codeedit_guidance(granted: bool) -> str:
    """GENERATED worker-side code-edit rights (appended always: granted block
    or one-line denial so the worker never guesses)."""
    if not granted:
        return ("RUNTIME SCOPE: NEXUS own code is OFF-LIMITS to you (edits are "
                "denied + logged). Work inside your session workspace; if the "
                "mission truly needs runtime edits, say so on the radio/your "
                "result and let the leader re-grant.")
    r = str(APP_ROOT)
    return ("RUNTIME CODE-EDIT GRANTED (explicit, this task only): you may "
            f"read/edit files under {r} (backend + web/ frontend). Rules: "
            "read-first, minimal diffs, VERIFY (py_compile / node --check / "
            "relevant smokes) and REPORT diffs + evidence in your result. You "
            "can NEVER restart — no such tool exists for you; the leader "
            "restarts after verifying. Never touch secrets (.env values stay "
            "secret). Grant is NOT inherited by YOUR sub-workers: set "
            "code_edit:true per task if they need it too.")


def worker_nest_guidance(depth: int) -> str:
    """GENERATED worker-side delegation manual — leader-grade instructions,
    worker-voiced, minus leader-only powers (user finals, memory writes,
    restart, background delegation, sessions/evolve). Appended only when the
    worker is actually offered agent.delegate."""
    d, t, tree = CFG.recursive_max_depth, CFG.recursive_max_tasks, CFG.recursive_max_tree
    return (
        f"DELEGATION MANUAL (you are depth {depth} of {d} — a worker that commands "
        f"its own workers, exactly like the leader commands you). Your children will "
        f"be depth {depth + 1}; depth-{d} workers cannot nest further.\n"
        "1) NEST-FIRST ORDER: if your brief orders sub-workers, agent.delegate is "
        "your FIRST tool call, before any solo searching — nested rounds take "
        "minutes and solo work first burns the round clock. Solo checks go WHILE "
        "children run only if your brief explicitly allows a second phase.\n"
        "2) EXACT COUNTS OVERRIDE: a named number (\"appoint 10\") beats every "
        f"default — spawn EXACTLY that many across chained calls (≤{t} each), never "
        "silently fewer. A truncated result names the REMAINDER and orders you to "
        "call again with exactly those tasks.\n"
        "3) BRIEF FRAMEWORK — every child brief MUST carry these 6 labeled lines, "
        "or the call bounces and you burn a turn: ROLE (specialist title) / GOAL "
        "(one verifiable sentence) / CONTEXT (only the facts THIS child needs — "
        "never your whole history) / METHOD (exact tools + parameters + timeouts, "
        "e.g. web.search queries=[...] max_results=5; web.fetch fmt=txt timeout=20) / "
        "OUTPUT (exact contract, e.g. '5 lines max: URL | snippet') / TALK "
        "(coordination pattern + exit, e.g. pattern=FAN_OUT; role=peer; exit when "
        "all report). Vague briefs produce hallucinated meta-work — write them "
        "like the brief you yourself received.\n"
        "4) VALIDATION (hard, enforced — a bounced call returns an error naming "
        "the bad tasks, fix and re-call): name = REQUIRED, 1–2 words ≤16 chars "
        "each (letters/digits/_/-/space); color = REQUIRED hex #rrggbb worn on "
        "its hologram body; brief = REQUIRED non-empty; NAMES are session-unique "
        "across ALL rounds (a reused name overwrites a hologram body and is "
        "REJECTED) — ALWAYS prefix children with your own id (e.g. 'S5-G1'…'S5-G10'), "
        "never bare 'G1'…'G10'.\n"
        "5) PRE-FLIGHT MATH: your subtree counts against the SAME tree budget "
        f"(≤{tree} nested per tree) — multiply planned children × grandchildren "
        "before launching; over budget the last calls fail dry and those children "
        "never run, so narrow scope or report the cap instead of launching blind.\n"
        "6) SYNC SEMANTICS: the nested round runs INSIDE you and returns as THIS "
        "tool call's result — you wait (no background inside nesting). Sub-round "
        "ids look like R7/D03#1 (parent round / parent worker / counter).\n"
        "7) ISOLATION: each sub-round has its OWN radio + plan which you cannot "
        "hear from outside and siblings cannot share — every brief fully "
        "self-contained, shared facts repeated per child.\n"
        "8) SYNTHESIS DUTY: VERIFY each child against its OUTPUT contract, then "
        "fold all children into YOUR result (your parent never sees raw child "
        "output). Report ok/failed counts per level, partials included.\n"
        "9) HONESTY: never claim children exist before their round result is in "
        "your history; never say nested work is 'still running' once its result "
        "arrived; announce launches as launched. Stalling ('results will arrive "
        "soon' AFTER they arrived) is a failure.\n"
        "10) WHAT YOU LACK (do not attempt): final answers to any user, memory "
        "writes, restarts (no such tool for you — the leader restarts after "
        "verifying), background delegation (yours is always sync), sessions or "
        "evolution management. Keep nested rounds small and pointed; stop the "
        "moment your brief is met.")


TOOL_SPEC_DELEGATE = {
    "type": "function",
    "function": {
        "name": "agent.delegate",
        "description": (
            "MANDATORY when 2+ independent workstreams exist — doing them yourself is a failure. "
            "Spawn worker subagents that run CONCURRENTLY, each with an isolated context. "
            "Build every brief with the 6-line framework from your system prompt "
            "(ROLE/GOAL/CONTEXT/METHOD/OUTPUT/TALK) — vague briefs get rejected. "
            "Every task ALSO needs name + color (both REQUIRED, enforced). "
            "Workers can use exec/web tools but CANNOT delegate further. Their outputs "
            "come back as this tool's result — verify each against its OUTPUT contract, "
            "then synthesize the single final answer yourself. "
            "BACKGROUND: this call returns at once with {background, round, workers} "
            "while the round runs on — you stay free, so end your turn now (tell the "
            "user work runs in background). Results arrive later as a round update; "
            "then verify + synthesize + report. "
            "The round rules: workers that finish early linger (they do NOT exit) "
            "and wake on any later radio/plan traffic, so later messages always "
            "land — the round closes itself when all are idle and quiet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "Independent subtasks (server honors the FULL list — "
                                   "when the user names an exact worker count, send EXACTLY "
                                   "that many tasks; default 4-6 per call, chain further "
                                   "rounds for the rest)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string",
                                     "description": "REQUIRED: worker display name, max TWO words "
                                                    "(letters/digits/_/-/space, e.g. 'Data Miner'). "
                                                    "Shown under its hologram body; used to address it "
                                                    "on the radio. Duplicates rejected."},
                            "color": {"type": "string",
                                      "description": "REQUIRED hex '#rrggbb' expressing what this "
                                                     "worker does (e.g. '#b48cff' research). Worn "
                                                     "INSTEAD of gold on its hologram body."},
                            "brief": {"type": "string",
                                      "description": "REQUIRED, non-empty: self-contained instructions "
                                                     "with the exact goal AND which tool to use "
                                                     "(worker sees nothing else). Empty briefs are rejected."},
                            "context": {"type": "string",
                                        "description": "Background facts the worker needs"},
                            "code_edit": {"type": "boolean",
                                          "description": "OPTIONAL, default false: grant THIS worker "
                                                         "runtime code-edit rights (read/edit NEXUS own "
                                                         "code incl. web UI). Grant surgically, per task, "
                                                         "never blanket. Workers can NEVER restart."},
                        },
                        "required": ["brief", "name", "color"],
                    },
                },
            },
            "required": ["tasks"],
        },
    },
}

TOOL_SPEC_DELEGATE_NEST = {
    "type": "function",
    "function": {
        "name": "agent.delegate",
        "description": (
            "NEST MODE: spawn YOUR OWN sub-workers (they run one level deeper). "
            "The nested round runs INSIDE you and returns as this call's result — "
            "you wait, then verify each child against its OUTPUT contract and "
            "fold them into your result. When your brief names an EXACT count, "
            "spawn EXACTLY that many (chain further calls for any remainder). "
            "Caps enforced: depth/"
            "tasks/tree (see your system prompt). Briefs must be self-contained: "
            "6-line framework (ROLE/GOAL/CONTEXT/METHOD/OUTPUT/TALK) + REQUIRED "
            "name (max TWO words, unique) + color (#rrggbb) per task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "Sub-subtasks (truncated to the nested per-call cap with a warning)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string",
                                     "description": "REQUIRED: worker display name, max TWO words"},
                            "color": {"type": "string",
                                      "description": "REQUIRED hex '#rrggbb'"},
                            "brief": {"type": "string",
                                      "description": "REQUIRED, non-empty: self-contained instructions"},
                            "context": {"type": "string",
                                        "description": "Background facts the sub-worker needs"},
                            "code_edit": {"type": "boolean",
                                          "description": "OPTIONAL, default false: grant THIS sub-worker "
                                                         "runtime code-edit rights. Set it explicitly per "
                                                         "task when the mission needs it (never inherited, "
                                                         "never blanket). No worker can ever restart."},
                        },
                        "required": ["brief", "name", "color"],
                    },
                },
            },
            "required": ["tasks"],
            "tree_budget": {"type": "integer",
                            "description": "OPTIONAL: nested-worker budget for this tree "
                                           "(default covers 100; declare N×M+margin when the "
                                           "user demands an N×M hierarchy, max 500)"},
        },
    },
}

# Leader sees everything including delegation. Workers get tools but no
# delegation by default (depth guard — enforced again in code; NEST MODE
# offers TOOL_SPEC_DELEGATE_NEST instead), plus the round radio and joint
# plan so they can talk and coordinate with each other.
TOOL_SPEC_SAY = {
    "type": "function",
    "function": {
        "name": "agent.say",
        "description": (
            "Send a short radio message to peer workers in this round "
            "(they read it before their next step). to='all' broadcasts, "
            "or name one worker id. Workers only — max ~100 messages/round."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Worker id or 'all'", "default": "all"},
                "text": {"type": "string", "description": "Message (max ~500 chars)"},
            },
            "required": ["text"],
        },
    },
}

TOOL_SPEC_PLAN = {
    "type": "function",
    "function": {
        "name": "agent.plan",
        "description": (
            "Joint plan shared by all workers in this round. Actions: add "
            "(new step, needs title), claim (take a step), done (finish it, "
            "optional note), note (annotate), list (read current plan). "
            "Claim before doing so peers don't duplicate work."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string",
                           "description": "add, claim, done, note or list", "default": "list"},
                "step_id": {"type": "string", "description": "Step id, e.g. s1"},
                "title": {"type": "string", "description": "Title for add"},
                "note": {"type": "string", "description": "Note for done/note"},
            },
            "required": ["action"],
        },
    },
}

TOOL_SPEC_RADIO_LIST = {
    "type": "function",
    "function": {
        "name": "radio.list",
        "description": (
            "LEADER-ONLY: list every round group (top-level + nested) with "
            "live worker states, message counts and done flags. Call FIRST to "
            "learn group ids, then radio.read / radio.send into any of them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "active_only": {"type": "boolean",
                                "description": "Only rounds still open (default false)",
                                "default": False},
            },
        },
    },
}
TOOL_SPEC_RADIO_READ = {
    "type": "function",
    "function": {
        "name": "radio.read",
        "description": (
            "LEADER-ONLY: read a round group's radio traffic (any group from "
            "radio.list, any depth). Use since_seq to page; includes the joint "
            "plan unless include_plan=false."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "round": {"type": "string",
                          "description": "REQUIRED: round id, e.g. R3 or R3/D02#1"},
                "since_seq": {"type": "integer",
                              "description": "Only messages after this seq (default 0)",
                              "default": 0},
                "limit": {"type": "integer",
                          "description": "Max messages back (1-50, default 20)",
                          "default": 20},
                "include_plan": {"type": "boolean",
                                 "description": "Include the joint plan snapshot (default true)",
                                 "default": True},
            },
            "required": ["round"],
        },
    },
}
TOOL_SPEC_RADIO_SEND = {
    "type": "function",
    "function": {
        "name": "radio.send",
        "description": (
            "LEADER-ONLY: speak INTO a round group as 'leader' (any group from "
            "radio.list). to='all' (default) wakes every lingering worker; name "
            "one worker id to wake just it. Closed rounds reject the message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "round": {"type": "string",
                          "description": "REQUIRED: round id, e.g. R3 or R3/D02#1"},
                "to": {"type": "string",
                       "description": "Worker id or 'all' (default 'all')",
                       "default": "all"},
                "text": {"type": "string",
                         "description": "REQUIRED: message (max ~500 chars)"},
            },
            "required": ["round", "text"],
        },
    },
}
TOOL_SPEC_MEM_READ = {
    "type": "function",
    "function": {
        "name": "memory.read",
        "description": "Read long-term memory: user profile + stored facts. Use when the user asks what you know about them, or before personalizing.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "Keyword filter (optional)"}}},
    },
}

TOOL_SPEC_MEM_REMEMBER = {
    "type": "function",
    "function": {
        "name": "memory.remember",
        "description": "Store ONE durable fact (user name, preference, project, learning). Survives across sessions.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string", "description": "The fact to remember"}},
                       "required": ["text"]},
    },
}

TOOL_SPEC_MEM_FORGET = {
    "type": "function",
    "function": {
        "name": "memory.forget",
        "description": "Delete stored facts containing the given text.",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string", "description": "Substring to match"}},
                       "required": ["text"]},
    },
}

TOOL_SPEC_MEM_PROFILE = {
    "type": "function",
    "function": {
        "name": "memory.profile",
        "description": "Set one user-profile field (name, role, timezone, language…). Empty value reads the profile.",
        "parameters": {"type": "object",
                       "properties": {"key": {"type": "string"}, "value": {"type": "string"}}},
    },
}

WORKER_TOOLS = [TOOL_SPEC_EXEC_SHELL, TOOL_SPEC_EXEC_CODE,
                TOOL_SPEC_WEB_SEARCH, TOOL_SPEC_WEB_FETCH, TOOL_SPEC_BROWSER, TOOL_SPEC_PARALLEL,
                TOOL_SPEC_SAY, TOOL_SPEC_PLAN, TOOL_SPEC_MEM_READ]
LEADER_TOOLS = [TOOL_SPEC_EXEC_SHELL, TOOL_SPEC_EXEC_CODE,
                TOOL_SPEC_WEB_SEARCH, TOOL_SPEC_WEB_FETCH, TOOL_SPEC_BROWSER, TOOL_SPEC_PARALLEL,
                TOOL_SPEC_DELEGATE, TOOL_SPEC_RUNTIME_RESTART,
                TOOL_SPEC_RADIO_LIST, TOOL_SPEC_RADIO_READ, TOOL_SPEC_RADIO_SEND,
                TOOL_SPEC_MEM_READ, TOOL_SPEC_MEM_REMEMBER,
                TOOL_SPEC_MEM_FORGET, TOOL_SPEC_MEM_PROFILE]


# ============================================================
#  LLM PROVIDER ABSTRACTION
# ============================================================
def _to_ollama_messages(messages: List[dict]) -> List[dict]:
    """Translate our normalized history into shapes ollama's client accepts.

    Our tool calls are flat ({id, name, arguments}) but ollama's Message
    model requires {function: {name, arguments}} — sending ours back verbatim
    fails client-side validation on the 2nd turn of any tool loop. Never raises.
    """
    out: List[dict] = []
    for m in messages or []:
        try:
            role = m.get("role", "user")
            if role == "assistant" and m.get("tool_calls"):
                tcs = []
                for tc in (m.get("tool_calls") or []):
                    args = (tc or {}).get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    item: dict = {"function": {"name": (tc or {}).get("name", ""),
                                               "arguments": args or {}}}
                    if (tc or {}).get("id"):
                        item["id"] = tc["id"]
                    tcs.append(item)
                out.append({"role": "assistant", "content": m.get("content", "") or "",
                            "tool_calls": tcs})
            elif role == "tool":
                out.append({"role": "tool", "content": m.get("content", "") or ""})
            else:
                out.append({"role": role, "content": m.get("content", "") or ""})
        except Exception:
            try:
                out.append({"role": m.get("role", "user"), "content": str(m.get("content", ""))})
            except Exception:
                pass
    return out


def _is_error_content(content: str) -> bool:
    c = content or ""
    return c.startswith("[LLM error]") or c.startswith("[Ollama error]")


_HFAIL = object()


def _hfind(seg: str, tag: str, lo: int, hi: int):
    """Find <tag = value> in seg[lo:hi]; returns (value, end) or (None, -1)."""
    i = seg.lower().find("<" + tag, lo, hi)
    if i < 0:
        return None, -1
    j = i + 1 + len(tag)
    while j < hi and seg[j] in " \t\r\n":
        j += 1
    if j >= hi or seg[j] != "=":
        return None, -1
    j += 1
    while j < hi and seg[j] in " \t\r\n":
        j += 1
    k = j
    while k < hi and seg[k] not in " \t\r\n>":
        k += 1
    if k <= j:
        return None, -1
    return seg[j:k].strip(), k + (1 if k < hi and seg[k] == ">" else 0)


def _hjson(seg: str, base: int):
    """Parse one JSON value at seg[base:] (leading ws skipped).
    Returns (value, abs_end, partial). Partial=True when a truncated array
    was salvaged (complete items kept, broken tail dropped)."""
    rest0 = seg[base:]
    p = base + (len(rest0) - len(rest0.lstrip()))
    try:
        obj, end = json.JSONDecoder().raw_decode(seg[p:])
        return obj, p + end, False
    except Exception:
        pass
    if p < len(seg) and seg[p] == "[":
        arr, q = [], p + 1
        while q < len(seg):
            while q < len(seg) and seg[q] in " \t\r\n,":
                q += 1
            if q >= len(seg) or seg[q] == "]":
                break
            try:
                o, e = json.JSONDecoder().raw_decode(seg[q:])
            except Exception:
                break
            arr.append(o)
            q += e
        if arr:
            return arr, q, True
    return _HFAIL, p, False


def _extract_text_calls(text: str, valid_names) -> tuple:
    """Parse Hermes-style TEXT tool calls some models emit instead of native
    calls: <tool_call> <function=NAME> <parameter=P> <json> (</tool_call>).
    Returns (stripped_text, calls). Unknown names / unparseable JSON are left
    in the text (never execute blind)."""
    if not text or "<tool_call>" not in text.lower():
        return text, []
    valid = {str(n) for n in (valid_names or []) if n}
    out_calls, spans = [], []
    low, idx, n = text.lower(), 0, 0
    # Span boundaries come from PARSED JSON ends, never from </tool_call>
    # (log-found: a bogus closer inside a brief truncated the strip and
    # 27KB kept leaking). A truncated tail is swallowed to the next opener.
    while True:
        s = low.find("<tool_call>", idx)
        if s < 0:
            break
        nxt = low.find("<tool_call>", s + 11)
        seg_end = nxt if nxt >= 0 else len(text)
        seg = text[s:seg_end]
        name, hend = _hfind(seg, "function", 0, min(500, len(seg)))
        params, ok_parse, value_end, partial = {}, bool(name and name in valid), 0, False
        if ok_parse:
            ppos = hend
            while True:
                pname, pend = _hfind(seg, "parameter", ppos, len(seg))
                if pname is None:
                    break
                val, vend, is_partial = _hjson(seg, pend)
                if val is _HFAIL:
                    ok_parse = False
                    break
                params[pname] = val
                value_end = vend
                if is_partial:
                    partial = True
                    break
                ppos = vend
        if ok_parse and params:
            span_end = s + value_end
            if partial:
                span_end = seg_end
            else:
                q = span_end
                while q < seg_end and text[q] in " \t\r\n":
                    q += 1
                if text[q:q + 12].lower() == "</tool_call>":
                    q += 12
                span_end = q
            if partial:
                params["_recovered_partial"] = True
            out_calls.append({"id": f"text_{n}", "name": name,
                              "arguments": json.dumps(params, ensure_ascii=False)})
            spans.append((s, span_end))
            n += 1
            idx = seg_end
        else:
            idx = s + 11
    if not out_calls:
        return text, []
    stripped = text
    for s, e in sorted(spans, reverse=True):
        stripped = stripped[:s] + stripped[e:]
    while "\n\n\n" in stripped:
        stripped = stripped.replace("\n\n\n", "\n\n")
    return stripped.strip(), out_calls


def _adopt_text_calls(content: str, specs, who: str, sid: str):
    """Adopt text-formatted calls as real ones (log-found: models emitting
    <tool_call> text had their 31KB pseudo-calls delivered as the ANSWER).
    Returns (content, calls) with blocks stripped from content."""
    names = [s.get("function", {}).get("name", "") for s in (specs or [])]
    stripped, calls = _extract_text_calls(content or "", names)
    if calls:
        log_event("LLM" if who == "leader" else "SUBAGENT",
                  f"adopted {len(calls)} text tool call(s) sid={sid} "
                  f"({', '.join(c['name'] for c in calls)}) — stripped from answer",
                  level="WARNING")
    return stripped, calls


def _is_billing_error(content: str) -> bool:
    """402 / free-usage / credits errors must NOT be retried — retrying a
    billing refusal 3x only burns time (log shows 3x 402 bursts)."""
    c = (content or "").lower()
    return ("402" in c or "free usage" in c or "usage credits" in c
            or "pay as you go" in c or "upgrade for included" in c)


MODEL_UNAVAILABLE = ("The model endpoint is temporarily failing on Ollama's side (cloud error). "
                     "Nothing is wrong with NEXUS — please send your message again in a moment.")

MODEL_BILLING = ("This Ollama cloud model is not included in free usage (402) — "
                 "add usage credits at https://ollama.com/settings or switch to a free model "
                 "with /models (or /model <name>). Nothing is wrong with NEXUS itself.")


_llm_sem: Optional[asyncio.Semaphore] = None


def _llm_semaphore() -> asyncio.Semaphore:
    """Cap concurrent model calls (log-found fix: Ollama 429 bursts when the
    leader + a worker wave all fire at once). Env LLM_MAX_CONCURRENCY."""
    global _llm_sem
    if _llm_sem is None:
        try:
            n = max(1, int(os.getenv("LLM_MAX_CONCURRENCY", "6")))
        except Exception:
            n = 6
        _llm_sem = asyncio.Semaphore(n)
    return _llm_sem


class LLMProvider:
    """Unified interface for OpenAI-compatible and Ollama APIs."""
    def __init__(self, cfg: Config = CFG):
        self.cfg = cfg

    async def chat(self, messages: List[dict], tools: Optional[List[dict]] = None,
                 retries: int = 2) -> dict:
        t0 = time.time()
        log_event("LLM", f"request provider={self.cfg.provider} "
                         f"model={self.cfg.model if self.cfg.provider != 'ollama' else self.cfg.ollama_model} "
                         f"msgs={len(messages)} tools={len(tools) if tools else 0}")
        out: dict = {"content": "", "tool_calls": [], "thinking": "", "raw": None}
        attempts = 0
        # One slot per in-flight call: bursts (leader + worker wave) queue
        # instead of slamming the endpoint into 429s. Never held across turns.
        async with _llm_semaphore():
            while True:
                attempts += 1
                if self.cfg.provider == "ollama":
                    out = await self._ollama(messages, tools)
                else:
                    out = await self._openai(messages, tools)
                content = out.get("content", "") or ""
                if not _is_error_content(content) or attempts > retries:
                    break
                if _is_billing_error(content):
                    # Billing refusal will never succeed on retry — stop now.
                    log_event("LLM", f"attempt {attempts} billing refusal (402), "
                                     f"not retrying", level="ERROR")
                    break
                wait = 1.5 * attempts
                log_event("LLM", f"attempt {attempts} failed ({_short(content, 120)}), "
                                 f"retrying in {wait:.1f}s", level="WARNING")
                await asyncio.sleep(wait)
        latency = round(time.time() - t0, 3)
        content = out.get("content", "") or ""
        n_tc = len(out.get("tool_calls", []) or [])
        err = content if _is_error_content(content) else None
        model_name = self.cfg.ollama_model if self.cfg.provider == "ollama" else self.cfg.model
        log_llm_call(self.cfg.provider, model_name, len(messages),
                     len(tools) if tools else 0, latency, content, n_tc, error=err)
        if err:
            log_result("model", f"{self.cfg.provider}/{model_name} FAIL after {latency:.2f}s", err, level="ERROR")
        else:
            log_result("model", f"{ self.cfg.provider}/{model_name} OK after {latency:.2f}s "
                                f"tool_calls={n_tc} reply_len={len(content)}")
        return out

    async def _openai(self, messages, tools):
        import openai
        client = openai.AsyncOpenAI(
            api_key=self.cfg.api_key or "sk-no-key",
            base_url=self.cfg.base_url,
        )
        kwargs = dict(
            model=self.cfg.model,
            messages=messages,
            temperature=self.cfg.temperature,
            max_tokens=self.cfg.max_tokens,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            resp = await client.chat.completions.create(**kwargs)
            msg = resp.choices[0].message
            return {
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
                    for tc in (msg.tool_calls or [])
                ],
                "thinking": "",
                "raw": msg,
            }
        except Exception as e:
            return {"content": f"[LLM error] {e}", "tool_calls": [], "thinking": "", "raw": None}

    async def _ollama(self, messages, tools):
        import ollama
        client = ollama.AsyncClient(host=self.cfg.ollama_host)
        try:
            resp = await client.chat(
                model=self.cfg.ollama_model,
                messages=_to_ollama_messages(messages),
                tools=tools or None,
                think=self.cfg.ollama_think,
                options={"temperature": self.cfg.temperature, "num_predict": self.cfg.max_tokens},
            )
            msg = resp.get("message", {}) if hasattr(resp, "get") else getattr(resp, "message", {})
            if isinstance(msg, dict):
                content = msg.get("content", "") or ""
                raw_tcs = msg.get("tool_calls") or []
            else:
                # ollama>=0.4 returns Message objects; .get exists but
                # tool_calls is None (not missing) when there are no calls
                try:
                    content = msg.get("content", "") or ""
                except Exception:
                    content = getattr(msg, "content", "") or ""
                try:
                    raw_tcs = msg.get("tool_calls") or []
                except Exception:
                    raw_tcs = getattr(msg, "tool_calls", None) or []
            try:
                thinking = msg.get("thinking", "") or "" if hasattr(msg, "get") else ""
            except Exception:
                thinking = ""
            if not thinking:
                try:
                    thinking = getattr(msg, "thinking", "") or ""
                except Exception:
                    thinking = ""
            parsed = []
            for i, tc in enumerate(raw_tcs or []):
                try:
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        args = fn.get("arguments", {})
                    else:
                        fn = getattr(tc, "function", None)
                        if isinstance(fn, dict):
                            name = fn.get("name", "")
                            args = fn.get("arguments", {})
                        else:
                            name = getattr(fn, "name", "") or ""
                            args = getattr(fn, "arguments", {}) or {}
                    if isinstance(args, str):
                        args_s = args
                    else:
                        args_s = json.dumps(args or {})
                    parsed.append({"id": f"call_{i}", "name": name, "arguments": args_s})
                except Exception as e:
                    log_event("LLM", f"tool_call parse failed idx={i}: {e}", level="WARNING")
            return {"content": content, "tool_calls": parsed, "thinking": thinking or "", "raw": msg}
        except Exception as e:
            return {"content": f"[Ollama error] {e}", "tool_calls": [], "thinking": "", "raw": None}


RADIO_MAX_MSGS = 100
RADIO_MAX_LEN = 500
PLAN_MAX_STEPS = 100


class DelegateRound:
    """Shared war-room for ONE agent.delegate fan-out: radio bus + joint plan.

    The round RULES: no worker exits permanently while the round is open.
    A worker that finishes its own task goes IDLE (lingering) instead of
    exiting; any radio directed at it (`to=all` or `to=<id>`) or any plan
    change wakes it back to work. Only the supervisor (leader) closing the
    round — after all workers idle + radio quiet, or on timeout — releases
    workers to return for good.

    All workers run as coroutines on the same event-loop thread, so plain
    lists are safe (no await happens between a read and its paired write).
    Workers drain new traffic before every LLM call, so they genuinely read
    each other and coordinate instead of working blind.
    """
    def __init__(self, rid: str, worker_ids: List[str]):
        self.rid = rid
        self.worker_ids = list(worker_ids)
        self.seq = 0
        self.messages: List[dict] = []  # {seq, from, to, text, ts}
        self.plan: List[dict] = []      # {id, title, status, owner, note}
        self.plan_version = 0
        self.done = False
        self.states: Dict[str, str] = {wid: "working" for wid in worker_ids}
        self.last_activity = time.time()
        self.nested_active = 0  # sub-rounds in flight under this round's workers
        self.children: Dict[str, "DelegateRound"] = {}  # sub_rid -> sub-round (nest tree)

    def note_activity(self):
        """Any radio/plan traffic resets the quiet clock. Never raises."""
        try:
            self.last_activity = time.time()
        except Exception:
            pass

    def set_state(self, wid: str, state: str):
        """working | idle | done. Never raises."""
        try:
            if wid in self.states:
                self.states[wid] = state
        except Exception:
            pass

    def close(self, reason: str = ""):
        """End the round: lingering workers are released to return. Idempotent."""
        if self.done:
            return
        self.done = True
        try:
            for wid in self.states:
                if self.states[wid] != "done":
                    self.states[wid] = "done"
        except Exception:
            pass
        log_event("ROUND", f"round={self.rid} CLOSED ({reason or 'no reason'})")

    def snapshot_plan(self) -> List[dict]:
        return [dict(s) for s in self.plan]

    def say(self, frm: str, to: str, text: str) -> dict:
        text = _short((text or "").strip(), RADIO_MAX_LEN)
        if not text:
            return {"success": False, "error": "empty message — say something or skip radio"}
        if self.done:
            return {"success": False, "error": f"round {self.rid} is closed — message dropped"}
        if len(self.messages) >= RADIO_MAX_MSGS:
            return {"success": False, "error": "radio full (100 msgs); stop chatting and finish the task"}
        to = " ".join((to or "all").strip().split()) or "all"
        self.seq += 1
        msg = {"seq": self.seq, "from": frm, "to": to, "text": text,
               "ts": datetime.now().strftime("%H:%M:%S")}
        self.messages.append(msg)
        self.note_activity()
        log_event("RADIO", f"round={self.rid} #{msg['seq']} {frm}->{to}: {_short(text, 200)!r}")
        return {"success": True, "seq": self.seq}

    def plan_op(self, frm: str, action: str, step_id: str = "",
                  title: str = "", note: str = "") -> dict:
        action = (action or "list").strip().lower()
        if action == "list":
            return {"success": True, "plan": self.snapshot_plan(), "version": self.plan_version}
        if self.done:
            return {"success": False, "error": f"round {self.rid} is closed",
                    "plan": self.snapshot_plan()}
        if action == "add":
            if len(self.plan) >= PLAN_MAX_STEPS:
                return {"success": False, "error": "plan full (100 steps max)"}
            sid = (step_id or f"s{len(self.plan) + 1}").strip() or f"s{len(self.plan) + 1}"
            if any(s["id"] == sid for s in self.plan):
                sid = f"s{len(self.plan) + 1}_{frm}"
            self.plan.append({"id": sid, "title": _short((title or "").strip(), 200) or "untitled step",
                              "status": "open", "owner": "", "note": ""})
            self.plan_version += 1
            self.note_activity()
            log_event("PLAN", f"round={self.rid} {frm} add {sid}")
            return {"success": True, "step": sid, "plan": self.snapshot_plan(),
                    "version": self.plan_version}
        step = next((s for s in self.plan if s["id"] == (step_id or "").strip()), None)
        if step is None:
            return {"success": False, "error": f"unknown step {step_id!r}",
                    "plan": self.snapshot_plan()}
        if action == "claim":
            step["status"] = "doing"
            step["owner"] = frm
        elif action == "done":
            step["status"] = "done"
            step["owner"] = step["owner"] or frm
            if note:
                step["note"] = _short(note, 200)
        elif action == "note":
            step["note"] = _short(note or "", 200)
        else:
            return {"success": False, "error": f"unknown action {action!r} (add/claim/done/note/list)"}
        self.plan_version += 1
        self.note_activity()
        log_event("PLAN", f"round={self.rid} {frm} {action} {step['id']}")
        return {"success": True, "step": step["id"], "plan": self.snapshot_plan(),
                "version": self.plan_version}

    def drain(self, last_seq: int, last_version: int):
        """Messages since last_seq (max 8, chronological) + plan if changed."""
        msgs = [m for m in self.messages if m["seq"] > last_seq][-8:]
        plan = self.snapshot_plan() if self.plan_version != last_version else None
        return msgs, plan, self.seq, self.plan_version

    def format_traffic(self, msgs: List[dict], plan: Optional[List[dict]]) -> str:
        lines = []
        for m in msgs:
            lines.append(f"[{m['ts']}] {m['from']}→{m['to']}: {m['text']}")
        if plan is not None:
            icon = {"open": "⬜", "doing": "🔄", "done": "✅"}
            if plan:
                lines.append("PLAN " + " | ".join(
                    f"{icon.get(s['status'], '?')} {s['id']} {s['title']}"
                    + (f" ({s['owner']})" if s["owner"] else "")
                    for s in plan))
            else:
                lines.append("PLAN (empty)")
        return "\n".join(lines)


def _validate_delegate_tasks(tasks: list) -> Optional[str]:
    """Shared hard validation for agent.delegate (leader AND nested worker
    paths): every task needs a NAME (max two words, hologram/radio identity),
    a COLOR (#rrggbb), and a non-empty self-contained brief. Mutates tasks
    in place (sets id/color). Returns an error string, or None when valid."""
    import re as _re
    _name_ok = _re.compile(r"[A-Za-z0-9_][A-Za-z0-9_ \-]{0,30}[A-Za-z0-9_]?$")
    _hex_ok = _re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})$")
    bad_name, bad_color, seen = [], [], set()
    for i, t in enumerate(tasks):
        raw_name = " ".join(str(t.get("name", "") or "").split())
        words = raw_name.split(" ") if raw_name else []
        good_words = (1 <= len(words) <= 2 and all(1 <= len(w) <= 16 for w in words)
                      and _name_ok.match(raw_name) is not None)
        key = raw_name.lower()
        if not good_words:
            bad_name.append(f"task#{i + 1}")
        elif key in seen:
            bad_name.append(f"task#{i + 1} (duplicate name {raw_name!r})")
        else:
            seen.add(key)
            t["id"] = raw_name  # the hologram/radio identity IS the name
        raw_color = str(t.get("color", "") or "").strip()
        m = _hex_ok.match(raw_color)
        if not m:
            bad_color.append(t.get("id", f"task#{i + 1}"))
        else:
            h = m.group(1)
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            t["color"] = "#" + h.lower()
    if bad_name:
        return (f"tasks {bad_name} need a 'name' (REQUIRED, max TWO words, "
                f"letters/digits/_/-/space, e.g. 'Data Miner'). Re-call with names.")
    if bad_color:
        return (f"tasks {bad_color} need a 'color' (REQUIRED hex like '#b48cff' "
                f"expressing what the worker does — worn instead of gold). Re-call with colors.")
    bad = [t.get("id", f"t{i + 1}") for i, t in enumerate(tasks)
           if not (t.get("brief") or "").strip()]
    if bad:
        return (f"tasks {bad} have empty 'brief'. Re-call with a non-empty, "
                f"self-contained 'brief' per task (exact goal + which tool to use).")
    return None


# ============================================================
#  AGENT CORE — TRINITY LOOP
# ============================================================
class NexusAgent:
    """
    SINGLE leader agent per session. Owns the conversation history and is the
    ONLY one that produces the final answer (exactly one per user turn).

    Multi-agent behavior is on demand, not always: trivial tasks are answered
    directly; decomposable tasks are fanned out to isolated NexusWorker
    subagents via the agent.delegate tool, whose outputs the leader
    synthesizes into its single final answer.
    """
    def __init__(self, session_id: str, sm: SessionManager, llm: LLMProvider):
        self.sid = session_id
        self.sm = sm
        self.llm = llm
        self.env = detect_environment()
        self.workspace = str(sm.workspace(session_id))
        self.system_prompt = self._build_system_prompt()
        # Seed once: appending system every turn duplicated it and confused
        # the model with N system prompts after N turns.
        self.history: List[dict] = [{"role": "system", "content": self.system_prompt}]
        self.round_n = 0  # delegation round counter (R1, R2, … per session)
        self.turn_n = 0  # leader turns this session (run_id seed for evolution runs)
        self._evo_rec = None  # EvolutionRecorder (lazy, never breaks the loop)
        self.pending_files: List[dict] = []  # uploads waiting for next turn [{name,size}]
        self.on_event: Optional[Callable] = None  # callback for TUI (legacy single)
        self.listeners: List[Callable] = []  # extra fan-out targets (e.g. web UI bus)
        self.stop_event: Optional[Any] = None  # set by web STOP button; checked cooperatively
        self.on_round_done: Optional[Callable] = None  # web: async cb(summary) on bg round close
        self._bg_rounds: Dict[str, dict] = {}  # rid -> {rnd, tasks, ids, t0, task}
        self.pending_round_notes: List[str] = []  # finished-round summaries awaiting synthesis
        self.recursive_mode: bool = False  # NEST MODE: workers may spawn sub-workers (toggle)
        self._used_worker_names: set = set()  # every worker name this session (hologram keys)
        self._rounds: Dict[str, DelegateRound] = {}  # rid -> round (radio readable/writable)
        try:
            import evolution
            _rec = evolution.EvolutionRecorder()
            self._evo_rec = _rec
            _me = self
            self.add_listener(lambda e, d: _rec.listener(_me.sid, e, d))
        except Exception:
            pass

    def _build_system_prompt(self, evo_base=None) -> str:
        base = CFG.system_prompt_path.read_text(encoding="utf-8") if CFG.system_prompt_path.exists() else "You are NEXUS."
        env = self.env
        shells = ", ".join(s["name"] for s in env["shells"])
        ctx = f"""

# ── RUNTIME CONTEXT ──
Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} (current local date and time — use it for any time question)
OS: {env['os']} {env['release']} ({env['arch']})
Python: {env['python']}
Available shells: {shells}
Workspace: {self.workspace}
Session ID: {self.sid}

# ── LONG-TERM MEMORY (user profile + durable facts) ──
{MEM.context_block()}
"""
        try:
            # Self-evolution layer: base prompt + learned rules (versioned).
            # Falls back to base text silently if the store is absent/broken.
            import evolution
            base = evolution.active_prompt_text(base, evo_base or evolution.EVODIR)
        except Exception as e:
            log_event("AGENT", f"evolution layer skipped: {e}", level="DEBUG")
        try:
            rec_block = recursive_guidance(bool(getattr(self, "recursive_mode", False)))
        except Exception:
            rec_block = ""
        try:
            self_block = selfedit_guidance()
        except Exception:
            self_block = ""
        return base + ("\n\n" + rec_block if rec_block else "") + \
            ("\n\n" + self_block if self_block else "") + ctx

    def _refresh_system(self, extra_context: str = "", evo_base=None):
        """Rebuild history[0] so a long-lived agent picks up evolved rules,
        fresh date, and per-turn retrieved memories. Never raises."""
        try:
            self.system_prompt = self._build_system_prompt(evo_base)
            sys_text = self.system_prompt + (("\n\n" + extra_context) if extra_context else "")
            if self.history and self.history[0].get("role") == "system":
                self.history[0] = {"role": "system", "content": sys_text}
            else:
                self.history.insert(0, {"role": "system", "content": sys_text})
        except Exception as e:
            log_event("AGENT", f"system refresh failed: {e}", level="DEBUG")

    def add_listener(self, fn: Callable):
        """Attach another event consumer (web UI bus). Never raises."""
        try:
            if fn not in self.listeners:
                self.listeners.append(fn)
        except Exception:
            pass

    def _stopped(self) -> bool:
        """Cooperative STOP flag (web STOP button). Never raises."""
        try:
            ev = getattr(self, "stop_event", None)
            return bool(ev is not None and ev.is_set())
        except Exception:
            return False

    def _emit(self, event: str, data: dict):
        for target in ([self.on_event] if self.on_event else []) + list(self.listeners):
            try:
                target(event, data)
            except Exception as e:
                # Never silent: a failed emit means a UI goes blind
                # (this is how iteration/final updates were once lost).
                log_event("AGENT", f"emit {event} failed: {e}", level="DEBUG")

    def restore_history(self, max_msgs: int = 40) -> int:
        """Rebuild in-memory history from the stored session (used when
        switching to an old conversation). Returns messages restored."""
        self.history = [{"role": "system", "content": self.system_prompt}]
        try:
            meta = self.sm.load(self.sid)
        except Exception as e:
            log_event("AGENT", f"restore sid={self.sid} failed: {e}", level="WARNING")
            return 0
        n = 0
        for m in meta.get("messages", [])[-max_msgs:]:
            if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip():
                self.history.append({"role": m["role"], "content": m["content"]})
                n += 1
        log_event("AGENT", f"restored {n} messages sid={self.sid}")
        return n

    async def run(self, user_input: str) -> str:
        """Main leader loop. Returns the ONE final response for this turn."""
        log_event("AGENT", f"run start sid={self.sid} input={_short(user_input, 500)!r}")
        self.turn_n = getattr(self, "turn_n", 0) + 1
        try:
            import evolution
            evo_base = getattr(self, "evo_base", None) or evolution.EVODIR
            mem_block = evolution.retrieve(user_input, evo_base)
            if mem_block:
                log_event("AGENT", f"injected {len(mem_block)} chars of repo memory sid={self.sid}",
                          level="DEBUG")
            self._refresh_system(mem_block, evo_base)
            if self._evo_rec:
                self._evo_rec.run_start(self.sid, self.turn_n, user_input, self.workspace)
        except Exception as e:
            log_event("AGENT", f"turn preamble failed: {e}", level="DEBUG")
        notes = getattr(self, "pending_round_notes", None) or []
        if notes:
            self.pending_round_notes = []
            # Log-found fix: this used to ride as a mid-thread `system` message
            # with soft wording — the model twice replied "still waiting" WITH
            # the results already in context, then told the user agents were
            # "still running" after they had finished. User role + hard order.
            note = ("[BACKGROUND ROUNDS finished — the results are pasted BELOW. "
                    "You MUST synthesize and report them to the user now, then handle "
                    "the new request. Do NOT say you are waiting for results — they "
                    "are already here. Never claim background work is still running "
                    "when its round note is in this history:]\n"
                    + "\n".join(notes))
            self.history.append({"role": "user", "content": note})
            self.sm.add_message(self.sid, "user", note)
            log_event("AGENT", f"injected {len(notes)} background round note(s) sid={self.sid}")
        self.history.append({"role": "user", "content": user_input})
        self.sm.add_message(self.sid, "user", user_input)
        pending = getattr(self, "pending_files", None) or []
        if pending:
            # Files uploaded (button / drag-and-drop) since last turn.
            self.pending_files = []
            names = ", ".join(f"{p.get('name', '?')} ({p.get('size', 0)} bytes)" for p in pending)
            note = ("[Files uploaded to the session workspace since your last turn: " + names +
                    ". They are inside the session workspace directory.]")
            self.history.append({"role": "user", "content": note})
            self.sm.add_message(self.sid, "user", note)
            log_event("AGENT", f"injected {len(pending)} uploaded file(s) sid={self.sid}")
        self._delegate_rounds = 0  # per-turn delegation budget

        final = ""
        for iteration in range(CFG.max_iterations):
            if self._stopped():
                final = "⏹ Stopped by user — partial work above is kept."
                self.history.append({"role": "assistant", "content": final})
                self.sm.add_message(self.sid, "assistant", final)
                self._emit("final", {"content": final, "workspace": self.workspace,
                                     "stopped": True})
                log_event("AGENT", f"stopped by user sid={self.sid} iter={iteration + 1}")
                break
            self._emit("iteration", {"n": iteration + 1, "max": CFG.max_iterations})
            log_event("AGENT", f"iteration {iteration + 1}/{CFG.max_iterations} sid={self.sid}")
            llm_out = await self.llm.chat(self.history, tools=LEADER_TOOLS)
            if self._stopped():
                final = "⏹ Stopped by user — partial work above is kept."
                self.history.append({"role": "assistant", "content": final})
                self.sm.add_message(self.sid, "assistant", final)
                self._emit("final", {"content": final, "workspace": self.workspace,
                                     "stopped": True})
                log_event("AGENT", f"stopped by user sid={self.sid} iter={iteration + 1}")
                break
            if isinstance(llm_out, str):
                log_event("AGENT", f"sid={self.sid} LLM returned bare str — wrapped",
                          level="WARNING")
                llm_out = {"content": llm_out, "tool_calls": []}
            content = llm_out.get("content", "") or ""
            tool_calls = llm_out.get("tool_calls", []) or []
            if any(not isinstance(c, dict) for c in tool_calls):
                log_event("AGENT", f"sid={self.sid} dropped non-dict tool call(s)",
                          level="WARNING")
                tool_calls = [c for c in tool_calls if isinstance(c, dict)]
            if not tool_calls:
                content, tool_calls = _adopt_text_calls(
                    content, LEADER_TOOLS, "leader", self.sid)
            if llm_out.get("thinking"):
                self._emit("thinking", {"who": "leader", "text": llm_out["thinking"]})

            # If no tool calls, this is the single final answer for the turn
            if not tool_calls:
                if _is_error_content(content):
                    # Model endpoint still failing after retries: NEVER show the
                    # raw "[Ollama error] ... (ref:...)" text as the answer.
                    log_event("AGENT", f"final UNAVAILABLE sid={self.sid} iter={iteration + 1}",
                              level="WARNING")
                    final = MODEL_BILLING if _is_billing_error(content) else MODEL_UNAVAILABLE
                else:
                    final = content
                    log_event("AGENT", f"final sid={self.sid} iter={iteration + 1} len={len(content)}")
                    log_result("agent", f"final answer sid={self.sid} iter={iteration + 1} len={len(content)}",
                               _short(content, 2000))
                self.history.append({"role": "assistant", "content": final})
                self.sm.add_message(self.sid, "assistant", final)
                self._emit("final", {"content": final, "workspace": self.workspace})
                break

            # Record assistant message with tool calls
            self.history.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
            self.sm.add_message(self.sid, "assistant", content, tool_calls)
            log_event("AGENT", f"dispatch {len(tool_calls)} tool(s) sid={self.sid} iter={iteration + 1}",
                      extra={"tools": [tc.get("name") for tc in tool_calls]})

            # Execute tool calls — if multiple, run in parallel
            self._emit("tools_start", {"count": len(tool_calls)})

            async def _run_one(tc):
                params, res, el = await self._exec_leader_call(tc)
                self.sm.add_tool_call(self.sid, tc["name"], params, res, el)
                self._emit("tool_result", {"call_id": tc.get("id"), "who": "leader",
                                           "name": tc["name"], "params": params,
                                           "result": res, "elapsed": el})
                return tc, res

            if len(tool_calls) == 1:
                tc, res = await _run_one(tool_calls[0])
                gathered = [(tc, res)]
            else:
                gathered = await asyncio.gather(*[_run_one(tc) for tc in tool_calls])
            for tc, res in gathered:
                self.history.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(res, ensure_ascii=False)[:50000],
                })

            self._emit("tools_done", {"iteration": iteration + 1})

        if not final:
            log_event("AGENT", f"run exhausted max_iterations={CFG.max_iterations} sid={self.sid}", level="WARNING")
            log_result("agent", f"exhausted max_iterations sid={self.sid}", None, level="WARNING")
        try:
            import evolution
            evo_base = getattr(self, "evo_base", None) or evolution.EVODIR
            evolution.note_turn_completed(evo_base)
            self._maybe_auto_evolve()
        except Exception as e:
            log_event("AGENT", f"turn epilogue failed: {e}", level="DEBUG")
        return final

    def _maybe_auto_evolve(self):
        """Phase 5/10: background evolution cycle when due — never blocks the turn."""
        try:
            import evolution
            if getattr(self, "_evolving", False):
                return
            evo_base = getattr(self, "evo_base", None) or evolution.EVODIR
            last = evolution.last_run_summary(self.sid, evo_base)
            ok, reason = evolution.should_auto_evolve(last, evo_base)
            if not ok:
                return
            self._evolving = True
            log_event("EVOLVE", f"auto trigger ({reason}) sid={self.sid}")
            _me = self

            async def _bg():
                try:
                    base_text = CFG.system_prompt_path.read_text(encoding="utf-8") \
                        if CFG.system_prompt_path.exists() else "You are NEXUS."
                    await evolution.evolve_once(
                        _me.sm.root, Path(__file__).parent, base_text, _me.llm,
                        base=evo_base, task_ref=f"auto:{_me.sid[:8]}t{_me.turn_n}",
                        log_fn=lambda m: log_event("EVOLVE", str(m)[:200]))
                except Exception as e:
                    log_event("EVOLVE", f"auto cycle failed: {e}", level="ERROR")
                finally:
                    _me._evolving = False

            asyncio.create_task(_bg())
        except Exception:
            pass

    async def _exec_leader_call(self, tc: dict):
        """Execute one leader tool call. agent.delegate fans out to workers;
        everything else runs locally. Returns (params, result, elapsed)."""
        params = json.loads(tc["arguments"]) if isinstance(tc["arguments"], str) else (tc["arguments"] or {})
        self._emit("tool_start", {"call_id": tc.get("id"), "name": tc.get("name"),
                                  "params": params, "who": "leader"})
        t0 = time.time()
        if tc.get("name") == "agent.delegate":
            result = await self._delegate(params)
        elif tc.get("name") in ("radio.list", "radio.read", "radio.send"):
            result = self._radio_op(tc.get("name", ""), params)
        else:
            result = await execute_tool(tc.get("name", ""), params, self.workspace,
                                allow_code_edit=True, actor="leader")
        return params, result, round(time.time() - t0, 3)

    async def _delegate(self, params: dict) -> dict:
        """Fan out to isolated worker subagents running CONCURRENTLY.

        Workers get only their brief (+ optional context) — never the leader
        history. Their outputs return here as ONE tool result for the leader
        to synthesize. Workers cannot delegate (depth guard).
        """
        tasks = params.get("tasks", []) or []
        if not isinstance(tasks, list) or not tasks:
            return {"success": False, "error": "agent.delegate needs a non-empty 'tasks' list"}
        if len(tasks) > CFG.max_subagents:
            log_event("AGENT", f"delegate truncated {len(tasks)} -> {CFG.max_subagents} tasks sid={self.sid}",
                      level="WARNING")
            tasks = tasks[:CFG.max_subagents]
        for i, t in enumerate(tasks):
            if not isinstance(t, dict):
                tasks[i] = {"brief": str(t)}
        # Hard validation shared with nested workers (see
        # _validate_delegate_tasks): NAME + COLOR + non-empty brief.
        err = _validate_delegate_tasks(tasks)
        if err:
            return {"success": False, "error": err}
        # Loop guard: a leader that re-delegates instead of synthesizing is
        # capped — afterwards it must answer from what it already has.
        self._delegate_rounds = getattr(self, "_delegate_rounds", 0) + 1
        if self._delegate_rounds > CFG.delegate_max_rounds:
            return {"success": False,
                    "error": f"delegation budget exhausted ({CFG.delegate_max_rounds} rounds/turn). "
                             f"STOP delegating and synthesize your final answer from results so far."}

        ids = [t.get("id") for t in tasks]
        self.round_n += 1
        rid = f"R{self.round_n}"
        rnd = DelegateRound(rid, ids)
        rnd.note_activity()  # birth counts as activity (no instant close)
        self._remember_round(rid, rnd)
        self._emit("delegate_start", {"round": rid, "count": len(tasks), "ids": ids,
                                        "colors": {t.get("id"): t.get("color", "#D4AF37")
                                                   for t in tasks}})
        log_event("AGENT", f"delegate start sid={self.sid} round={rid} workers={ids}")
        t0 = time.time()
        tree = TreeBudget(resolve_tree_budget(params), names=self._used_worker_names)
        dupes = tree.claim_names(ids)
        if dupes:
            return {"success": False,
                    "error": f"names {dupes} are already used by other workers this "
                             f"session (one hologram body per name — reusing a name "
                             f"would overwrite it). Re-call with unique names "
                             f"(e.g. prefix with the round: 'R{self.round_n}-Miner')."}
        if getattr(self, "recursive_mode", False):
            log_event("AGENT", f"delegate tree budget sid={self.sid} round={rid} "
                                f"nested_cap={tree.limit}")
        if self.on_round_done is None:
            # TUI/CLI: no completion hook -> classic BLOCKING supervised round.
            futs = [asyncio.ensure_future(self._run_worker(t, rnd, tree)) for t in tasks]
            results = await self._supervise_round(rnd, futs, ids, t0)
            return self._finish_round(rnd, tasks, ids, results, t0)
        # WEB: BACKGROUND round — the leader (and the user) stays free for
        # anything else. Results come back through on_round_done and are
        # injected into the leader on a later turn (or auto-reported).
        record = {"rid": rid, "rnd": rnd, "tasks": tasks, "ids": ids, "t0": t0,
                  "done": False, "task": None, "tree": tree}
        self._bg_rounds[rid] = record
        record["task"] = asyncio.ensure_future(self._run_round_background(record))
        return {"success": True, "background": True, "round": rid, "workers": ids,
                "note": ("Round started in the BACKGROUND — you stay free for anything "
                         "else the user asks. Do NOT wait: end your turn now (tell the user "
                         "work is running in background). Report ONLY this launch "
                         "(round id + worker count + background). NEVER describe children, "
                         "sub-agents, or results that do not exist yet — they arrive as a "
                         "round update which you will synthesize and report to the user then.")}

    async def _supervise_round(self, rnd: DelegateRound, futs: list,
                               ids: list, t0: float) -> list:
        """SUPERVISED round (the round rules): workers linger instead of
        exiting, so a bare gather would hang. Tick instead: close when all
        workers idle/done AND the radio has been quiet, or on timeout.
        Quiet + no traffic ever -> instant close (independent tasks).
        Returns the per-worker result dicts in task order."""
        pending = set(futs)
        grace_until = 0.0
        # Log-found fix: a fixed 600s timeout massacres big rounds (20 workers
        # x ~30s/iter x 12 iters / 6 LLM slots ~= 1200s). Scale with size.
        timeout = max(CFG.round_timeout_sec, 60.0 * max(1, len(ids)))
        log_event("AGENT", f"delegate supervise sid={self.sid} round={rnd.rid} "
                            f"workers={len(ids)} timeout={timeout:.0f}s")
        try:
            while pending:
                if self._stopped():
                    rnd.close("stopped by user")
                _done, pending = await asyncio.wait(pending, timeout=1.0)
                if not pending:
                    break
                if rnd.done:
                    if grace_until and time.time() > grace_until:
                        for f in list(pending):
                            f.cancel()
                    continue
                try:
                    states = [rnd.states.get(wid, "done") for wid in ids]
                    quiet_for = time.time() - rnd.last_activity
                    need = 0.0 if (rnd.seq == 0 and rnd.plan_version == 0) else CFG.round_quiet_sec
                    if all(s in ("idle", "done") for s in states) and quiet_for >= need:
                        rnd.close(f"all idle + quiet {quiet_for:.0f}s")
                        continue
                    if time.time() - t0 > timeout and getattr(rnd, "nested_active", 0) <= 0:
                        rnd.close(f"round timeout {timeout:.0f}s")
                        grace_until = time.time() + 60
                except Exception as e:
                    log_event("AGENT", f"delegate supervisor tick failed: {e}",
                              level="WARNING")
        finally:
            rnd.close("delegate settled")
            for f in pending:
                f.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        results = []
        for wid, f in zip(ids, futs):
            try:
                results.append(f.result())
            except asyncio.CancelledError:
                results.append({"id": wid, "brief": "", "output": "",
                                "error": "cancelled", "success": False,
                                "tools_used": 0, "radio_seen": 0,
                                "radio_sent": 0, "elapsed": 0})
            except Exception as e:
                results.append({"id": wid, "brief": "", "output": "",
                                "error": f"worker crashed: {e}", "success": False,
                                "tools_used": 0, "radio_seen": 0,
                                "radio_sent": 0, "elapsed": 0})
        return results

    def _finish_round(self, rnd: DelegateRound, tasks: list, ids: list,
                      results: list, t0: float) -> dict:
        """Persist + announce a closed round. Shared by sync and background paths."""
        elapsed = round(time.time() - t0, 3)
        n_ok = sum(1 for r in results if r.get("success"))
        out = {"success": True, "round": rnd.rid, "results": results,
               "plan": rnd.snapshot_plan(),
               "elapsed": elapsed, "ok": n_ok, "failed": len(results) - n_ok}
        # Persist ONE entry for the whole fan-out (worker traces stay in the log).
        try:
            self.sm.add_tool_call(self.sid, "agent.delegate",
                                  {"tasks": [{k: t.get(k) for k in ("id", "brief")} for t in tasks]},
                                  out, elapsed)
        except Exception as e:
            log_event("SAVE", f"delegate persist failed sid={self.sid}: {e}", level="ERROR")
        log_event("AGENT", f"delegate done sid={self.sid} round={rnd.rid} ok={n_ok}/{len(results)} "
                            f"elapsed={elapsed:.1f}s radio={rnd.seq} plan_v{rnd.plan_version}")
        log_result("agent", f"delegate done sid={self.sid} round={rnd.rid} ok={n_ok}/{len(results)}", out)
        self._emit("delegate_done", {"round": rnd.rid, "ok": n_ok, "failed": len(results) - n_ok,
                                     "elapsed": elapsed, "plan": rnd.snapshot_plan()})
        return out

    def _remember_round(self, rid: str, rnd) -> None:
        """Register a round for radio.list/read/send; prune oldest CLOSED
        beyond 30 (active rounds are never pruned)."""
        try:
            self._rounds[rid] = rnd
            if len(self._rounds) > 30:
                for old_rid, old_rnd in list(self._rounds.items()):
                    if len(self._rounds) <= 30:
                        break
                    if getattr(old_rnd, "done", True) and old_rid != rid:
                        del self._rounds[old_rid]
        except Exception:
            pass

    def _find_round(self, rid: str):
        """Top-level round or any nested descendant (walk children)."""
        if not rid:
            return None
        if rid in (self._rounds or {}):
            return self._rounds[rid]
        stack = list((self._rounds or {}).values())
        while stack:
            r = stack.pop()
            if getattr(r, "rid", None) == rid:
                return r
            try:
                stack.extend((getattr(r, "children", None) or {}).values())
            except Exception:
                pass
        return None

    def _radio_op(self, name: str, params: dict) -> dict:
        """LEADER-ONLY radio trio: list groups, read any group's traffic,
        speak into any group as 'leader' (wakes lingering workers)."""
        params = params or {}
        if name == "radio.list":
            active_only = params.get("active_only") is True
            rows = []

            def walk(r, depth, parent):
                try:
                    states = getattr(r, "states", {}) or {}
                    n_w = len(states)
                    n_idle = sum(1 for s in states.values() if s == "idle")
                    n_done = sum(1 for s in states.values() if s == "done")
                    rows.append({"round": r.rid, "depth": depth,
                                 "parent": parent,
                                 "workers": n_w, "working": n_w - n_idle - n_done,
                                 "idle": n_idle, "done_workers": n_done,
                                 "done": bool(getattr(r, "done", True)),
                                 "messages": len(getattr(r, "messages", []) or []),
                                 "plan_version": getattr(r, "plan_version", 0)})
                    for cid, c in (getattr(r, "children", None) or {}).items():
                        walk(c, depth + 1, r.rid)
                except Exception:
                    pass

            for r in (self._rounds or {}).values():
                walk(r, 0, None)
            if active_only:
                rows = [x for x in rows if not x["done"]]
            return {"success": True, "rounds": rows}
        if name == "radio.read":
            rid = str(params.get("round") or "")
            rnd = self._find_round(rid)
            if rnd is None:
                known = sorted((self._rounds or {}).keys())
                return {"success": False,
                        "error": f"unknown round {rid!r}. Known top-level: {known}. "
                                 f"Use radio.list first."}
            try:
                since = max(0, int(params.get("since_seq") or 0))
            except Exception:
                since = 0
            try:
                limit = max(1, min(50, int(params.get("limit") or 20)))
            except Exception:
                limit = 20
            msgs = [m for m in (getattr(rnd, "messages", []) or [])
                    if m.get("seq", 0) > since][-limit:]
            out = {"success": True, "round": rnd.rid,
                   "done": bool(getattr(rnd, "done", True)),
                   "seq": getattr(rnd, "seq", 0), "messages": msgs,
                   "states": dict(getattr(rnd, "states", {}) or {})}
            if params.get("include_plan", True) is not False:
                try:
                    out["plan"] = rnd.snapshot_plan()
                except Exception:
                    out["plan"] = []
            return out
        if name == "radio.send":
            rid = str(params.get("round") or "")
            rnd = self._find_round(rid)
            if rnd is None:
                return {"success": False,
                        "error": f"unknown round {rid!r}. Use radio.list first."}
            res = rnd.say("leader", params.get("to", "all"),
                          params.get("text", ""))
            if res.get("success"):
                res = dict(res, round=rnd.rid, **{"from": "leader"})
            return res
        return {"success": False, "error": f"unknown radio op {name!r}"}

    async def _run_round_background(self, record: dict):
        """Background completion: supervise, finalize, then hand the summary
        to on_round_done (web: notify + synthesize). Never raises."""
        rnd, tasks, ids, t0 = record["rnd"], record["tasks"], record["ids"], record["t0"]
        try:
            futs = [asyncio.ensure_future(self._run_worker(t, rnd, record.get("tree")))
                    for t in tasks]
            results = await self._supervise_round(rnd, futs, ids, t0)
            out = self._finish_round(rnd, tasks, ids, results, t0)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_event("AGENT", f"background round {record['rid']} crashed: {e}", level="ERROR")
            out = {"success": False, "round": record["rid"], "results": [],
                   "error": str(e), "ok": 0, "failed": len(ids)}
        finally:
            record["done"] = True
            self._bg_rounds.pop(record["rid"], None)
        if not out.get("success"):
            summary_note = (f"[ROUND {record['rid']} FAILED in background: {out.get('error', '?')}] "
                            f"Tell the user plainly.")
        else:
            lines = []
            for r in out.get("results", []):
                mark = "OK" if r.get("success") else "FAIL"
                lines.append(f"- {r.get('id')} [{mark}]: {_short(r.get('output') or r.get('error') or '(empty)', 800)}")
            summary_note = (f"[ROUND {record['rid']} finished in background: "
                            f"{out.get('ok', 0)} ok / {out.get('failed', 0)} failed "
                            f"in {out.get('elapsed', 0):.0f}s]\n" + "\n".join(lines) +
                            "\nReport these results to the user now.")
        cb = self.on_round_done
        if cb is not None:
            try:
                if asyncio.iscoroutinefunction(cb):
                    await cb({"sid": self.sid, "summary": summary_note,
                              "round": record["rid"], "ok": out.get("success", False)})
                else:
                    cb({"sid": self.sid, "summary": summary_note,
                        "round": record["rid"], "ok": out.get("success", False)})
            except Exception as e:
                log_event("AGENT", f"on_round_done failed: {e}", level="ERROR")


    async def _run_worker(self, task: dict, rnd: DelegateRound,
                          tree: Optional[TreeBudget] = None) -> dict:
        wid = task.get("id", "t?")
        brief = task.get("brief", "")
        color = task.get("color", "#D4AF37")
        nest_on = bool(getattr(self, "recursive_mode", False))
        self._emit("subagent_start", {"round": rnd.rid, "id": wid, "brief": brief,
                                      "color": color, "depth": 1, "parent": None})
        log_event("SUBAGENT", f"start sid={self.sid} round={rnd.rid} id={wid} brief={_short(brief, 300)!r}")
        t0 = time.time()
        try:
            worker = NexusWorker(wid, brief, task.get("context", "") or "",
                                 self.sid, self.sm, self.llm, self.workspace,
                                 rnd, self._emit, color,
                                 depth=1, tree=tree, nest_on=nest_on,
                                 allow_code_edit=task.get("code_edit") is True)
            worker.stop_event = getattr(self, "stop_event", None)
            res = await worker.run()
        except Exception as e:
            import traceback as _tb
            log_event("SUBAGENT", f"worker {wid} CRASHED {type(e).__name__}: {e}\n"
                                  f"{_tb.format_exc()[-2000:]}", level="ERROR")
            res = {"text": "", "tools_used": 0, "iterations": 0, "success": False,
                   "error": f"{type(e).__name__}: {e}", "radio_seen": 0, "radio_sent": 0}
        elapsed = round(time.time() - t0, 3)
        ok = bool(res.get("success", True)) and not res.get("error")
        log_event("SUBAGENT", f"done sid={self.sid} round={rnd.rid} id={wid} {'OK' if ok else 'FAIL'} "
                              f"elapsed={elapsed:.1f}s tools={res.get('tools_used', 0)} "
                              f"radio_seen={res.get('radio_seen', 0)} radio_sent={res.get('radio_sent', 0)}")
        log_result("subagent", f"worker {wid} {'OK' if ok else 'FAIL'} ({elapsed:.1f}s): "
                               f"{_short(res.get('text') or res.get('error') or '', 300)!r}")
        self._emit("subagent_done", {"round": rnd.rid, "id": wid, "brief": brief,
                                     "output": res.get("text", ""),
                                     "success": ok, "elapsed": elapsed,
                                     "tools_used": res.get("tools_used", 0),
                                     "radio_seen": res.get("radio_seen", 0),
                                     "radio_sent": res.get("radio_sent", 0)})
        return {"id": wid, "brief": brief, "output": res.get("text", ""),
                "error": res.get("error", ""), "success": ok,
                "tools_used": res.get("tools_used", 0),
                "radio_seen": res.get("radio_seen", 0),
                "radio_sent": res.get("radio_sent", 0), "elapsed": elapsed}


class NexusWorker:
    """Isolated worker subagent. Own private history (task brief only), own
    iteration budget, exec/web tools but NO delegation. Talks to peer workers
    over the round radio (agent.say) and coordinates via the joint plan
    (agent.plan) — new traffic is injected before every step. Its result text
    is an INPUT to the leader — it never writes a final answer and never
    touches the leader's history. Completion lines are [SUBAGENT]/[RADIO]/
    [PLAN], never [AGENT]."""

    def __init__(self, wid: str, brief: str, context: str, sid: str,
                 sm: SessionManager, llm: LLMProvider, workspace: str,
                 rnd: Optional[DelegateRound] = None,
                 emit: Optional[Callable] = None, color: str = "#D4AF37",
                 depth: int = 1, tree: Optional["TreeBudget"] = None,
                 nest_on: bool = False, allow_code_edit: bool = False):
        self.wid = wid
        self.color = color or "#D4AF37"
        self.brief = brief or ""
        self.sid = sid
        self.sm = sm
        self.llm = llm
        self.workspace = workspace
        self.rnd = rnd
        self.emit = emit or (lambda e, d: None)
        self.depth = max(1, int(depth or 1))
        self.tree = tree
        self.nest_on = bool(nest_on)
        self.allow_code_edit = bool(allow_code_edit)  # explicit per-task grant only
        self._nest_seq = 0  # nested-round counter (sub-round ids)
        self.tools_used = 0
        self.radio_seen = 0
        self.radio_sent = 0
        self.last_seq = 0
        self.last_version = -1  # -1 forces the opening plan snapshot on step 1
        self.stop_event = None  # set by web STOP button via the leader's agent
        peers = [w for w in (rnd.worker_ids if rnd else []) if w != wid]
        self.history: List[dict] = [
            {"role": "system", "content":
                "You are a NEXUS worker subagent. Complete ONLY the task below with tools. "
                "Be concise: your last message must be the self-contained result (facts, "
                "numbers, file paths — everything the leader needs, since it never sees "
                "your tool outputs directly). Do not ask questions. There is no delegation tool."
                + (" " + worker_nest_guidance(self.depth)
                   if self._nest_offered() else "")
                + (f" You share round {rnd.rid} with peer worker(s) {peers}: coordinate with "
                     "agent.say (to='all' or a peer id — they read it before their next step) "
                     "and the joint agent.plan (add/claim/done/note/list). Claim a plan step "
                     "before doing it so peers don't duplicate work, and READ incoming radio "
                     "and plan updates instead of redoing finished work. RADIO DISCIPLINE (mandatory): "
                     "1) first action: broadcast who you are and which step you claim; "
                     "2) if a peer addresses YOU by id on the radio, answer them directly (to=their id) "
                     "before you finish; 3) broadcast your key finding + plan done when finished. "
                     "A worker that never speaks is a failed worker. "
                     "The leader reads this radio (any round, any depth) and may "
                     "chime in as 'leader' — heed a leader message like a briefing update. "
                     "TOOL DISCIPLINE: match shell_type to your syntax (PowerShell code → pwsh; "
                     "cmd syntax → cmd.exe) — if stdout just echoes your command, switch shells "
                     "immediately. Prefer fast checks (ping -n 4, fetch timeout 20); never "
                     "tracert -h 30; never re-fetch a URL that returned junk. STOP calling "
                     "tools the moment your brief is fulfilled — report partial results "
                     "instead of burning iterations. Stopping does NOT end you: the round "
                     "rules, and while it is open radio addressed to you (or to all) or "
                     "plan changes will wake you back to work, so keep watching."
                   if rnd and peers else "")
                + (" " + worker_codeedit_guidance(self.allow_code_edit))},
            {"role": "user", "content":
                f"TASK {wid}: {brief}" + (f"\nCONTEXT: {context}" if context else "")},
        ]

    def _nest_offered(self) -> bool:
        """True when this worker is actually offered agent.delegate."""
        return bool(self.nest_on and self.depth < CFG.recursive_max_depth)

    def _nest_allowed(self) -> bool:
        """Gate for an incoming agent.delegate call: mode on, depth headroom,
        and tree budget left (claimed at spawn time inside _delegate_nested)."""
        if not self._nest_offered():
            return False
        return self.tree is None or self.tree.room() > 0

    def _tools(self) -> list:
        """Dynamic tool list: delegate appears only when nesting is offered."""
        if self._nest_offered():
            return WORKER_TOOLS + [TOOL_SPEC_DELEGATE_NEST]
        return WORKER_TOOLS

    async def _delegate_nested(self, params: dict) -> dict:
        """SYNC nested fan-out inside this worker (NEST MODE). Spawns
        depth+1 children in their own sub-round with private radio/plan,
        supervises to close, and returns their outputs as THIS tool call's
        result for the worker to verify + fold into its own result."""
        tasks = params.get("tasks", []) or []
        if not isinstance(tasks, list) or not tasks:
            return {"success": False,
                    "error": "agent.delegate needs a non-empty 'tasks' list"}
        err = _validate_delegate_tasks(tasks)
        if err:
            return {"success": False, "error": err}
        # Log-found fix (Gatherer1-6 in 4 sub-rounds overwrote one body):
        # names are session-unique — duplicates bounce with a fix hint.
        dupes = self.tree.claim_names([x["id"] for x in tasks]) if self.tree else []
        if dupes:
            return {"success": False,
                    "error": f"names {dupes} are already used by other workers this "
                             f"session (one hologram body per name). Re-call with unique "
                             f"names — e.g. prefix with your own id: '{self.wid}-G1'."}
        wanted = [x["id"] for x in tasks]
        cap = CFG.recursive_max_tasks
        trunc = ""
        if len(tasks) > cap:
            tasks = tasks[:cap]
            trunc = f" (truncated to the nested per-call cap of {cap})"
        granted = self.tree.claim(len(tasks)) if self.tree else len(tasks)
        if granted < len(tasks):
            tasks = tasks[:granted]
            trunc += (f" (tree budget: only {granted} spawned, "
                      f"{self.tree.used}/{self.tree.limit} nested workers used)")
        if not tasks:
            return {"success": False,
                    "error": "tree budget exhausted — no nested workers left; "
                             "do the task with exec/web tools"}
        # Chaining order: the worker MUST call again for the remainder unless
        # the tree is dry (then it reports partials + the cap, honestly).
        dropped = [i for i in wanted if i not in {x["id"] for x in tasks}]
        if dropped and self.tree is not None:
            # Free dropped names IN PLACE (shared session set) so the chained
            # retry for the remainder passes the uniqueness gate.
            drop_low = {str(i).lower() for i in dropped}
            for n in [n for n in self.tree.names if str(n).lower() in drop_low]:
                self.tree.names.discard(n)
        if dropped:
            if self.tree is not None and self.tree.room() <= 0:
                trunc += (f" REMAINDER {dropped} CANNOT spawn (tree budget dry) — "
                          f"report your partials and state the {self.tree.limit}-worker "
                          f"tree cap plainly.")
            else:
                trunc += (f" REMAINDER NOT SPAWNED: {dropped} — call agent.delegate "
                          f"AGAIN with exactly these remaining tasks (same brief rules).")
        self._nest_seq += 1
        base_rid = self.rnd.rid if self.rnd else "R?"
        sub_rid = f"{base_rid}/{self.wid}#{self._nest_seq}"
        sub = DelegateRound(sub_rid, [x["id"] for x in tasks])
        sub.note_activity()
        if self.rnd is not None:
            try:
                self.rnd.children[sub_rid] = sub
            except Exception:
                pass
        try:
            self.emit("delegate_start", {"round": sub_rid, "nested": True,
                                         "parent": self.wid, "depth": self.depth + 1,
                                         "count": len(tasks),
                                         "ids": [x["id"] for x in tasks],
                                         "colors": {x["id"]: x.get("color", "#D4AF37")
                                                    for x in tasks}})
        except Exception:
            pass
        log_event("AGENT", f"nested delegate start sid={self.sid} round={sub_rid} "
                            f"parent={self.wid} depth={self.depth + 1} workers={[x['id'] for x in tasks]}")
        t0 = time.time()
        if self.rnd is not None:
            self.rnd.nested_active = getattr(self.rnd, "nested_active", 0) + 1
        try:
            futs = [asyncio.ensure_future(self._run_child(x, sub)) for x in tasks]
            results = await self._supervise_nested(sub, futs, [x["id"] for x in tasks], t0)
        finally:
            if self.rnd is not None:
                self.rnd.nested_active = max(0, getattr(self.rnd, "nested_active", 1) - 1)
        elapsed = round(time.time() - t0, 3)
        n_ok = sum(1 for r in results if r.get("success"))
        slim = [{k: r.get(k) for k in ("id", "brief", "output", "error", "success",
                                       "tools_used", "elapsed")}
                for r in results]
        try:
            self.sm.add_tool_call(self.sid, "agent.delegate",
                                  {"nested": True, "parent": self.wid,
                                   "tasks": [{k: x.get(k) for k in ("id", "brief")} for x in tasks]},
                                  {"success": True, "round": sub_rid, "results": slim,
                                   "elapsed": elapsed, "ok": n_ok,
                                   "failed": len(results) - n_ok}, elapsed)
        except Exception as e:
            log_event("SAVE", f"nested delegate persist failed sid={self.sid}: {e}",
                      level="ERROR")
        log_event("AGENT", f"nested delegate done sid={self.sid} round={sub_rid} "
                            f"ok={n_ok}/{len(results)} elapsed={elapsed:.1f}s")
        try:
            self.emit("delegate_done", {"round": sub_rid, "nested": True,
                                        "parent": self.wid, "ok": n_ok,
                                        "failed": len(results) - n_ok,
                                        "elapsed": elapsed})
        except Exception:
            pass
        head = (f"Nested round {sub_rid} finished: {n_ok} ok / "
                f"{len(results) - n_ok} failed in {elapsed:.0f}s{trunc}. "
                f"VERIFY each child against its OUTPUT contract, then fold them "
                f"into YOUR result (I never see raw child output).")
        lines = [f"- {r.get('id')} [{'OK' if r.get('success') else 'FAIL'}]: "
                 f"{_short(r.get('output') or r.get('error') or '(empty)', 800)}"
                 for r in results]
        return {"success": True, "nested": True, "round": sub_rid,
                "ok": n_ok, "failed": len(results) - n_ok,
                "results": slim,
                "note": head + "\n" + "\n".join(lines)}

    async def _run_child(self, task: dict, sub: DelegateRound) -> dict:
        """Spawn + run one nested child (depth+1), emitting depth/parent so
        the hologram can nest its orbit. Mirrors NexusAgent._run_worker."""
        wid = task.get("id", "t?")
        brief = task.get("brief", "")
        color = task.get("color", "#D4AF37")
        try:
            self.emit("subagent_start", {"round": sub.rid, "id": wid, "brief": brief,
                                         "color": color, "depth": self.depth + 1,
                                         "parent": self.wid})
        except Exception:
            pass
        log_event("SUBAGENT", f"start sid={self.sid} round={sub.rid} id={wid} "
                               f"depth={self.depth + 1} parent={self.wid} "
                               f"brief={_short(brief, 300)!r}")
        t0 = time.time()
        try:
            worker = NexusWorker(wid, brief, task.get("context", "") or "",
                                 self.sid, self.sm, self.llm, self.workspace,
                                 sub, self.emit, color,
                                 depth=self.depth + 1, tree=self.tree,
                                 nest_on=self.nest_on,
                                 allow_code_edit=task.get("code_edit") is True)
            worker.stop_event = getattr(self, "stop_event", None)
            res = await worker.run()
        except Exception as e:
            import traceback as _tb
            log_event("SUBAGENT", f"worker {wid} CRASHED {type(e).__name__}: {e}\n"
                                  f"{_tb.format_exc()[-2000:]}", level="ERROR")
            res = {"text": "", "tools_used": 0, "iterations": 0, "success": False,
                   "error": f"{type(e).__name__}: {e}", "radio_seen": 0, "radio_sent": 0}
        elapsed = round(time.time() - t0, 3)
        ok = bool(res.get("success", True)) and not res.get("error")
        log_event("SUBAGENT", f"done sid={self.sid} round={sub.rid} id={wid} "
                               f"{'OK' if ok else 'FAIL'} elapsed={elapsed:.1f}s "
                               f"tools={res.get('tools_used', 0)}")
        try:
            self.emit("subagent_done", {"round": sub.rid, "id": wid, "brief": brief,
                                        "output": res.get("text", ""),
                                        "success": ok, "elapsed": elapsed,
                                        "tools_used": res.get("tools_used", 0),
                                        "radio_seen": res.get("radio_seen", 0),
                                        "radio_sent": res.get("radio_sent", 0),
                                        "depth": self.depth + 1, "parent": self.wid})
        except Exception:
            pass
        return {"id": wid, "brief": brief, "output": res.get("text", ""),
                "error": res.get("error", ""), "success": ok,
                "tools_used": res.get("tools_used", 0),
                "radio_seen": res.get("radio_seen", 0),
                "radio_sent": res.get("radio_sent", 0), "elapsed": elapsed}

    async def _supervise_nested(self, sub: DelegateRound, futs: list,
                                ids: list, t0: float) -> list:
        """Nested supervisor: same round rules as top-level (idle+quiet close,
        scaled timeout, grace+cancel), scoped to one sub-round. Never raises
        past CancelledError."""
        pending = set(futs)
        grace_until = 0.0
        timeout = scaled_round_timeout(len(ids))
        try:
            while pending:
                if self._stopped():
                    sub.close("stopped by user")
                _done, pending = await asyncio.wait(pending, timeout=1.0)
                if not pending:
                    break
                if sub.done:
                    if grace_until and time.time() > grace_until:
                        for f in list(pending):
                            f.cancel()
                    continue
                try:
                    states = [sub.states.get(wid, "done") for wid in ids]
                    quiet_for = time.time() - sub.last_activity
                    need = 0.0 if (sub.seq == 0 and sub.plan_version == 0) else CFG.round_quiet_sec
                    if all(s in ("idle", "done") for s in states) and quiet_for >= need:
                        sub.close(f"all idle + quiet {quiet_for:.0f}s")
                        continue
                    if time.time() - t0 > timeout and getattr(sub, "nested_active", 0) <= 0:
                        sub.close(f"nested round timeout {timeout:.0f}s")
                        grace_until = time.time() + 60
                except Exception as e:
                    log_event("AGENT", f"nested supervisor tick failed: {e}",
                              level="WARNING")
        finally:
            sub.close("nested delegate settled")
            for f in pending:
                f.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        results = []
        for wid, f in zip(ids, futs):
            try:
                results.append(f.result())
            except asyncio.CancelledError:
                results.append({"id": wid, "brief": "", "output": "",
                                "error": "cancelled", "success": False,
                                "tools_used": 0, "radio_seen": 0,
                                "radio_sent": 0, "elapsed": 0})
            except Exception as e:
                results.append({"id": wid, "brief": "", "output": "",
                                "error": f"worker crashed: {e}", "success": False,
                                "tools_used": 0, "radio_seen": 0,
                                "radio_sent": 0, "elapsed": 0})
        return results

    def _drain_radio(self):
        """Pull new round traffic into history before the next LLM call."""
        if not self.rnd:
            return
        msgs, plan, seq, ver = self.rnd.drain(self.last_seq, self.last_version)
        self.radio_seen += sum(1 for m in msgs if m["from"] != self.wid)
        self.last_seq, self.last_version = seq, ver
        if msgs or plan is not None:
            self.history.append({
                "role": "user",
                "content": f"📻 ROUND {self.rnd.rid} update:\n"
                           + self.rnd.format_traffic(msgs, plan),
            })

    def _stopped(self) -> bool:
        try:
            ev = getattr(self, "stop_event", None)
            return bool(ev is not None and ev.is_set())
        except Exception:
            return False

    async def _linger(self) -> bool:
        """Idle (not exit) while the round is open. Wakes on radio addressed
        to me (`to=<wid>`) or broadcast (`to=all`), or on any plan change.
        Returns True if woken with news, False if the round closed (exit for
        good) or the linger failsafe fired. Never raises."""
        rnd = self.rnd
        if rnd is None or rnd.done:
            return False
        rnd.set_state(self.wid, "idle")
        log_event("SUBAGENT", f"id={self.wid} sid={self.sid} idle — lingering for round traffic")
        try:
            self.emit("idle", {"round": rnd.rid, "id": self.wid})
        except Exception:
            pass
        start = time.time()
        try:
            while not rnd.done and not self._stopped():
                if time.time() - start > CFG.worker_linger_sec:
                    log_event("SUBAGENT", f"id={self.wid} linger failsafe "
                                          f"({CFG.worker_linger_sec:.0f}s) — exiting",
                              level="WARNING")
                    return False
                await asyncio.sleep(0.5)
                try:
                    msgs, plan, seq, ver = rnd.drain(self.last_seq, self.last_version)
                except Exception:
                    continue
                norm = lambda s: " ".join(str(s or "").split()).lower()
                news = [m for m in (msgs or [])
                        if norm(m.get("to")) in (norm(self.wid), "all")]
                if news or plan is not None:
                    self.last_seq, self.last_version = seq, ver
                    try:
                        self.radio_seen += sum(1 for m in news if m.get("from") != self.wid)
                    except Exception:
                        pass
                    self.history.append({
                        "role": "user",
                        "content": f"📻 ROUND {rnd.rid} update (the round is still open and this "
                                   f"needs you — address it with tools, then continue):\n"
                                   + rnd.format_traffic(news, plan),
                    })
                    try:
                        self.sm.add_message(self.sid, "user",
                                            f"[round {rnd.rid} radio -> {self.wid}: waking]")
                    except Exception:
                        pass
                    log_event("SUBAGENT", f"id={self.wid} WOKE on {len(news)} msg(s) "
                                          f"plan={'yes' if plan is not None else 'no'}")
                    try:
                        self.emit("wake", {"round": rnd.rid, "id": self.wid,
                                           "msgs": len(news)})
                    except Exception:
                        pass
                    return True
                self.last_seq, self.last_version = seq, ver
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log_event("SUBAGENT", f"id={self.wid} linger crashed: {e} — exiting",
                      level="ERROR")
            return False
        return False

    async def run(self) -> dict:
        iterations = 0
        text = ""
        updates = 0
        finished_early = False
        closed_by_round = False
        # Refuse empty briefs instantly: no LLM calls wasted on hallucinated meta-work.
        if not self.brief.strip():
            return {"text": "", "tools_used": 0, "iterations": 0, "success": False,
                    "error": "empty brief — leader must supply a self-contained task",
                    "radio_seen": 0}
        if self.rnd:
            self.rnd.set_state(self.wid, "working")
        try:
            for i in range(CFG.subagent_max_iterations):
                iterations = i + 1
                if self._stopped():
                    log_event("SUBAGENT", f"id={self.wid} stopped by user")
                    finished_early = True
                    break
                if self.rnd:
                    if self.rnd.done:
                        closed_by_round = True
                        break
                    self.rnd.set_state(self.wid, "working")
                self._drain_radio()
                out = await self.llm.chat(self.history, tools=self._tools())
                if isinstance(out, str):
                    log_event("SUBAGENT", f"id={self.wid} LLM returned bare str — wrapped",
                              level="WARNING")
                    out = {"content": out, "tool_calls": []}
                content = out.get("content", "") or ""
                calls = out.get("tool_calls", []) or []
                if any(not isinstance(c, dict) for c in calls):
                    log_event("SUBAGENT", f"id={self.wid} dropped non-dict tool call(s)",
                              level="WARNING")
                    calls = [c for c in calls if isinstance(c, dict)]
                if not calls:
                    content, calls = _adopt_text_calls(
                        content, self._tools(), "worker", self.sid)
                if out.get("thinking"):
                    self.emit("thinking", {"who": self.wid, "text": out["thinking"],
                                           "round": self.rnd.rid if self.rnd else ""})
                if not calls:
                    # Own task finished -> IDLE, never a permanent exit while
                    # the round is open. Later finishes APPEND (round updates)
                    # so the first real result is never overwritten by an ack.
                    if content.strip():
                        if text and content.strip() != text.strip():
                            updates += 1
                            text = text + f"\n\n[round update {updates}]: {content.strip()}"
                        elif not text:
                            text = content
                    self.history.append({"role": "assistant", "content": content})
                    if self.rnd is None or self.rnd.done:
                        finished_early = True
                        break
                    if await self._linger():
                        continue  # woken by radio/plan -> another step
                    finished_early = True
                    break  # round closed (or failsafe) -> exit for good
                self.history.append({"role": "assistant", "content": content, "tool_calls": calls})

                async def _one(tc):
                    params = (json.loads(tc["arguments"])
                              if isinstance(tc["arguments"], str) else (tc["arguments"] or {}))
                    name = tc.get("name", "")
                    rid = self.rnd.rid if self.rnd else ""
                    t0 = time.time()
                    self.emit("wtool_start", {"call_id": tc.get("id"), "round": rid,
                                              "who": self.wid, "name": name, "params": params})
                    if name == "agent.delegate":
                        if self._nest_allowed():
                            res = await self._delegate_nested(params)
                        else:
                            why = (f"at max nesting depth {CFG.recursive_max_depth}")
                            if not self.nest_on:
                                why = ("RECURSIVE MODE is OFF for this session — flip the "
                                       "NEST toggle (or /recursive on) to allow workers "
                                       "that spawn sub-workers")
                            res = {"success": False,
                                   "error": f"workers cannot delegate ({why}); "
                                            f"do the task with exec/web tools"}
                    elif name in ("radio.list", "radio.read", "radio.send"):
                        res = {"success": False,
                               "error": "leader-only radio tools — workers use agent.say / "
                                        "agent.plan inside their own round"}
                    elif name == "agent.say" and self.rnd:
                        res = self.rnd.say(self.wid, params.get("to", "all"), params.get("text", ""))
                        if res.get("success"):
                            self.radio_sent += 1
                            self.emit("radio", {"round": self.rnd.rid, "seq": res["seq"],
                                                "from": self.wid, "to": params.get("to", "all") or "all",
                                                "text": params.get("text", "")})
                    elif name == "agent.plan" and self.rnd:
                        res = self.rnd.plan_op(self.wid, params.get("action", "list"),
                                               params.get("step_id", ""), params.get("title", ""),
                                               params.get("note", ""))
                        if res.get("success") and (params.get("action", "list") != "list"):
                            self.emit("plan", {"round": self.rnd.rid, "by": self.wid,
                                               "action": params.get("action", ""),
                                               "step": res.get("step", params.get("step_id", "")),
                                               "plan": res.get("plan", []),
                                               "version": res.get("version", 0)})
                    elif name in ("agent.say", "agent.plan"):
                        res = {"success": False, "error": "no active round"}
                    else:
                        res = await execute_tool(name, params, self.workspace,
                                                 allow_code_edit=self.allow_code_edit,
                                                 actor=f"worker {self.wid}")
                    self.emit("wtool_result", {"call_id": tc.get("id"), "round": rid,
                                               "who": self.wid, "name": name, "params": params,
                                               "result": res, "elapsed": round(time.time() - t0, 3)})
                    return tc, params, res

                if len(calls) == 1:
                        gathered = [await _one(calls[0])]
                else:
                        gathered = await asyncio.gather(*[_one(tc) for tc in calls])
                for tc, params, res in gathered:
                    self.tools_used += 1
                    self.history.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(res, ensure_ascii=False)[:20000],
                    })
        finally:
            if self.rnd:
                self.rnd.set_state(self.wid, "done")
        round_cut = closed_by_round and not finished_early and not text
        if round_cut:
            # Log-found fix: the supervisor closed the round under this worker
            # (timeout/quiet) — it did NOT exhaust any budget. Report honestly
            # instead of the old "(budget exhausted...)" lie.
            log_event("SUBAGENT", f"id={self.wid} sid={self.sid} round closed under it "
                                  f"after {iterations} iterations with no output",
                      level="WARNING")
        elif not finished_early and not closed_by_round:
            log_event("SUBAGENT", f"id={self.wid} sid={self.sid} hit iteration budget "
                                  f"({CFG.subagent_max_iterations})", level="WARNING")
        if self._stopped() and not text:
            text = "(stopped by user before producing output)"
        if _is_error_content(text):
            # Model endpoint failed: report failure so the leader knows this
            # output is unusable (never pass raw error text up as a result).
            billing = _is_billing_error(text)
            return {"text": "", "tools_used": self.tools_used,
                    "iterations": iterations, "success": False,
                    "error": ("model billing refusal 402 (switch to a free model)"
                              if billing else
                              "model endpoint temporarily failing (retry delegation)"),
                    "radio_seen": self.radio_seen, "radio_sent": self.radio_sent}
        if not text:
            # Ended with no output: say WHY (round cut vs true exhaustion).
            if round_cut:
                text = (f"(round closed by supervisor after {iterations} iterations "
                        f"before producing output; partial trace in nexus.log)")
            else:
                text = f"(budget exhausted after {iterations} iterations, {self.tools_used} tool calls; " \
                       f"partial trace in nexus.log)"
            success = False
        else:
            # A round-cut worker that DID produce text keeps its result.
            success = True
        return {"text": text, "tools_used": self.tools_used,
                "iterations": iterations, "success": success,
                "radio_seen": self.radio_seen, "radio_sent": self.radio_sent}


# ============================================================
#  SLASH COMMANDS (/new /sessions /open /delete /models /model
#  /thinking /memory /help) — shared by TUI and web UI.
#  handle_slash() never calls the LLM; it returns
#  {"handled": bool, "reply": markdown, "action": None|"new"|"switch"|"clear",
#   "sid": str}. Callers apply "action" (create/attach agent, clear screen).
# ============================================================
SLASH_COMMANDS = [
    {"cmd": "/new", "usage": "/new", "desc": "Start a new session"},
    {"cmd": "/sessions", "usage": "/sessions", "desc": "List old conversations"},
    {"cmd": "/open", "usage": "/open <id|number>", "desc": "Switch to an old conversation"},
    {"cmd": "/delete", "usage": "/delete <id|number>", "desc": "Delete a conversation (not the active one)"},
    {"cmd": "/models", "usage": "/models [ollama|openai|all]", "desc": "List available models (live from providers)"},
    {"cmd": "/model", "usage": "/model <name>", "desc": "Switch model live (validated against live list)"},
    {"cmd": "/provider", "usage": "/provider [ollama|openai]", "desc": "Show/switch LLM provider live"},
    {"cmd": "/base-url", "usage": "/base-url <url>", "desc": "Show/set OpenAI-compatible base URL"},
    {"cmd": "/key", "usage": "/key <sk-...>", "desc": "Set OpenAI-compatible API key (masked, saved to .env)"},
    {"cmd": "/ollama-host", "usage": "/ollama-host <url>", "desc": "Show/set Ollama host URL"},
    {"cmd": "/thinking", "usage": "/thinking [low|medium|high]", "desc": "Show/set reasoning effort"},
    {"cmd": "/recursive", "usage": "/recursive [on|off]", "desc": "NEST MODE: let workers spawn sub-workers"},
    {"cmd": "/layout", "usage": "/layout [flow|lattice|toggle]", "desc": "Hologram layout: flow scatter or lattice sphere-grid"},
    {"cmd": "/memory", "usage": "/memory", "desc": "Show what NEXUS remembers about you"},
    {"cmd": "/evolve", "usage": "/evolve [history|rollback N|why vNNN|revalidate]", "desc": "Run self-evolution cycle / versions"},
    {"cmd": "/clear", "usage": "/clear", "desc": "Clear the screen (local)"},
    {"cmd": "/help", "usage": "/help", "desc": "This list"},
]

# Hologram layout law (frontend visual): "flow" (computed-map scatter around
# the caller) or "lattice" (imaginary sphere-grid around the caller).
# /layout flips it; web clients apply it via Entity3D.setLayoutMode.
LAYOUT_MODE = "flow"


def update_env_file(key: str, value: str):
    """Persist one KEY=value into .env (replace or append). Never raises."""
    try:
        p = Path(__file__).parent / ".env"
        lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
        out, done = [], False
        for ln in lines:
            s = ln.strip()
            # Match KEY= and #KEY= (commented example lines) so toggling works.
            target = s[1:].strip() if s.startswith("#") else s
            if target.startswith(key + "=") and not done:
                out.append(f"{key}={value}")
                done = True
            else:
                out.append(ln)
        if not done:
            out.append(f"{key}={value}")
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
    except Exception as e:
        log_event("SYSTEM", f"env persist {key} failed: {e}", level="WARNING")


def _normalize_openai_base(raw: str) -> str:
    """Normalize an OpenAI-compatible base URL. Never raises."""
    try:
        u = (raw or "").strip().strip('"').strip("'")
        if not u:
            return "https://api.openai.com/v1"
        # Users paste full chat URLs — trim to the API root.
        for suffix in ("/chat/completions", "/completions", "/responses"):
            if u.endswith(suffix):
                u = u[: -len(suffix)]
        u = u.rstrip("/")
        if not u.startswith(("http://", "https://")):
            low = u.lower()
            local = (low.startswith("localhost") or low.startswith("127.")
                     or low.startswith("0.0.0.0") or low.startswith("192.168.")
                     or low.startswith("10.") or low.startswith("[::1]"))
            u = ("http://" if local else "https://") + u
        return u.rstrip("/") or "https://api.openai.com/v1"
    except Exception:
        return "https://api.openai.com/v1"


def _mask_key(key: str) -> str:
    try:
        k = (key or "").strip()
        if not k:
            return "(empty)"
        if len(k) <= 8:
            return "***"
        return f"{k[:4]}…{k[-4:]}"
    except Exception:
        return "***"


def _match_session(sm: SessionManager, prefix: str):
    """Resolve an id prefix OR a 1-based number from `/sessions` to one session.

    Returns (sid, error_reply). Numbers refer to the same newest-first order
    `/sessions` prints, so `/open 1` opens the most recent conversation.
    """
    prefix = (prefix or "").strip()
    if not prefix:
        return None, "Usage: `/open <id|number>` — see `/sessions`."
    try:
        sessions = sm.list_sessions()
    except Exception as e:
        return None, f"Can't list sessions: {e}"
    if not sessions:
        return None, "No conversations yet."
    # Numeric shortcut: `/open 1` .. `/open 10`
    if prefix.isdigit():
        idx = int(prefix) - 1
        if 0 <= idx < min(len(sessions), 10):
            return sessions[idx].get("id"), ""
        return None, f"No session number `{prefix}` — see `/sessions` (1–{min(len(sessions), 10)})."
    cands = [s for s in sessions if s.get("id", "").startswith(prefix)]
    if not cands:
        # also try substring match on the id for convenience
        cands = [s for s in sessions if prefix in s.get("id", "")]
    if not cands:
        return None, f"No session starts with `{prefix}` — see `/sessions`."
    if len(cands) > 1:
        lines = "\n".join(f"- `{s['id']}` — {s.get('title', '')}" for s in cands[:8])
        return None, f"Ambiguous — matches {len(cands)}:\n{lines}\nBe more specific."
    return cands[0]["id"], ""


async def _fetch_ollama_models(host: Optional[str] = None) -> dict:
    """Live list from Ollama `GET {host}/api/tags`. Never raises.

    Returns {"models": [names...], "error": str|None, "host": str}.
    """
    h = _normalize_ollama_host(host or CFG.ollama_host)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(f"{h}/api/tags")
            r.raise_for_status()
            data = r.json()
        raw = data.get("models", []) if isinstance(data, dict) else []
        names = sorted(m.get("name", "") for m in raw if isinstance(m, dict) and m.get("name"))
        log_event("PROVIDER", f"ollama models ok host={h} count={len(names)}")
        return {"models": names, "error": None, "host": h}
    except Exception as e:
        err = str(e)[:220] or type(e).__name__
        log_event("PROVIDER", f"ollama models FAIL host={h}: {err}", level="WARNING")
        return {"models": [], "error": err, "host": h}


async def _fetch_openai_models(base_url: Optional[str] = None,
                               api_key: Optional[str] = None) -> dict:
    """Live list from an OpenAI-compatible `GET {base}/models`. Never raises.

    Works for OpenAI, OpenRouter, LM Studio, vLLM, Ollama-compat, etc.
    Returns {"models": [ids...], "error": str|None, "base_url": str, "status": int|None}.
    """
    base = _normalize_openai_base(base_url or CFG.base_url)
    key = (api_key if api_key is not None else CFG.api_key) or ""
    headers = {"User-Agent": "NEXUS/1.0"}
    if key.strip():
        headers["Authorization"] = f"Bearer {key.strip()}"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            r = await client.get(f"{base}/models", headers=headers)
            status = r.status_code
            if status == 401:
                msg = "401 Unauthorized — bad/missing API key (set with `/key <sk-...>`)."
                log_event("PROVIDER", f"openai models {msg} base={base}", level="WARNING")
                return {"models": [], "error": msg, "base_url": base, "status": status}
            if status == 404:
                msg = (f"404 from {base}/models — wrong base URL? "
                       f"Examples: `https://api.openai.com/v1`, `http://localhost:1234/v1`. "
                       f"Set with `/base-url <url>`.")
                log_event("PROVIDER", f"openai models {msg}", level="WARNING")
                return {"models": [], "error": msg, "base_url": base, "status": status}
            r.raise_for_status()
            data = r.json()
        items = data.get("data", data) if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = items.get("data", [])
        names: List[str] = []
        if isinstance(items, list):
            for m in items:
                if isinstance(m, dict) and m.get("id"):
                    names.append(str(m["id"]))
                elif isinstance(m, str):
                    names.append(m)
        names = sorted(set(names))
        log_event("PROVIDER", f"openai models ok base={base} count={len(names)}")
        return {"models": names, "error": None, "base_url": base, "status": status}
    except Exception as e:
        err = str(e)[:220] or type(e).__name__
        log_event("PROVIDER", f"openai models FAIL base={base}: {err}", level="WARNING")
        return {"models": [], "error": err, "base_url": base, "status": None}


def _ollama_models() -> List[str]:
    """Sync compat wrapper (blocks briefly). Prefer `_fetch_ollama_models()`."""
    try:
        import httpx as _hx
        host = CFG.ollama_host.rstrip("/")
        r = _hx.get(f"{host}/api/tags", timeout=6)
        return sorted(m.get("name", "") for m in r.json().get("models", []) if m.get("name"))
    except Exception:
        return []


async def handle_slash(text: str, ctx: dict) -> dict:
    """Parse + execute a slash command. ctx: {sm, sid}. Never raises."""
    try:
        parts = (text or "").strip().split()
        cmd = (parts[0].lower() if parts else "").split("@")[0]
        arg = " ".join(parts[1:]).strip()
        sm: SessionManager = (ctx or {}).get("sm")
        sid: str = (ctx or {}).get("sid", "") or ""
        if cmd in ("/new", "/sessions", "/open", "/delete", "/evolve") and sm is None:
            return {"handled": True, "reply": "Session store unavailable.", "action": None}

        if cmd == "/recursive":
            # Per-session NEST MODE, applied by the caller (TUI/web) which owns
            # the agent: explicit on/off only (this layer cannot see the flag).
            want = (arg or "").lower().strip()
            if want not in ("on", "off"):
                return {"handled": True,
                        "reply": "Usage: `/recursive on|off` — NEST MODE: workers "
                                 "may spawn their own sub-workers.",
                        "action": None}
            enabled = want == "on"
            log_event("TUI", f"slash /recursive -> {'on' if enabled else 'off'}")
            state = "ON (workers may spawn sub-workers)" if enabled else "OFF"
            return {"handled": True,
                    "reply": f"RECURSIVE MODE → **{state}**.",
                    "action": "recursive", "enabled": enabled, "sid": sid}

        if cmd == "/layout":
            # Hologram layout law (pure frontend visual): flow = computed-map
            # scatter around the caller, lattice = imaginary sphere-grid
            # around the caller. Applied by the caller (TUI/web) via
            # Entity3D.setLayoutMode; the web UI persists it per browser.
            global LAYOUT_MODE
            want = (arg or "").lower().strip()
            if want in ("lattice", "grid", "sphere"):
                mode = "lattice"
            elif want in ("flow", "map", "scatter"):
                mode = "flow"
            elif want in ("toggle", "switch", "flip"):
                mode = "flow" if LAYOUT_MODE == "lattice" else "lattice"
            else:
                return {"handled": True,
                        "reply": f"LAYOUT is **{LAYOUT_MODE.upper()}**. "
                                 f"Usage: `/layout flow|lattice|toggle` — flow = scatter "
                                 f"around the caller, lattice = sphere-grid around the caller.",
                        "action": None}
            LAYOUT_MODE = mode
            log_event("TUI", f"slash /layout -> {mode}")
            fancy = ("LATTICE (sphere-grid around the caller — crystal spheres, "
                     "random slots)") if mode == "lattice" else \
                    ("FLOW (computed-map scatter around the caller)")
            return {"handled": True,
                    "reply": f"LAYOUT → **{fancy}**.",
                    "action": "layout", "mode": mode, "sid": sid}

        if cmd == "/new":
            try:
                nsid = sm.create("interactive")
            except Exception as e:
                return {"handled": True, "reply": f"Can't create session: {e}", "action": None}
            log_event("TUI", f"slash /new -> {nsid}")
            return {"handled": True, "reply": f"New session `{nsid}`.", "action": "new", "sid": nsid}

        if cmd == "/sessions":
            try:
                sessions = sm.list_sessions()[:10]
            except Exception as e:
                return {"handled": True, "reply": f"Can't list sessions: {e}", "action": None}
            if not sessions:
                return {"handled": True, "reply": "No conversations yet.", "action": None}
            lines = []
            for i, s in enumerate(sessions, 1):
                mark = " **← active**" if s.get("id") == sid else ""
                msgs = s.get("messages", []) or []
                first = next((m.get("content", "") for m in msgs if m.get("role") == "user"), "")
                upd = str(s.get("updated_at", ""))[:16].replace("T", " ")
                lines.append(f"{i}. `{s['id']}`{mark} — {s.get('title', '')} "
                             f"({len(msgs)} msgs{(', ' + upd) if upd else ''}) "
                             f"{('“' + _short(first, 60) + '”') if first else ''}")
            return {"handled": True,
                    "reply": "Recent conversations (`/open <number|id>`):\n" + "\n".join(lines),
                    "action": None}

        if cmd == "/open":
            nsid, err = _match_session(sm, arg)
            if err:
                return {"handled": True, "reply": err, "action": None}
            if nsid == sid:
                return {"handled": True, "reply": "Already on that session.", "action": None}
            return {"handled": True, "reply": f"Switched to `{nsid}`.", "action": "switch", "sid": nsid}

        if cmd == "/delete":
            nsid, err = _match_session(sm, arg)
            if err:
                # keep usage hint specific to /delete
                if err.startswith("Usage: `/open"):
                    err = err.replace("`/open <id|number>`", "`/delete <id|number>`")
                return {"handled": True, "reply": err, "action": None}
            if nsid == sid:
                return {"handled": True,
                        "reply": "Can't delete the active session — `/open` another one first.",
                        "action": None}
            import shutil
            try:
                shutil.rmtree(sm.root / nsid, ignore_errors=True)
            except Exception as e:
                return {"handled": True, "reply": f"Delete failed: {e}", "action": None}
            log_event("TUI", f"slash /delete {nsid}")
            return {"handled": True, "reply": f"Deleted `{nsid}`.", "action": None}

        if cmd == "/models":
            which = (arg or "").strip().lower()
            if which in ("", "current", "here"):
                which = CFG.provider
            if which not in ("ollama", "openai", "all"):
                return {"handled": True,
                        "reply": f"Usage: `/models [ollama|openai|all]` — got `{arg}`.",
                        "action": None}

            async def _section_ollama():
                res = await _fetch_ollama_models()
                cur = CFG.ollama_model
                if res["error"] and not res["models"]:
                    return (f"**Ollama** ({res['host']}) — unreachable: `{_short(res['error'], 160)}`\n"
                            f"Current: `{cur}`. Is `ollama serve` running? "
                            f"Set host with `/ollama-host <url>`.")
                lines = "\n".join(f"- `{n}`" + (" **← active**" if n == cur else "")
                                   for n in res["models"])
                head = f"**Ollama** ({res['host']}) — {len(res['models'])} models (`/model <name>`):"
                tail = f"\n⚠️ list warning: `{_short(res['error'], 140)}`" if res["error"] else ""
                return f"{head}\n{lines}{tail}"

            async def _section_openai():
                res = await _fetch_openai_models()
                cur = CFG.model
                if res["error"] and not res["models"]:
                    return (f"**OpenAI-compatible** ({res['base_url']}) — unreachable: "
                            f"`{_short(res['error'], 200)}`\n"
                            f"Current: `{cur}`. Check `/base-url`, `/key`, then `/models openai` again.")
                lines = "\n".join(f"- `{n}`" + (" **← active**" if n == cur else "")
                                   for n in res["models"][:100])
                extra = f"\n…+{len(res['models']) - 100} more" if len(res["models"]) > 100 else ""
                head = (f"**OpenAI-compatible** ({res['base_url']}) — "
                        f"{len(res['models'])} models (`/model <name>`):")
                tail = f"\n⚠️ list warning: `{_short(res['error'], 140)}`" if res["error"] else ""
                key_hint = "" if (CFG.api_key or "").strip() else "\n⚠️ no API key set — use `/key <sk-...>`."
                return f"{head}\n{lines}{extra}{tail}{key_hint}"

            if which == "ollama":
                return {"handled": True, "reply": await _section_ollama(), "action": None}
            if which == "openai":
                return {"handled": True, "reply": await _section_openai(), "action": None}
            # all: fetch both concurrently
            import asyncio as _aio
            oll_txt, oai_txt = await _aio.gather(_section_ollama(), _section_openai())
            return {"handled": True,
                    "reply": f"{oll_txt}\n\n{oai_txt}\n\nActive provider: `{CFG.provider}` "
                             f"(switch with `/provider <ollama|openai>`).",
                    "action": None}

        if cmd == "/model":
            if not arg:
                cur = CFG.ollama_model if CFG.provider == "ollama" else CFG.model
                return {"handled": True, "reply": f"Current model (`{CFG.provider}`): `{cur}`. "
                                                  f"Usage: `/model <name>` (see `/models`).",
                        "action": None}
            name = arg.strip().strip('"').strip("'")
            if CFG.provider == "ollama":
                res = await _fetch_ollama_models()
                names = res["models"]
                if names and name not in names:
                    close = [n for n in names if name.lower() in n.lower()][:3]
                    if not close:
                        # try token overlap for typos like llama3 vs llama3.1
                        base = name.lower().split(":")[0]
                        close = [n for n in names if base and base in n.lower()][:3]
                    hint = f"\nDid you mean: {', '.join(f'`{c}`' for c in close)}?" if close else ""
                    return {"handled": True,
                            "reply": f"Unknown Ollama model `{name}`.{hint}\nSee `/models ollama`.",
                            "action": None}
                CFG.ollama_model = name
                update_env_file("OLLAMA_MODEL", name)
                warn = (f" (⚠️ list unreachable — switched anyway: "
                        f"`{_short(res['error'], 120)}`)" if res["error"] and not names else "")
                log_event("SYSTEM", f"slash /model -> ollama/{name}")
                return {"handled": True,
                        "reply": f"Model switched to `{name}` on **ollama** (saved, live now).{warn}",
                        "action": None}
            else:
                res = await _fetch_openai_models()
                names = res["models"]
                if names and name not in names:
                    close = [n for n in names if name.lower() in n.lower()][:3]
                    hint = f"\nDid you mean: {', '.join(f'`{c}`' for c in close)}?" if close else ""
                    return {"handled": True,
                            "reply": f"Unknown model `{name}` for `{res['base_url']}`.{hint}\n"
                                     f"See `/models openai`.",
                            "action": None}
                CFG.model = name
                update_env_file("LLM_MODEL", name)
                warn = (f" (⚠️ list unreachable — switched anyway: "
                        f"`{_short(res['error'], 140)}`)" if res["error"] and not names else "")
                log_event("SYSTEM", f"slash /model -> openai/{name}")
                return {"handled": True,
                        "reply": f"Model switched to `{name}` on **openai** ({CFG.base_url}) "
                                 f"(saved, live now).{warn}",
                        "action": None}

        if cmd == "/provider":
            want = (arg or "").strip().lower()
            if not want:
                cur_model = CFG.ollama_model if CFG.provider == "ollama" else CFG.model
                return {"handled": True,
                        "reply": (f"Provider: **{CFG.provider}** — model `{cur_model}`\n"
                                  f"- ollama host: `{CFG.ollama_host}`\n"
                                  f"- openai base: `{CFG.base_url}` key: `{_mask_key(CFG.api_key)}`\n"
                                  f"Switch with `/provider <ollama|openai>`. List with `/models [all]`."),
                        "action": None}
            if want not in ("ollama", "openai"):
                return {"handled": True,
                        "reply": f"Usage: `/provider <ollama|openai>` — got `{arg}`.",
                        "action": None}
            CFG.provider = want
            update_env_file("LLM_PROVIDER", want)
            log_event("SYSTEM", f"slash /provider -> {want}")
            cur_model = CFG.ollama_model if want == "ollama" else CFG.model
            return {"handled": True,
                    "reply": f"Provider → **{want}** (model `{cur_model}`, saved, live now). "
                             f"See `/models {want}`.",
                    "action": None}

        if cmd in ("/base-url", "/baseurl", "/base_url"):
            if not arg:
                return {"handled": True,
                        "reply": f"OpenAI base URL: `{CFG.base_url}`\nSet with `/base-url <url>`.",
                        "action": None}
            norm = _normalize_openai_base(arg)
            CFG.base_url = norm
            update_env_file("OPENAI_BASE_URL", norm)
            log_event("SYSTEM", f"slash /base-url -> {norm}")
            probe = await _fetch_openai_models(base_url=norm)
            if probe["error"] and not probe["models"]:
                return {"handled": True,
                        "reply": f"Base URL saved → `{norm}` (live now), "
                                 f"but `/models` check failed: `{_short(probe['error'], 200)}`.",
                        "action": None}
            return {"handled": True,
                    "reply": f"Base URL → `{norm}` (saved, live now) — "
                             f"{len(probe['models'])} models reachable. See `/models openai`.",
                    "action": None}

        if cmd in ("/key", "/api-key", "/apikey", "/api_key"):
            if not arg:
                return {"handled": True,
                        "reply": f"API key: `{_mask_key(CFG.api_key)}` "
                                 f"(base `{CFG.base_url}`). Set with `/key <sk-...>`.",
                        "action": None}
            secret = arg.strip().strip('"').strip("'")
            if len(secret) < 4:
                return {"handled": True, "reply": "That key looks too short — nothing saved.",
                        "action": None}
            CFG.api_key = secret
            update_env_file("OPENAI_API_KEY", secret)
            log_event("SYSTEM", "slash /key -> updated (masked)")
            return {"handled": True,
                    "reply": f"API key saved (`{_mask_key(secret)}`, live now). "
                             f"Verify with `/models openai`.",
                    "action": None}

        if cmd in ("/ollama-host", "/ollama_host", "/ollama"):
            if not arg:
                return {"handled": True,
                        "reply": f"Ollama host: `{CFG.ollama_host}`\nSet with `/ollama-host <url>`.",
                        "action": None}
            norm = _normalize_ollama_host(arg)
            CFG.ollama_host = norm
            update_env_file("OLLAMA_HOST", norm)
            log_event("SYSTEM", f"slash /ollama-host -> {norm}")
            probe = await _fetch_ollama_models(host=norm)
            if probe["error"] and not probe["models"]:
                return {"handled": True,
                        "reply": f"Ollama host saved → `{norm}` (live now), "
                                 f"but check failed: `{_short(probe['error'], 180)}`.",
                        "action": None}
            return {"handled": True,
                    "reply": f"Ollama host → `{norm}` (saved, live now) — "
                             f"{len(probe['models'])} models reachable. See `/models ollama`.",
                    "action": None}

        if cmd == "/thinking":
            if not arg:
                return {"handled": True,
                        "reply": f"Reasoning effort: **{CFG.think_effort}** "
                                 f"(tokens {CFG.max_tokens}, up to {CFG.max_iterations} iters).\n"
                                 f"Set with `/thinking low|medium|high`.", "action": None}
            lvl = arg.lower().strip()
            if lvl not in ("low", "medium", "high"):
                return {"handled": True, "reply": "Usage: `/thinking low|medium|high`.", "action": None}
            CFG.think_effort = lvl
            update_env_file("THINK_EFFORT", lvl)
            log_event("SYSTEM", f"slash /thinking -> {lvl}")
            note = " (Ollama native depth)" if CFG.provider == "ollama" else " (stored; applies to Ollama)"
            return {"handled": True, "reply": f"Thinking effort → **{lvl}**{note}.", "action": None}

        if cmd == "/memory":
            try:
                data = MEM.read("")
            except Exception as e:
                return {"handled": True, "reply": f"Memory read failed: {e}", "action": None}
            prof = data.get("profile", {}) or {}
            facts = data.get("facts", []) or []
            if not prof and not facts:
                return {"handled": True, "reply": "Memory is empty — I don't know you yet. Tell me your name!",
                        "action": None}
            lines = []
            if prof:
                lines.append("Profile: " + "; ".join(f"{k}={v}" for k, v in prof.items()))
            lines += [f"- {f.get('text', '')}" for f in facts[-10:]]
            total = data.get("total_facts", 0) or 0
            extra = f"\n…+{total - 10} more" if total > 10 else ""
            return {"handled": True, "reply": "What I remember:\n" + "\n".join(lines) + extra,
                    "action": None}

        if cmd in ("/help", "/h", "/?"):
            lines = "\n".join(f"- `{c['usage']}` — {c['desc']}" for c in SLASH_COMMANDS)
            cur_model = CFG.ollama_model if CFG.provider == "ollama" else CFG.model
            return {"handled": True,
                    "reply": (f"Commands (provider `{CFG.provider}`, model `{cur_model}`):\n"
                              + lines),
                    "action": None}

        if cmd == "/clear":
            return {"handled": True, "reply": "", "action": "clear"}

        if cmd == "/evolve":
            import evolution
            from pathlib import Path as _P
            repo = _P(__file__).parent
            sub = (arg or "").strip().split()
            if sub and sub[0].lower() == "history":
                h = evolution.history()
                lines = [f"Prompt **{h['current']}** · {len(h['rules'])} active rules"]
                for t in h["transitions"][-8:]:
                    lines.append(f"- `{t.get('ts', '')[:16]}` {t.get('kind')}: "
                                 f"{t.get('from', '?')} → {t.get('to', '?')} — "
                                 f"{str(t.get('reason', ''))[:100]}")
                return {"handled": True, "reply": "\n".join(lines), "action": None}
            if sub and sub[0].lower() == "rollback" and len(sub) > 1:
                base_text = CFG.system_prompt_path.read_text(encoding="utf-8") \
                    if CFG.system_prompt_path.exists() else "You are NEXUS."
                r = evolution.rollback(base_text, sub[1])
                if not r.get("ok"):
                    return {"handled": True, "reply": f"Rollback failed: {r.get('error')}", "action": None}
                return {"handled": True,
                        "reply": f"Prompt rolled back to **{r['to']}** "
                                 f"({r['active_rules']} active rules). New sessions use it immediately.",
                        "action": None}
            if sub and sub[0].lower() == "why" and len(sub) > 1:
                import evolution as _evo_why
                lin = _evo_why.lineage_for_rule(sub[1])
                if not lin.get("ok"):
                    return {"handled": True, "reply": f"Lineage failed: {lin.get('error')}", "action": None}
                out = [f"**Why does the prompt say this?** (`{lin['version']}`)",
                       f"Rule: {lin['rule'][:300]}",
                       f"Reason: {lin['reason'][:200]}"]
                if lin.get("linked_failures"):
                    out.append("Linked failures:")
                    out += [f"- `{f.get('ts', '')[:16]}` {f.get('kind')}: {f.get('detail', '')[:120]}"
                            for f in lin["linked_failures"]]
                if lin.get("linked_memories"):
                    out.append("Linked memories:")
                    out += [f"- `{m.get('id')}` [{m.get('type')}] {m.get('content', '')[:120]}"
                            for m in lin["linked_memories"]]
                ev = lin.get("evolution_record", {}) or {}
                if ev.get("ts"):
                    out.append(f"Recorded {ev.get('ts', '')[:16]} (gen evolution).")
                return {"handled": True, "reply": "\n".join(out), "action": None}
            if sub and sub[0].lower() == "revalidate":
                import evolution
                from pathlib import Path as _P2
                v = evolution.revalidate(_P2(__file__).parent)
                return {"handled": True,
                        "reply": f"Memory revalidated against current files: "
                                 f"{v['reconfirmed']} ok, {v['moved']} moved, "
                                 f"{v['stale']} stale, {v['skipped']} skipped "
                                 f"({v['total']} total).",
                        "action": None}
            llm = LLMProvider()
            base_text = CFG.system_prompt_path.read_text(encoding="utf-8") \
                if CFG.system_prompt_path.exists() else "You are NEXUS."
            try:
                cycle = await evolution.evolve_once(
                    sm.root, repo, base_text, llm,
                    task_ref=f"slash:{(sid or '?')[:8]}",
                    log_fn=lambda m: log_event("EVOLVE", str(m)[:200]))
            except Exception as e:
                return {"handled": True, "reply": f"Evolution cycle failed: {e}", "action": None}
            ap = cycle.get("applied", {})
            lines = [f"Evolution cycle done — prompt **{cycle.get('prompt_before')} → "
                     f"{cycle.get('prompt_after')}**",
                     f"observations: {len(cycle.get('observations', []))}, "
                     f"facts: {len(ap.get('facts', []))}, rules: {len(ap.get('rules', []))}, "
                     f"skipped: {ap.get('skipped', 0)}, repo changes: {cycle.get('repo_changes', 0)}"]
            for d in (cycle.get("decisions", []) or [])[:6]:
                if d.get("verdict") in ("fact", "rule"):
                    lines.append(f"- [{d['verdict']}] {str(d.get('text', ''))[:140]}")
            return {"handled": True, "reply": "\n".join(lines), "action": None}

        if cmd.startswith("/"):
            known = ", ".join(c["cmd"] for c in SLASH_COMMANDS)
            return {"handled": True,
                    "reply": f"Unknown command `{cmd}`. Try one of: {known}",
                    "action": None}
        return {"handled": False, "reply": "", "action": None}
    except Exception as e:
        log_event("TUI", f"slash failed: {e}", level="ERROR")
        return {"handled": True, "reply": f"Command failed: {e}", "action": None}


# ============================================================
#  TUI — TEXTUAL APP WITH CUSTOM THEME
# ============================================================
NEXUS_THEME = Theme(
    name="nexus",
    primary="#7C3AED",        # violet
    secondary="#06B6D4",      # cyan
    accent="#F59E0B",         # amber
    warning="#EF4444",        # red
    error="#DC2626",
    success="#10B981",        # emerald
    foreground="#E5E7EB",
    background="#0F172A",     # slate-900
    surface="#1E293B",        # slate-800
    panel="#334155",          # slate-700
    dark=True,
)


class NexusTUI(App):
    """Production TUI for the NEXUS multi-agent system."""
    CSS = """
    Screen { background: $background; }
    #main { layout: horizontal; height: 100%; }
    #left { width: 55%; border-right: solid $panel; }
    #right { width: 45%; }
    .panel-title { padding: 0 1; text-style: bold; color: $accent; }
    #chat-log { height: 1fr; border: solid $panel; margin: 0 1; }
    #input-row { height: 3; margin: 0 1; }
    #status-bar { height: 3; background: $surface; padding: 0 1; }
    #tools-table { height: 10; margin: 0 1; }
    #agents-log { height: 12; border: solid $panel; margin: 0 1; }
    #plan { height: 7; border: solid $panel; margin: 0 1; }
    #session-tree { height: 8; margin: 0 1; }
    #stats { height: 6; margin: 0 1; }
    #logs-log { height: 10; border: solid $panel; margin: 0 1; }
    .stat-box { border: solid $panel; padding: 0 1; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+n", "new_session", "New Session"),
        Binding("ctrl+l", "clear_log", "Clear"),
        Binding("ctrl+s", "save", "Save"),
        Binding("f1", "help", "Help"),
    ]

    current_session: reactive[str] = reactive("")

    def __init__(self):
        super().__init__()
        self.sm = SessionManager()
        self.llm = LLMProvider()
        self.agent: Optional[NexusAgent] = None
        self.sessions = self.sm.list_sessions()
        self.env = detect_environment()
        self.tool_call_count = 0
        self.total_elapsed = 0.0
        self.subagent_done_count = 0
        # ── multi-agent war-room state ──
        self.round_plans: Dict[str, dict] = {}  # rid -> {workers, plan, done}
        self.active_round: str = ""
        # ── health / liveness state (shown in status-bar) ──
        self.llm_status: str = "⏳ CHECKING"
        self.llm_ok: Optional[bool] = None  # None=unknown, True=online, False=offline
        self.agent_state: str = "IDLE"
        self.last_tool_name: str = "—"
        self.last_tool_ok: Optional[bool] = None
        self.last_tool_elapsed: float = 0.0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="main"):
            with Vertical(id="left"):
                yield Label("💬 CONVERSATION", classes="panel-title")
                yield RichLog(id="chat-log", highlight=True, markup=True, wrap=True)
                with Horizontal(id="input-row"):
                    yield Input(placeholder="Ask NEXUS anything…  (Enter to send)", id="user-input")
            with VerticalScroll(id="right"):
                yield Label("🔧 TOOL CALLS", classes="panel-title")
                yield DataTable(id="tools-table")
                yield Label("📻 AGENTS (worker radio + activity)", classes="panel-title")
                yield RichLog(id="agents-log", highlight=True, markup=False, wrap=True, max_lines=300)
                yield Label("📋 PLAN (joint worker plan)", classes="panel-title")
                yield Static("No active round", id="plan")
                yield Label("📁 SESSIONS", classes="panel-title")
                yield Tree("Sessions", id="session-tree")
                yield Label("📊 STATS", classes="panel-title")
                yield Static(id="stats")
                yield Label("📝 LOGS (nexus.log)", classes="panel-title")
                yield RichLog(id="logs-log", highlight=True, markup=False, wrap=True, max_lines=200)
        yield Static(id="status-bar")
        yield Footer()

    def on_mount(self):
        self.register_theme(NEXUS_THEME)
        self.theme = "nexus"
        table = self.query_one("#tools-table", DataTable)
        table.add_columns("Tool", "Params", "Status", "Time")
        self._refresh_sessions()
        self._refresh_stats()
        self._new_session()
        log_event("TUI", "app mounted", extra={"provider": CFG.provider,
                  "model": CFG.model if CFG.provider != "ollama" else CFG.ollama_model})
        self._log_system("Welcome to NEXUS. Type a request below.")
        self._log_system(f"OS: {self.env['os']} {self.env['release']} | Shells: {', '.join(s['name'] for s in self.env['shells'])}")
        self._log_system(f"Workspace root: {CFG.workspace_root}")
        self._log_system(f"Logs: {LOG_FILE} (every tool/model/save/result)")
        self._update_status()
        self._tail_logs()
        # live tail of nexus.log + async LLM health probe (proof the app works)
        self.set_interval(2.0, self._tail_logs)
        self.run_worker(self._health_check(), exclusive=False)

    # ── health / status-bar / logs panel ──
    def _update_status(self):
        """Status-bar is the 'does it work?' indicator. Never raises."""
        try:
            bar = self.query_one("#status-bar", Static)
        except Exception:
            return
        if self.llm_ok is True:
            llm_dot = "🟢 ONLINE"
        elif self.llm_ok is False:
            llm_dot = "🔴 OFFLINE"
        else:
            llm_dot = "🟡 CHECKING"
        model = CFG.ollama_model if CFG.provider == "ollama" else CFG.model
        if self.last_tool_ok is True:
            last = f"✅ {self.last_tool_name} ({self.last_tool_elapsed:.2f}s)"
        elif self.last_tool_ok is False:
            last = f"❌ {self.last_tool_name} ({self.last_tool_elapsed:.2f}s)"
        else:
            last = "— no tools yet —"
        try:
            log_lines = sum(1 for _ in LOG_FILE.open("r", encoding="utf-8", errors="replace")) if LOG_FILE.exists() else 0
        except Exception:
            log_lines = 0
        bar.update(
            f"[{llm_dot}] LLM:{CFG.provider}/{model} | "
            f"State:{self.agent_state} | "
            f"Last:{last} | "
            f"Tools:{self.tool_call_count} ({self.total_elapsed:.1f}s) | "
            f"Workers:{self.subagent_done_count} | "
            f"Log:{LOG_FILE.name} {log_lines} lines"
        )

    def _set_state(self, state: str):
        self.agent_state = state
        self._update_status()

    def _tail_logs(self):
        """Refresh the 📝 LOGS panel with the tail of nexus.log."""
        try:
            panel = self.query_one("#logs-log", RichLog)
        except Exception:
            return
        try:
            lines = get_log_tail(30)
            panel.clear()
            for ln in lines:
                # RichLog markup=False so raw log lines are safe
                panel.write(ln)
        except Exception:
            pass

    def _push_log_line(self, line: str):
        try:
            self.query_one("#logs-log", RichLog).write(line)
        except Exception:
            pass

    def _agents_line(self, line: str):
        """One line into the 📻 AGENTS war-room feed. Never raises."""
        try:
            self.query_one("#agents-log", RichLog).write(line)
        except Exception:
            pass

    def _render_plan(self):
        """Refresh the 📋 PLAN panel from the active round. Never raises."""
        try:
            panel = self.query_one("#plan", Static)
        except Exception:
            return
        try:
            rnd = self.round_plans.get(self.active_round)
            if not rnd:
                panel.update("No active round")
                return
            steps = rnd.get("plan", [])
            if not steps:
                panel.update(f"{self.active_round}: plan empty — workers coordinate via radio")
                return
            icon = {"open": "⬜", "doing": "🔄", "done": "✅"}
            lines = [f"{self.active_round} ({len(rnd.get('workers', []))} workers)"]
            for s in steps[:5]:
                owner = f" @{s['owner']}" if s.get("owner") else ""
                lines.append(f"{icon.get(s.get('status'), '?')} {s['id']} {_short(s.get('title', ''), 34)}{owner}")
            if len(steps) > 5:
                lines.append(f"… +{len(steps) - 5} more")
            if rnd.get("done"):
                lines.append("━━ round closed ━━")
            panel.update("\n".join(lines))
        except Exception:
            pass

    async def _health_check(self):
        """Ping the LLM once at startup: proves model + logging pipeline work."""
        self.llm_status = "⏳ CHECKING"
        self.llm_ok = None
        self._update_status()
        log_event("TUI", "health check start")
        try:
            out = await self.llm.chat([{"role": "user", "content": "ping"}])
            content = (out.get("content") or "")
            if content.startswith("[LLM error]") or content.startswith("[Ollama error]"):
                self.llm_ok = False
                self.llm_status = "🔴 OFFLINE"
                self._log_system(f"LLM health: OFFLINE — {_short(content, 200)}")
                log_event("TUI", "health check FAILED", level="ERROR", extra={"reply": content})
            else:
                self.llm_ok = True
                self.llm_status = "🟢 ONLINE"
                self._log_system(f"LLM health: ONLINE ({CFG.provider}) — logging to {LOG_FILE.name} ✓")
                log_event("TUI", "health check OK", extra={"preview": _short(content, 200)})
        except Exception as e:
            self.llm_ok = False
            self.llm_status = "🔴 OFFLINE"
            self._log_system(f"LLM health: OFFLINE — {e}")
            log_event("TUI", f"health check EXCEPTION: {e}", level="ERROR")
        self._update_status()
        self._tail_logs()

    # ── session management ──
    def _new_session(self):
        sid = self.sm.create("interactive")
        self._attach_session(sid)
        self._log_system(f"New session: {sid}")
        log_event("TUI", f"new session {sid}")
        self._tail_logs()

    def _attach_session(self, sid: str):
        """Bind the TUI to a session (new or restored old conversation)."""
        self.current_session = sid
        self.agent = NexusAgent(sid, self.sm, self.llm)
        self.agent.on_event = self._on_agent_event
        n = self.agent.restore_history()
        self._refresh_sessions()
        self._refresh_stats()
        self._update_status()
        return n

    def _refresh_sessions(self):
        tree = self.query_one("#session-tree", Tree)
        tree.clear()
        root = tree.root
        root.expand()
        for s in self.sm.list_sessions()[:20]:
            label = f"{s['id'][:15]}…  {s['title']}"
            node = root.add(label)
            node.data = s["id"]

    def _refresh_stats(self):
        stats = self.query_one("#stats", Static)
        if not self.current_session:
            stats.update("No active session")
            return
        try:
            meta = self.sm.load(self.current_session)
        except Exception as e:
            # One corrupt session.json must not break the input loop.
            stats.update(f"Session data unavailable: {e}\n(see nexus.log)")
            return
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan")
        t.add_column()
        t.add_row("Session", self.current_session[:20])
        t.add_row("Messages", str(len(meta.get("messages", []))))
        t.add_row("Tool calls", str(len(meta.get("tool_calls", []))))
        try:
            mem_n = len(MEM.data.get("facts", []))
            mem_who = MEM.data.get("profile", {}).get("name", "—")
            t.add_row("Memory", f"{mem_n} facts · {mem_who}")
        except Exception:
            pass
        t.add_row("Workspace", str(self.sm.workspace(self.current_session)))
        stats.update(t)

    # ── agent event handler (thread-aware: same-loop vs background thread) ──
    def _on_agent_event(self, event: str, data: dict):
        """Deliver agent events to the UI thread without deadlocking.

        call_from_thread() RAISES RuntimeError when invoked from the app's
        own thread — exactly our case when agent.run() is awaited in
        on_input_submitted. That exception used to be swallowed, so no
        iteration/tool/final update ever reached the chat. Same-thread
        events go via call_later(); only real background threads use
        call_from_thread().
        """
        try:
            import threading
            if threading.get_ident() == getattr(self, "_thread_id", None):
                self.call_later(self._handle_event, event, data)
            else:
                self.call_from_thread(self._handle_event, event, data)
        except Exception as e:
            log_event("TUI", f"event dispatch {event} failed: {e}", level="ERROR")
            try:
                self.call_later(self._handle_event, event, data)
            except Exception:
                pass

    def _handle_event(self, event: str, data: dict):
        # One bad render must never kill the event stream.
        try:
            self._render_event(event, data)
        except Exception as e:
            log_event("TUI", f"render {event} failed: {e}", level="ERROR")
            try:
                self.query_one("#chat-log", RichLog).write(
                    Text(f"[render error ({event}): {e}]", style="red"))
            except Exception:
                pass

    def _render_event(self, event: str, data: dict):
        log = self.query_one("#chat-log", RichLog)
        table = self.query_one("#tools-table", DataTable)
        if event == "iteration":
            log.write(Text(f"── iteration {data['n']}/{data['max']} ──", style="dim"))
            self._set_state(f"WORKING iter {data['n']}/{data['max']}")
        elif event == "tool_result":
            name = data["name"]
            params = data["params"]
            result = data["result"]
            elapsed = data["elapsed"]
            ok = result.get("success", False) if isinstance(result, dict) else False
            status = "✅" if ok else "❌"
            param_str = json.dumps(params, ensure_ascii=False)[:60]
            table.add_row(name, param_str, status, f"{elapsed:.2f}s")
            self.tool_call_count += 1
            self.total_elapsed += elapsed
            # health indicator: last tool outcome drives 🟢/🔴 proof-of-work
            self.last_tool_name = name
            self.last_tool_ok = bool(ok)
            self.last_tool_elapsed = elapsed
            self._refresh_stats()
            self._update_status()
            self._tail_logs()
            # Show brief result in chat
            snippet = ""
            if isinstance(result, dict):
                if "stdout" in result:
                    snippet = (result["stdout"] or "")[:300]
                elif "content" in result:
                    snippet = (result["content"] or "")[:300]
                elif "error" in result:
                    snippet = result["error"][:300]
            if snippet:
                log.write(Text(f"  ↳ {snippet}", style="dim"))
        elif event == "final":
            content = (data.get("content") or "").strip()
            model = CFG.ollama_model if CFG.provider == "ollama" else CFG.model
            title = f"NEXUS ⬥ {CFG.provider}/{model}"
            subtitle = f"{(self.current_session or '')[:19]} · {datetime.now():%H:%M:%S}"
            if content:
                # Comprehensive Markdown: headings, bold/italic, lists,
                # tables, blockquotes, links + syntax-highlighted code
                # blocks (monokai) with python fallback for bare fences.
                md = Markdown(content, code_theme="monokai",
                              hyperlinks=True, inline_code_lexer="python")
                log.write(Panel(md, title=title, subtitle=subtitle,
                                border_style="cyan", padding=(1, 2)))
            else:
                # Tool-only turn: model returned no prose, only tool calls.
                # Never show a blank bubble — point at the tool table.
                log.write(Panel(
                    Text("Done — no prose reply, see TOOL CALLS above for results.",
                         style="dim"),
                    title=title, subtitle=subtitle, border_style="yellow"))
            self._set_state("IDLE ✓ done")
            self._tail_logs()
        elif event == "tools_start":
            log.write(Text(f"⚡ Executing {data['count']} tool(s)…", style="yellow"))
            self._set_state(f"WORKING ⚡ {data['count']} tool(s)")
        elif event == "tools_done":
            self._set_state("WORKING reasoning…")
        elif event == "delegate_start":
            rid = data.get("round", "?")
            ids = data.get("ids", [])
            self.round_plans[rid] = {"workers": list(ids), "plan": [], "done": False}
            self.active_round = rid
            log.write(Text(f"👥 Leader delegating {data['count']} task(s) → {', '.join(ids)} "
                           f"(round {rid}, running concurrently)…",
                           style="yellow"))
            self._agents_line(f"━━ ROUND {rid} open — workers: {', '.join(ids)} ━━")
            self._render_plan()
            self._set_state(f"WORKING 👥 {data['count']} workers")
        elif event == "subagent_start":
            self._agents_line(f"▶ {data['id']} started: {_short(data.get('brief', ''), 120)}")
            self._set_state(f"WORKING 👷 {data['id']}")
        elif event == "subagent_done":
            ok = bool(data.get("success"))
            table.add_row(f"👥 {data['id']}", _short(data.get("brief", ""), 60),
                          "✅" if ok else "❌", f"{data.get('elapsed', 0):.2f}s")
            self.subagent_done_count += 1
            self.last_tool_name = f"worker {data['id']}"
            self.last_tool_ok = ok
            self.last_tool_elapsed = data.get("elapsed", 0)
            snippet = _short(data.get("output", ""), 200)
            self._agents_line(f"■ {data['id']} done ({data.get('elapsed', 0):.1f}s, "
                              f"{data.get('tools_used', 0)} tools, "
                              f"radio_seen={data.get('radio_seen', 0)}): {snippet}")
            self._update_status()
            self._tail_logs()
        elif event == "radio":
            self._agents_line(f"📻 {data['from']}→{data['to']}: {data.get('text', '')}")
        elif event == "plan":
            rid = data.get("round", "")
            if rid in self.round_plans:
                self.round_plans[rid]["plan"] = data.get("plan", [])
                if rid == self.active_round:
                    self._render_plan()
            step = f" {data.get('step', '')}" if data.get("step") else ""
            self._agents_line(f"📋 {data['by']} {data.get('action', '')}{step} (plan v{data.get('version', 0)})")
        elif event == "idle":
            self._agents_line(f"◌ {data.get('id', '?')} idle — lingering for round traffic")
        elif event == "wake":
            self._agents_line(f"◉ {data.get('id', '?')} woke on {data.get('msgs', 0)} msg(s)")
        elif event == "delegate_done":
            rid = data.get("round", "?")
            if rid in self.round_plans:
                self.round_plans[rid]["plan"] = data.get("plan", self.round_plans[rid]["plan"])
                self.round_plans[rid]["done"] = True
                if rid == self.active_round:
                    self._render_plan()
            log.write(Text(f"👥 Workers done: {data['ok']} ok / {data['failed']} failed "
                           f"({data.get('elapsed', 0):.1f}s) — leader synthesizing…", style="yellow"))
            self._agents_line(f"━━ ROUND {rid} closed — leader synthesizing… ━━")
            self._set_state("WORKING synthesizing…")

    # ── input handling ──
    async def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        log = self.query_one("#chat-log", RichLog)
        if text.startswith("/"):
            res = await handle_slash(text, {"sm": self.sm, "sid": self.current_session})
            if res.get("handled"):
                action = res.get("action")
                if action == "new":
                    # handle_slash already created the session — attach to it,
                    # don't create a second orphan session.
                    if res.get("sid"):
                        self._attach_session(res["sid"])
                        self._log_system(f"New session: {res['sid']}")
                    else:
                        self._new_session()
                elif action == "switch":
                    n = self._attach_session(res["sid"])
                    self._log_system(f"Opened {res['sid']} ({n} messages restored)")
                elif action == "clear":
                    log.clear()
                elif action == "recursive":
                    try:
                        self.agent.recursive_mode = bool(res.get("enabled"))
                    except Exception:
                        pass
                if res.get("reply"):
                    log.write(Panel(Markdown(res["reply"]), title="CMD",
                                    subtitle=text.split()[0], border_style="amber"))
                self._refresh_stats()
                self._update_status()
                self._tail_logs()
                return
            # not a slash command (shouldn't happen — unknown /cmd returns
            # handled=True with a hint) — fall through to the agent
        log.write(Panel(Text(text, style="bold white"), title="You", border_style="violet"))
        log_event("TUI", f"user input sid={self.current_session} text={_short(text, 500)!r}")
        self._set_state("WORKING thinking…")
        self._log_system("Thinking…")
        try:
            result = await self.agent.run(text)
            self._refresh_stats()
            self._set_state("IDLE ✓ done")
        except Exception as e:
            log.write(Panel(Text(f"Error: {e}", style="red"), title="System"))
            log_event("TUI", f"agent run EXCEPTION: {e}", level="ERROR")
            self._set_state("IDLE ❌ error")
        try:
            # Tail may run during app shutdown (Ctrl+Q mid-run unmounts widgets).
            self._refresh_stats()
            self._update_status()
            self._tail_logs()
        except Exception:
            pass

    def _log_system(self, msg: str):
        log = self.query_one("#chat-log", RichLog)
        log.write(Text(f"▸ {msg}", style="dim cyan"))
        log_event("TUI", msg)
        self._push_log_line(f"[TUI] {msg}")

    # ── actions ──
    def action_new_session(self):
        self._new_session()
        self._update_status()

    def action_clear_log(self):
        self.query_one("#chat-log", RichLog).clear()
        log_event("TUI", "chat log cleared")

    def action_save(self):
        if self.current_session:
            try:
                self.sm.save(self.current_session, self.sm.load(self.current_session))
                self._log_system("Session saved.")
                log_result("save", f"manual save sid={self.current_session} OK")
            except Exception as e:
                self._log_system(f"Save failed: {e}")
                log_event("SAVE", f"manual save FAILED sid={self.current_session}: {e}", level="ERROR")
            self._tail_logs()
            self._update_status()

    def action_help(self):
        self._log_system("Ctrl+N: new session | Ctrl+L: clear | Ctrl+Q: quit | Enter: send")


# ============================================================
#  ENTRY POINT
# ============================================================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="NEXUS Multi-Agent System")
    parser.add_argument("--session", help="Load existing session ID")
    parser.add_argument("--cli", action="store_true", help="Run in headless CLI mode")
    parser.add_argument("--prompt", help="One-shot prompt (CLI mode)")
    parser.add_argument("--web", action="store_true", help="Run the JARVIS web UI backend")
    parser.add_argument("--host", default=os.getenv("WEB_HOST", "127.0.0.1"), help="Web bind host")
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "8777")), help="Web port")
    args = parser.parse_args()

    log_event("SYSTEM", f"startup provider={CFG.provider} "
                        f"model={CFG.model if CFG.provider != 'ollama' else CFG.ollama_model} "
                        f"cli={args.cli} web={args.web} log={LOG_FILE}")
    print("WARNING: NEXUS is an unrestricted RESEARCH build — agents run REAL shell commands, "
          "execute code and browse the web with NO sandbox or guardrails. Research use only: "
          "run it on a machine/VM you can afford to break. You are responsible for what it does.")
    log_event("SYSTEM", "research warning shown: unrestricted tools, research use only")
    if args.web:
        import webui
        webui.serve(host=args.host, port=args.port, session_id=args.session)
    elif args.cli:
        asyncio.run(_cli_mode(args))
    else:
        NexusTUI().run()


async def _cli_mode(args):
    sm = SessionManager()
    sid = args.session or sm.create("cli")
    log_event("SYSTEM", f"cli start sid={sid} prompt={_short(args.prompt or '', 300)!r}")
    agent = NexusAgent(sid, sm, LLMProvider())
    prompt = args.prompt or input("NEXUS> ")
    log_event("SAVE", f"cli input sid={sid} text={_short(prompt, 500)!r}")
    result = await agent.run(prompt)
    log_result("agent", f"cli final sid={sid} len={len(result or '')}", _short(result or "", 2000))
    console.print(Panel(Markdown(result), title="NEXUS", border_style="cyan"))


if __name__ == "__main__":
    main()