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
    subagent_max_iterations: int = int(os.getenv("SUBAGENT_MAX_ITERATIONS", "100"))
    delegate_max_rounds: int = int(os.getenv("DELEGATE_MAX_ROUNDS", "100"))
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
        for d in sorted(self.root.iterdir(), reverse=True):
            if d.is_dir() and (d / "session.json").exists():
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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
            results = await loop.run_in_executor(
                None,
                lambda: list(DDGS().text(q, max_results=max_results, region=region, safesearch=safesearch))
            )
            return {"query": q, "results": results}
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
        return {"success": True, "url": url, "format": fmt, "meta": meta, "content": content}
    except Exception as e:
        return {"success": False, "url": url, "error": str(e)}


# ============================================================
#  TOOL REGISTRY & PARALLEL EXECUTOR
# ============================================================
TOOL_REGISTRY: Dict[str, Callable[..., Awaitable[dict]]] = {
    "exec.shell": tool_exec_shell,
    "exec.code": tool_exec_code,
    "web.search": tool_web_search,
    "web.fetch": tool_web_fetch,
    "memory.read": tool_memory_read,
    "memory.remember": tool_memory_remember,
    "memory.forget": tool_memory_forget,
    "memory.profile": tool_memory_profile,
}


async def execute_tool(name: str, params: dict, session_workspace: str = None) -> dict:
    """Execute a single tool by name."""
    fn = TOOL_REGISTRY.get(name)
    if not fn:
        result = {"success": False, "error": f"Unknown tool: {name}"}
        log_tool_call(name, params or {}, result, 0.0)
        log_result("tool", f"{name} FAIL unknown-tool", result, level="WARNING")
        return result
    log_event("TOOL", f"start {name}", extra={"params": params})
    t0 = time.time()
    try:
        if name == "exec.code":
            params = {**params, "session_workspace": session_workspace}
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


async def tool_parallel(calls: List[dict], session_workspace: str = None) -> dict:
    """Execute multiple tool calls concurrently via asyncio.gather."""
    log_event("TOOL", f"parallel start fanout={len(calls)}",
              extra={"tools": [c.get("tool") for c in calls]})
    async def _one(call: dict):
        t0 = time.time()
        name = call.get("tool")
        params = call.get("params", {})
        result = await execute_tool(name, params, session_workspace)
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

TOOL_SPEC_DELEGATE = {
    "type": "function",
    "function": {
        "name": "agent.delegate",
        "description": (
            "MANDATORY when 2+ independent workstreams exist — doing them yourself is a failure. "
            "Spawn worker subagents that run CONCURRENTLY, each with an isolated context. "
            "Build every brief with the 6-line framework from your system prompt "
            "(ROLE/GOAL/CONTEXT/METHOD/OUTPUT/TALK) — vague briefs get rejected. "
            "Workers can use exec/web tools but CANNOT delegate further. Their outputs "
            "come back as this tool's result — verify each against its OUTPUT contract, "
            "then synthesize the single final answer yourself."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "description": "Independent subtasks (max honored by server)",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Short task id, e.g. t1"},
                            "brief": {"type": "string",
                                      "description": "REQUIRED, non-empty: self-contained instructions "
                                                     "with the exact goal AND which tool to use "
                                                     "(worker sees nothing else). Empty briefs are rejected."},
                            "context": {"type": "string",
                                        "description": "Background facts the worker needs"},
                        },
                        "required": ["brief"],
                    },
                },
            },
            "required": ["tasks"],
        },
    },
}

# Leader sees everything including delegation. Workers get tools but no
# delegation (depth guard — enforced again in code), plus the round radio
# and joint plan so they can talk and coordinate with each other.
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
                TOOL_SPEC_WEB_SEARCH, TOOL_SPEC_WEB_FETCH, TOOL_SPEC_PARALLEL,
                TOOL_SPEC_SAY, TOOL_SPEC_PLAN, TOOL_SPEC_MEM_READ]
LEADER_TOOLS = [TOOL_SPEC_EXEC_SHELL, TOOL_SPEC_EXEC_CODE,
                TOOL_SPEC_WEB_SEARCH, TOOL_SPEC_WEB_FETCH, TOOL_SPEC_PARALLEL,
                TOOL_SPEC_DELEGATE,
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


MODEL_UNAVAILABLE = ("The model endpoint is temporarily failing on Ollama's side (cloud error). "
                     "Nothing is wrong with NEXUS — please send your message again in a moment.")


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
        while True:
            attempts += 1
            if self.cfg.provider == "ollama":
                out = await self._ollama(messages, tools)
            else:
                out = await self._openai(messages, tools)
            content = out.get("content", "") or ""
            if not _is_error_content(content) or attempts > retries:
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

    def snapshot_plan(self) -> List[dict]:
        return [dict(s) for s in self.plan]

    def say(self, frm: str, to: str, text: str) -> dict:
        text = _short((text or "").strip(), RADIO_MAX_LEN)
        if not text:
            return {"success": False, "error": "empty message — say something or skip radio"}
        if len(self.messages) >= RADIO_MAX_MSGS:
            return {"success": False, "error": "radio full (100 msgs); stop chatting and finish the task"}
        to = (to or "all").strip() or "all"
        self.seq += 1
        msg = {"seq": self.seq, "from": frm, "to": to, "text": text,
               "ts": datetime.now().strftime("%H:%M:%S")}
        self.messages.append(msg)
        log_event("RADIO", f"round={self.rid} #{msg['seq']} {frm}->{to}: {_short(text, 200)!r}")
        return {"success": True, "seq": self.seq}

    def plan_op(self, frm: str, action: str, step_id: str = "",
                title: str = "", note: str = "") -> dict:
        action = (action or "list").strip().lower()
        if action == "list":
            return {"success": True, "plan": self.snapshot_plan(), "version": self.plan_version}
        if action == "add":
            if len(self.plan) >= PLAN_MAX_STEPS:
                return {"success": False, "error": "plan full (100 steps max)"}
            sid = (step_id or f"s{len(self.plan) + 1}").strip() or f"s{len(self.plan) + 1}"
            if any(s["id"] == sid for s in self.plan):
                sid = f"s{len(self.plan) + 1}_{frm}"
            self.plan.append({"id": sid, "title": _short((title or "").strip(), 200) or "untitled step",
                              "status": "open", "owner": "", "note": ""})
            self.plan_version += 1
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
        self.on_event: Optional[Callable] = None  # callback for TUI (legacy single)
        self.listeners: List[Callable] = []  # extra fan-out targets (e.g. web UI bus)

    def _build_system_prompt(self) -> str:
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
        return base + ctx

    def add_listener(self, fn: Callable):
        """Attach another event consumer (web UI bus). Never raises."""
        try:
            if fn not in self.listeners:
                self.listeners.append(fn)
        except Exception:
            pass

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
        self.history.append({"role": "user", "content": user_input})
        self.sm.add_message(self.sid, "user", user_input)
        self._delegate_rounds = 0  # per-turn delegation budget

        final = ""
        for iteration in range(CFG.max_iterations):
            self._emit("iteration", {"n": iteration + 1, "max": CFG.max_iterations})
            log_event("AGENT", f"iteration {iteration + 1}/{CFG.max_iterations} sid={self.sid}")
            llm_out = await self.llm.chat(self.history, tools=LEADER_TOOLS)
            content = llm_out.get("content", "") or ""
            tool_calls = llm_out.get("tool_calls", []) or []
            if llm_out.get("thinking"):
                self._emit("thinking", {"who": "leader", "text": llm_out["thinking"]})

            # If no tool calls, this is the single final answer for the turn
            if not tool_calls:
                if _is_error_content(content):
                    # Model endpoint still failing after retries: NEVER show the
                    # raw "[Ollama error] ... (ref:...)" text as the answer.
                    log_event("AGENT", f"final UNAVAILABLE sid={self.sid} iter={iteration + 1}",
                              level="WARNING")
                    final = MODEL_UNAVAILABLE
                else:
                    final = content
                    log_event("AGENT", f"final sid={self.sid} iter={iteration + 1} len={len(content)}")
                    log_result("agent", f"final answer sid={self.sid} iter={iteration + 1} len={len(content)}",
                               _short(content, 2000))
                self.history.append({"role": "assistant", "content": final})
                self.sm.add_message(self.sid, "assistant", final)
                self._emit("final", {"content": final})
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
        return final

    async def _exec_leader_call(self, tc: dict):
        """Execute one leader tool call. agent.delegate fans out to workers;
        everything else runs locally. Returns (params, result, elapsed)."""
        params = json.loads(tc["arguments"]) if isinstance(tc["arguments"], str) else (tc["arguments"] or {})
        self._emit("tool_start", {"call_id": tc.get("id"), "name": tc.get("name"),
                                  "params": params, "who": "leader"})
        t0 = time.time()
        if tc.get("name") == "agent.delegate":
            result = await self._delegate(params)
        else:
            result = await execute_tool(tc.get("name", ""), params, self.workspace)
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
            tasks[i].setdefault("id", f"t{i + 1}")
        # Hard validation: empty briefs make workers hallucinate meta-work
        # ("create initial plan") instead of doing the job. Bounce back so
        # the leader retries WITH real briefs.
        bad = [t.get("id", f"t{i + 1}") for i, t in enumerate(tasks)
               if not (t.get("brief") or "").strip()]
        if bad:
            return {"success": False,
                    "error": f"tasks {bad} have empty 'brief'. Re-call with a non-empty, "
                             f"self-contained 'brief' per task (exact goal + which tool to use)."}
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
        self._emit("delegate_start", {"round": rid, "count": len(tasks), "ids": ids})
        log_event("AGENT", f"delegate start sid={self.sid} round={rid} workers={ids}")
        t0 = time.time()
        results = await asyncio.gather(*[self._run_worker(t, rnd) for t in tasks])
        elapsed = round(time.time() - t0, 3)
        n_ok = sum(1 for r in results if r.get("success"))
        out = {"success": True, "round": rid, "results": results,
               "plan": rnd.snapshot_plan(),
               "elapsed": elapsed, "ok": n_ok, "failed": len(results) - n_ok}
        # Persist ONE entry for the whole fan-out (worker traces stay in the log).
        self.sm.add_tool_call(self.sid, "agent.delegate",
                              {"tasks": [{k: t.get(k) for k in ("id", "brief")} for t in tasks]},
                              out, elapsed)
        log_event("AGENT", f"delegate done sid={self.sid} round={rid} ok={n_ok}/{len(results)} "
                           f"elapsed={elapsed:.1f}s radio={rnd.seq} plan_v{rnd.plan_version}")
        log_result("agent", f"delegate done sid={self.sid} round={rid} ok={n_ok}/{len(results)}", out)
        self._emit("delegate_done", {"round": rid, "ok": n_ok, "failed": len(results) - n_ok,
                                     "elapsed": elapsed, "plan": rnd.snapshot_plan()})
        return out

    async def _run_worker(self, task: dict, rnd: DelegateRound) -> dict:
        wid = task.get("id", "t?")
        brief = task.get("brief", "")
        self._emit("subagent_start", {"round": rnd.rid, "id": wid, "brief": brief})
        log_event("SUBAGENT", f"start sid={self.sid} round={rnd.rid} id={wid} brief={_short(brief, 300)!r}")
        t0 = time.time()
        try:
            worker = NexusWorker(wid, brief, task.get("context", "") or "",
                                 self.sid, self.sm, self.llm, self.workspace,
                                 rnd, self._emit)
            res = await worker.run()
        except Exception as e:
            res = {"text": "", "tools_used": 0, "iterations": 0, "success": False,
                   "error": str(e), "radio_seen": 0, "radio_sent": 0}
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
                 emit: Optional[Callable] = None):
        self.wid = wid
        self.brief = brief or ""
        self.sid = sid
        self.sm = sm
        self.llm = llm
        self.workspace = workspace
        self.rnd = rnd
        self.emit = emit or (lambda e, d: None)
        self.tools_used = 0
        self.radio_seen = 0
        self.radio_sent = 0
        self.last_seq = 0
        self.last_version = -1  # -1 forces the opening plan snapshot on step 1
        peers = [w for w in (rnd.worker_ids if rnd else []) if w != wid]
        self.history: List[dict] = [
            {"role": "system", "content":
                "You are a NEXUS worker subagent. Complete ONLY the task below with tools. "
                "Be concise: your last message must be the self-contained result (facts, "
                "numbers, file paths — everything the leader needs, since it never sees "
                "your tool outputs directly). Do not ask questions. There is no delegation tool."
                + (f" You share round {rnd.rid} with peer worker(s) {peers}: coordinate with "
                     "agent.say (to='all' or a peer id — they read it before their next step) "
                     "and the joint agent.plan (add/claim/done/note/list). Claim a plan step "
                     "before doing it so peers don't duplicate work, and READ incoming radio "
                     "and plan updates instead of redoing finished work. RADIO DISCIPLINE (mandatory): "
                     "1) first action: broadcast who you are and which step you claim; "
                     "2) if a peer addresses YOU by id on the radio, answer them directly (to=their id) "
                     "before you finish; 3) broadcast your key finding + plan done when finished. "
                     "A worker that never speaks is a failed worker. "
                     "TOOL DISCIPLINE: match shell_type to your syntax (PowerShell code → pwsh; "
                     "cmd syntax → cmd.exe) — if stdout just echoes your command, switch shells "
                     "immediately. Prefer fast checks (ping -n 4, fetch timeout 20); never "
                     "tracert -h 30; never re-fetch a URL that returned junk. STOP the moment "
                     "your brief is fulfilled — report partial results instead of burning "
                     "iterations."
                   if rnd and peers else "")},
            {"role": "user", "content":
                f"TASK {wid}: {brief}" + (f"\nCONTEXT: {context}" if context else "")},
        ]

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

    async def run(self) -> dict:
        iterations = 0
        text = ""
        # Refuse empty briefs instantly: no LLM calls wasted on hallucinated meta-work.
        if not self.brief.strip():
            return {"text": "", "tools_used": 0, "iterations": 0, "success": False,
                    "error": "empty brief — leader must supply a self-contained task",
                    "radio_seen": 0}
        for i in range(CFG.subagent_max_iterations):
            iterations = i + 1
            self._drain_radio()
            out = await self.llm.chat(self.history, tools=WORKER_TOOLS)
            content = out.get("content", "") or ""
            calls = out.get("tool_calls", []) or []
            if out.get("thinking"):
                self.emit("thinking", {"who": self.wid, "text": out["thinking"],
                                       "round": self.rnd.rid if self.rnd else ""})
            if not calls:
                text = content
                self.history.append({"role": "assistant", "content": content})
                break
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
                    # Depth guard: workers cannot spawn workers.
                    res = {"success": False, "error": "workers cannot delegate; do the task with exec/web tools"}
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
                    res = await execute_tool(name, params, self.workspace)
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
        else:
            log_event("SUBAGENT", f"id={self.wid} sid={self.sid} hit iteration budget "
                                  f"({CFG.subagent_max_iterations})", level="WARNING")
        if _is_error_content(text):
            # Model endpoint failed: report failure so the leader knows this
            # output is unusable (never pass raw error text up as a result).
            return {"text": "", "tools_used": self.tools_used,
                    "iterations": iterations, "success": False,
                    "error": "model endpoint temporarily failing (retry delegation)",
                    "radio_seen": self.radio_seen, "radio_sent": self.radio_sent}
        if not text:
            # Ended on tool calls (budget hit): summarize what was gathered.
            text = f"(budget exhausted after {iterations} iterations, {self.tools_used} tool calls; " \
                   f"partial trace in nexus.log)"
            success = False
        else:
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
    {"cmd": "/open", "usage": "/open <id…>", "desc": "Switch to an old conversation"},
    {"cmd": "/delete", "usage": "/delete <id…>", "desc": "Delete a conversation (not the active one)"},
    {"cmd": "/models", "usage": "/models", "desc": "List available models"},
    {"cmd": "/model", "usage": "/model <name>", "desc": "Switch model live"},
    {"cmd": "/thinking", "usage": "/thinking [low|medium|high]", "desc": "Show/set reasoning effort"},
    {"cmd": "/memory", "usage": "/memory", "desc": "Show what NEXUS remembers about you"},
    {"cmd": "/clear", "usage": "/clear", "desc": "Clear the screen (local)"},
    {"cmd": "/help", "usage": "/help", "desc": "This list"},
]


def update_env_file(key: str, value: str):
    """Persist one KEY=value into .env (replace or append). Never raises."""
    try:
        p = Path(__file__).parent / ".env"
        lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
        out, done = [], False
        for ln in lines:
            if ln.strip().startswith(key + "=") and not done:
                out.append(f"{key}={value}")
                done = True
            else:
                out.append(ln)
        if not done:
            out.append(f"{key}={value}")
        p.write_text("\n".join(out) + "\n", encoding="utf-8")
    except Exception as e:
        log_event("SYSTEM", f"env persist {key} failed: {e}", level="WARNING")


def _match_session(sm: SessionManager, prefix: str):
    """Resolve an id prefix to one session. Returns (sid, error_reply)."""
    prefix = (prefix or "").strip()
    if not prefix:
        return None, "Usage: `/open <id…>` — see `/sessions`."
    cands = [s for s in sm.list_sessions() if s.get("id", "").startswith(prefix)]
    if not cands:
        return None, f"No session starts with `{prefix}`."
    if len(cands) > 1:
        lines = "\n".join(f"- `{s['id']}` — {s.get('title', '')}" for s in cands[:8])
        return None, f"Ambiguous — matches {len(cands)}:\n{lines}\nBe more specific."
    return cands[0]["id"], ""


def _ollama_models() -> List[str]:
    try:
        import httpx
        host = CFG.ollama_host.rstrip("/")
        r = httpx.get(f"{host}/api/tags", timeout=6)
        return sorted(m.get("name", "") for m in r.json().get("models", []) if m.get("name"))
    except Exception:
        return []


async def handle_slash(text: str, ctx: dict) -> dict:
    """Parse + execute a slash command. ctx: {sm, sid}. Never raises."""
    try:
        parts = (text or "").strip().split()
        cmd = parts[0].lower() if parts else ""
        arg = " ".join(parts[1:]).strip()
        sm: SessionManager = ctx.get("sm")
        sid: str = ctx.get("sid", "")

        if cmd == "/new":
            nsid = sm.create("interactive")
            log_event("TUI", f"slash /new -> {nsid}")
            return {"handled": True, "reply": f"New session `{nsid}`.", "action": "new", "sid": nsid}

        if cmd == "/sessions":
            sessions = sm.list_sessions()[:10]
            if not sessions:
                return {"handled": True, "reply": "No conversations yet.", "action": None}
            lines = []
            for s in sessions:
                mark = " **← active**" if s.get("id") == sid else ""
                msgs = s.get("messages", [])
                first = next((m.get("content", "") for m in msgs if m.get("role") == "user"), "")
                lines.append(f"- `{s['id']}`{mark} — {s.get('title', '')} "
                             f"({len(msgs)} msgs) {('“' + _short(first, 60) + '”') if first else ''}")
            return {"handled": True, "reply": "Recent conversations:\n" + "\n".join(lines), "action": None}

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
                return {"handled": True, "reply": err, "action": None}
            if nsid == sid:
                return {"handled": True,
                        "reply": "Can't delete the active session — `/open` another one first.",
                        "action": None}
            import shutil
            shutil.rmtree(sm.root / nsid, ignore_errors=True)
            log_event("TUI", f"slash /delete {nsid}")
            return {"handled": True, "reply": f"Deleted `{nsid}`.", "action": None}

        if cmd == "/models":
            if CFG.provider == "ollama":
                names = _ollama_models()
                cur = CFG.ollama_model
                if not names:
                    return {"handled": True, "reply": f"Model list unreachable — current: `{cur}`.",
                            "action": None}
                lines = "\n".join(f"- `{n}`" + (" **← active**" if n == cur else "") for n in names)
                return {"handled": True, "reply": f"Ollama models (`/model <name>`):\n{lines}",
                        "action": None}
            return {"handled": True, "reply": f"Provider `{CFG.provider}` — current model `{CFG.model}`.\n"
                                              f"Switch with `/model <name>`.", "action": None}

        if cmd == "/model":
            if not arg:
                cur = CFG.ollama_model if CFG.provider == "ollama" else CFG.model
                return {"handled": True, "reply": f"Current model: `{cur}`. Usage: `/model <name>` "
                                                  f"(see `/models`).", "action": None}
            if CFG.provider == "ollama":
                names = _ollama_models()
                if names and arg not in names:
                    close = [n for n in names if arg.lower() in n.lower()]
                    hint = f"\nDid you mean: {', '.join(f'`{c}`' for c in close[:3])}?" if close else ""
                    return {"handled": True,
                            "reply": f"Unknown model `{arg}`.{hint}\nSee `/models`.", "action": None}
                CFG.ollama_model = arg
                update_env_file("OLLAMA_MODEL", arg)
            else:
                CFG.model = arg
                update_env_file("LLM_MODEL", arg)
            log_event("SYSTEM", f"slash /model -> {arg}")
            return {"handled": True, "reply": f"Model switched to `{arg}` (saved, live now).",
                    "action": None}

        if cmd == "/thinking":
            if not arg:
                return {"handled": True,
                        "reply": f"Reasoning effort: **{CFG.think_effort}** "
                                 f"(tokens {CFG.max_tokens}, up to {CFG.max_iterations} iters).\n"
                                 f"Set with `/thinking low|medium|high`.", "action": None}
            lvl = arg.lower()
            if lvl not in ("low", "medium", "high"):
                return {"handled": True, "reply": "Usage: `/thinking low|medium|high`.", "action": None}
            CFG.think_effort = lvl
            update_env_file("THINK_EFFORT", lvl)
            log_event("SYSTEM", f"slash /thinking -> {lvl}")
            note = " (Ollama native depth)" if CFG.provider == "ollama" else " (stored; applies to Ollama)"
            return {"handled": True, "reply": f"Thinking effort → **{lvl}**{note}.", "action": None}

        if cmd == "/memory":
            data = MEM.read("")
            prof = data.get("profile", {})
            facts = data.get("facts", [])
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

        if cmd == "/help":
            lines = "\n".join(f"- `{c['usage']}` — {c['desc']}" for c in SLASH_COMMANDS)
            return {"handled": True, "reply": "Commands:\n" + lines, "action": None}

        if cmd == "/clear":
            return {"handled": True, "reply": "", "action": "clear"}

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
                    self._new_session()
                elif action == "switch":
                    n = self._attach_session(res["sid"])
                    self._log_system(f"Opened {res['sid']} ({n} messages restored)")
                elif action == "clear":
                    log.clear()
                if res.get("reply"):
                    log.write(Panel(Markdown(res["reply"]), title="CMD",
                                    subtitle=text.split()[0], border_style="amber"))
                self._refresh_stats()
                self._update_status()
                self._tail_logs()
                return
            # unknown /command falls through to the agent as plain text
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