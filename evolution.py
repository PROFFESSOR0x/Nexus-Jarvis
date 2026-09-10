#!/usr/bin/env python3
"""
NEXUS self-evolution engine — the agent behind the agent.

Watches real runs (session tool_calls + nexus.log), indexes the actual repo
with file/symbol/line/hash/anchor records, decides per observation whether it
is a durable FACT (repo memory) or a BEHAVIORAL RULE (prompt evolution), and
applies it with full version history. Nothing here restricts the main agent;
every change records *what* changed and *why* (evidence).

Store layout under EVODIR (~/.nexus/evolution/):
  prompts/current.md        working prompt = base system_prompt.txt + active rules
  prompts/vNNN.md           full snapshots (rollback targets)
  prompts/rules.jsonl       rule records {v, text, reason, evidence, active}
  evolution.jsonl           version transitions {seed,rule,rollback,rebase}
  cycles.jsonl              per-cycle run records (observations -> decisions)
  memory.jsonl              repo facts (file/symbol/lines/anchor/hash/content)
  changes.jsonl             file before/after records with diffs
  failures.jsonl            observed failure patterns with evidence
  files.json                latest repo index
  snapshots/YYYYMMDD_HHMMSS.json   content snapshots (hashes + text, last 5)

Only stdlib is imported here. The LLM is passed in (duck-typed .chat()).
"""

import ast
import difflib
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

EVODIR = Path(os.getenv("EVOLUTION_DIR", Path.home() / ".nexus" / "evolution"))
PROMPT_KEEP_VERSIONS = 50
SNAPSHOT_KEEP = 5
MAX_OBSERVATIONS_PER_CYCLE = 12


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha1(s: str, n: int = 8) -> str:
    return hashlib.sha1(s.encode("utf-8", "replace")).hexdigest()[:n]


def _read_jsonl(p: Path):
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _append_jsonl(p: Path, obj: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def evo_paths(base: Path = EVODIR) -> dict:
    return {
        "root": base,
        "prompts": base / "prompts",
        "current": base / "prompts" / "current.md",
        "rules": base / "prompts" / "rules.jsonl",
        "evolution": base / "evolution.jsonl",
        "cycles": base / "cycles.jsonl",
        "memory": base / "memory.jsonl",
        "changes": base / "changes.jsonl",
        "failures": base / "failures.jsonl",
        "files": base / "files.json",
        "snapshots": base / "snapshots",
    }


SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", ".mypy_cache", ".pytest_cache"}
SKIP_SUFFIX = {".tmp", ".log", ".json.tmp"}
MAX_INDEX_BYTES = 200_000


def _py_symbols(text: str):
    try:
        tree = ast.parse(text)
    except Exception:
        return [], []
    syms, imports = [], []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            syms.append({"name": node.name,
                         "kind": "class" if isinstance(node, ast.ClassDef) else "def",
                         "line": node.lineno,
                         "end": getattr(node, "end_lineno", node.lineno)})
        elif isinstance(node, ast.Import):
            imports += [a.asname or a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module.split(".")[0])
    return syms, sorted(set(imports)), []  # (symbols, imports, exports)


def _js_symbols(text: str):
    syms, imports, exports = [], [], []
    for m in re.finditer(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", text, re.M):
        syms.append({"name": m.group(1), "kind": "def", "line": text[:m.start()].count("\n") + 1, "end": 0})
    for m in re.finditer(r"^\s*class\s+([A-Za-z_$][\w$]*)", text, re.M):
        syms.append({"name": m.group(1), "kind": "class", "line": text[:m.start()].count("\n") + 1, "end": 0})
    for m in re.finditer(r"^\s*(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(", text, re.M):
        syms.append({"name": m.group(1), "kind": "def", "line": text[:m.start()].count("\n") + 1, "end": 0})
    for m in re.finditer(r"^\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", text, re.M):
        if m.group(1) not in ("if", "for", "while", "switch", "catch"):
            syms.append({"name": m.group(1), "kind": "def", "line": text[:m.start()].count("\n") + 1, "end": 0})
    seen, out = set(), []
    for s in syms:
        if s["name"] not in seen:
            seen.add(s["name"])
            out.append(s)
    for m in re.finditer(r"^\s*import\s+(?:[^'\"]+?\s+from\s+)?['\"]([^'\"]+)['\"]", text, re.M):
        imports.append(m.group(1).split("/")[-1][:60])
    for m in re.finditer(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)", text):
        imports.append(m.group(1).split("/")[-1][:60])
    for m in re.finditer(r"^\s*export\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)", text, re.M):
        exports.append(m.group(1))
    for m in re.finditer(r"module\.exports\s*=\s*\{([^}]*)\}", text):
        exports += [e.strip().split(":")[0].strip() for e in m.group(1).split(",") if e.strip()][:20]
    return out, sorted(set(imports))[:30], sorted(set(exports))[:30]


def index_repo(root: Path) -> dict:
    """Map every source file -> {language, lines, hash, symbols[]}. Never raises."""
    root = Path(root)
    index = {}
    try:
        paths = sorted(p for p in root.rglob("*") if p.is_file())
    except Exception:
        return index
    for p in paths:
        try:
            rel = p.relative_to(root).as_posix()
        except Exception:
            continue
        if any(part in SKIP_DIRS for part in rel.split("/")):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        try:
            if p.stat().st_size > MAX_INDEX_BYTES:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        suf = p.suffix.lower()
        if suf == ".py":
            lang = "python"
            syms, imports, exports = _py_symbols(text)
        elif suf in (".js", ".jsx", ".ts", ".tsx"):
            lang = "javascript"
            syms, imports, exports = _js_symbols(text)
        elif suf in (".md", ".txt", ".json", ".html", ".css", ".env", ".example", ".gitignore"):
            lang, syms, imports, exports = "text", [], [], []
        else:
            lang, syms, imports, exports = "other", [], [], []
        index[rel] = {"language": lang, "lines": text.count("\n") + 1,
                      "hash": sha1(text), "symbols": syms,
                      "imports": imports, "exports": exports}
    return index


def git_info(repo) -> dict:
    """HEAD + dirty files + recent log. Pure observation, never raises, 10s cap."""
    info = {"head": "", "dirty": [], "log": []}
    try:
        import subprocess
        repo = str(repo)
        r = subprocess.run(["git", "-C", repo, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            info["head"] = r.stdout.strip()
        r = subprocess.run(["git", "-C", repo, "status", "--porcelain"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            info["dirty"] = [ln.strip()[:120] for ln in r.stdout.splitlines() if ln.strip()][:30]
        r = subprocess.run(["git", "-C", repo, "log", "--oneline", "-5"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            info["log"] = [ln.strip()[:120] for ln in r.stdout.splitlines() if ln.strip()]
    except Exception:
        pass
    return info


def take_snapshot(repo: Path, base: Path = EVODIR) -> dict:
    """Content snapshot (hashes + text for small files). Keeps last SNAPSHOT_KEEP."""
    P = evo_paths(base)
    P["snapshots"].mkdir(parents=True, exist_ok=True)
    repo = Path(repo)
    files = {}
    for rel, meta in index_repo(repo).items():
        entry = {"hash": meta["hash"], "lines": meta["lines"], "language": meta["language"],
                 "symbols": meta["symbols"]}
        p = repo / rel
        try:
            if p.stat().st_size <= MAX_INDEX_BYTES:
                entry["content"] = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            pass
        files[rel] = entry
    snap = {"ts": utcnow(), "repo": str(repo), "git": git_info(repo), "files": files}
    (P["snapshots"] / (datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f") + ".json")).write_text(
        json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    snaps = sorted(P["snapshots"].glob("*.json"))
    for old in snaps[:-SNAPSHOT_KEEP]:
        try:
            old.unlink()
        except Exception:
            pass
    try:
        (P["root"] / "files.json").write_text(
            json.dumps({k: {kk: v[kk] for kk in ("language", "lines", "hash", "symbols")}
                        for k, v in files.items()}, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return snap


def _latest_snapshot(base: Path):
    P = evo_paths(base)
    snaps = sorted(P["snapshots"].glob("*.json"))
    if len(snaps) < 2:
        return None, None
    try:
        old = json.loads(snaps[-2].read_text(encoding="utf-8"))
        new = json.loads(snaps[-1].read_text(encoding="utf-8"))
        return old, new
    except Exception:
        return None, None


def diff_snapshots(old: dict, new: dict, max_diff_chars: int = 4000):
    """Before/after records with unified diff excerpts. Never raises."""
    changes = []
    try:
        of, nf = old.get("files", {}), new.get("files", {})
        for path in sorted(set(of) | set(nf)):
            o, n = of.get(path), nf.get(path)
            if o is None:
                changes.append({"path": path, "status": "added",
                                "after": {"hash": n["hash"], "lines": n["lines"]}})
            elif n is None:
                changes.append({"path": path, "status": "removed",
                                "before": {"hash": o["hash"], "lines": o["lines"]}})
            elif o["hash"] != n["hash"] and o.get("content") is not None and n.get("content") is not None:
                diff = "".join(difflib.unified_diff(
                    o["content"].splitlines(True), n["content"].splitlines(True),
                    fromfile="before", tofile="after"))[:max_diff_chars]
                changes.append({"path": path, "status": "modified",
                                "before": {"hash": o["hash"], "lines": o["lines"]},
                                "after": {"hash": n["hash"], "lines": n["lines"]},
                                "diff": diff})
            elif o["hash"] != n["hash"]:
                changes.append({"path": path, "status": "modified",
                                "before": {"hash": o["hash"], "lines": o["lines"]},
                                "after": {"hash": n["hash"], "lines": n["lines"]}})
    except Exception:
        pass
    return changes


def resolve_anchor(repo: Path, rel: str, symbol=None, anchor=None, window: int = 12):
    """Locate file/symbol/anchor NOW -> {lines, hash, excerpt, found}. Survives line drift."""
    repo = Path(repo)
    try:
        text = (repo / rel).read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {"found": False}
    lines = text.splitlines()
    idx = None
    if symbol:
        syms, _, _ = (_py_symbols(text) if rel.endswith(".py") else _js_symbols(text)) \
            if rel.endswith((".py", ".js", ".jsx", ".ts", ".tsx")) else ([], [], [])
        for s in syms:
            if s["name"] == symbol:
                idx = s["line"] - 1
                break
    if idx is None and anchor:
        a = anchor.strip().splitlines()[0][:120]
        for i, ln in enumerate(lines):
            if a and a in ln:
                idx = i
                break
    if idx is None:
        return {"found": False}
    lo, hi = max(0, idx - 2), min(len(lines), idx + window)
    excerpt = "\n".join(lines[lo:hi])
    return {"found": True, "lines": [lo + 1, hi], "hash": sha1(excerpt),
            "excerpt": excerpt[:1500]}


# ============================================================
#  EVIDENCE MINERS — patterns from real runs (sessions + nexus.log)
# ============================================================

def _norm_params(params) -> str:
    try:
        d = dict(params or {})
        d.pop("timeout", None)
        return json.dumps(d, sort_keys=True, ensure_ascii=False)[:300]
    except Exception:
        return ""


def mine_sessions(sessions_root: Path, limit: int = 8):
    """Observations from stored tool_calls. Returns list of observation dicts."""
    obs = []
    try:
        dirs = sorted([d for d in Path(sessions_root).iterdir() if d.is_dir()],
                      key=lambda d: d.stat().st_mtime, reverse=True)[:limit]
    except Exception:
        return obs
    failkey, seen_seq = {}, {}
    for d in dirs:
        try:
            meta = json.loads((d / "session.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        sid = meta.get("id", d.name)
        prev = None
        for tc in meta.get("tool_calls", []) or []:
            name = tc.get("name", "?")
            res = tc.get("result", {}) or {}
            ok = bool(res.get("success", False))
            err = str(res.get("error", "") or "")[:200]
            if not ok:
                key = (name, err[:80])
                failkey[key] = failkey.get(key, 0) + 1
                if failkey[key] == 2:
                    obs.append({"kind": "repeated_failure", "count": 2,
                                "detail": f"tool {name} failed 2+ times: {err[:150]}",
                                "evidence": [{"session": sid, "tool": name, "error": err[:150]}]})
            cur = (name, _norm_params(tc.get("params")))
            if prev == cur and name not in ("agent.say", "agent.plan"):
                k = ("retry", name, cur[1][:80])
                seen_seq[k] = seen_seq.get(k, 0) + 1
                if seen_seq[k] == 2:
                    obs.append({"kind": "retry_loop", "count": 2,
                                "detail": f"same {name} call repeated back-to-back (params identical)",
                                "evidence": [{"session": sid, "tool": name}]})
            prev = cur
            if "Timeout" in err:
                obs.append({"kind": "timeout", "count": 1,
                            "detail": f"{name} timed out (params: {_norm_params(tc.get('params'))[:120]})",
                            "evidence": [{"session": sid, "tool": name}]})
    return obs


LOG_PATTERNS = [
    ("budget_exhausted", re.compile(r"budget exhausted")),
    ("empty_brief", re.compile(r"empty .brief", re.I)),
    ("emit_failed", re.compile(r"emit .* failed")),
    ("worker_silent", re.compile(r"radio_seen=0")),
    ("delegate_round", re.compile(r"delegate done .*?ok=(\d+)/(\d+)")),
    ("model_error", re.compile(r"\[(LLM error|Ollama error)\]")),
]


def mine_log(log_path: Path, max_lines: int = 2000):
    obs = []
    try:
        lines = Path(log_path).read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
    except Exception:
        return obs
    counts, examples = {}, {}
    rounds_ok, rounds_n = 0, 0
    for ln in lines:
        for kind, rx in LOG_PATTERNS:
            m = rx.search(ln)
            if not m:
                continue
            if kind == "delegate_round":
                try:
                    rounds_ok += int(m.group(1))
                    rounds_n += int(m.group(2))
                except Exception:
                    pass
                continue
            counts[kind] = counts.get(kind, 0) + 1
            examples.setdefault(kind, []).append(ln[:220])
    for kind, n in counts.items():
        if kind == "worker_silent":
            continue  # noisy alone; only meaningful per-round, checked below
        obs.append({"kind": f"log_{kind}", "count": n,
                    "detail": f"{kind.replace('_', ' ')} seen {n}x in recent logs",
                    "evidence": examples.get(kind, [])[:2]})
    if rounds_n:
        obs.append({"kind": "delegate_yield", "count": rounds_n,
                    "detail": f"workers ok {rounds_ok}/{rounds_n} across recent rounds",
                    "evidence": []})
    return obs


# ============================================================
#  EVOLUTION AGENT — the judge: fact vs behavioral rule vs noise
# ============================================================

JUDGE_SYSTEM = """You are the Evolution Judge inside the NEXUS self-evolving agent system.
You receive OBSERVATIONS mined from real runs (tool failures, retries, timeouts,
silent workers, log patterns) plus repo context. For EACH observation decide:

- "fact": a stable truth about THIS repository (file/role/convention) worth
  remembering with file+symbol+line evidence. Give: text (<=200 chars),
  file (repo-relative path), symbol (function/class or ""), anchor (one exact
  source line, or "").
- "rule": a BEHAVIORAL fix for a REPEATED failure pattern (it happened 2+ times
  or wastes real budget). Give: text = one imperative prompt rule (<=200 chars),
  reason (why, <=150 chars), evidence (which observations).
- "none": one-off, noise, external flake (e.g. single cloud 500), or already
  covered by an existing rule below. Give: reason.

Rules:
- NEVER propose a rule for a single transient cloud/infra error.
- behavior_defect observations carry occurrence counts: 3+ (high impact) strongly
  favors a rule verdict; single occurrences favor none unless the waste is severe.
- NEVER propose more than 3 rules per cycle; prefer none over weak rules.
- Facts must name a real file from the repo index. No invented paths.
- Output STRICT JSON only: {"decisions":[{"id":0,"verdict":"fact|rule|none",
  "text":"...","reason":"...","file":"","symbol":"","anchor":"","evidence":[]}]}.
  Match each decision id to the observation id given."""


async def judge(llm, observations, repo_index, existing_rules, session_note=""):
    """One LLM call for the whole batch. Returns decisions list. Never raises."""
    if not observations:
        return []
    files = [f"{p} ({m['language']},{m['lines']}L)" for p, m in
             sorted(repo_index.items(), key=lambda kv: -kv[1]["lines"])[:25]]
    obs_txt = "\n".join(
        f"[{o.get('id', i)}] {o.get('kind')}: {o.get('detail','')[:300]}"
        for i, o in enumerate(observations[:MAX_OBSERVATIONS_PER_CYCLE]))
    user = (f"REPO FILES (top):\n" + "\n".join(files) +
            f"\n\nEXISTING EVOLVED RULES (do not duplicate):\n" +
            ("\n".join(f"- {r}" for r in existing_rules) if existing_rules else "(none)") +
            f"\n\nOBSERVATIONS:\n{obs_txt}\n" +
            (f"\nSESSION CONTEXT: {session_note[:400]}\n" if session_note else "") +
            "\nDecide fact|rule|none per observation. STRICT JSON only.")
    try:
        out = await llm.chat([{"role": "system", "content": JUDGE_SYSTEM},
                              {"role": "user", "content": user}])
        txt = (out.get("content") or "").strip()
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt).strip()
        data = json.loads(txt)
        decs = data.get("decisions", []) if isinstance(data, dict) else []
        clean = []
        for d in decs:
            if not isinstance(d, dict):
                continue
            v = str(d.get("verdict", "none")).lower()
            if v not in ("fact", "rule", "none"):
                v = "none"
            clean.append({"id": d.get("id"), "verdict": v,
                          "text": str(d.get("text", ""))[:300],
                          "reason": str(d.get("reason", ""))[:300],
                          "file": str(d.get("file", "") or ""),
                          "symbol": str(d.get("symbol", "") or ""),
                          "anchor": str(d.get("anchor", "") or "")[:160],
                          "evidence": d.get("evidence", []) or []})
        return clean
    except Exception as e:
        return [{"id": None, "verdict": "none", "text": "", "reason": f"judge failed: {e}",
                 "file": "", "symbol": "", "anchor": "", "evidence": []}]


# ============================================================
#  APPLY — facts to repo memory, rules to versioned prompts
# ============================================================

def _active_rules(base: Path):
    return [r for r in _read_jsonl(evo_paths(base)["rules"]) if r.get("active")]


def _render_prompt(base_text: str, rules) -> str:
    txt = base_text.rstrip() + "\n"
    if rules:
        txt += "\n\n# ============================================================\n"
        txt += "#  LEARNED RULES (auto-evolved — do not edit by hand)\n"
        txt += "# ============================================================\n"
        for r in rules:
            txt += f"\n## Rule {r.get('v')} ({r.get('ts', '')[:10]}): {r.get('reason', '')[:140]}\n"
            txt += f"- {r.get('text', '')}\n"
    return txt


def _next_version(base: Path) -> str:
    P = evo_paths(base)
    mx = 0
    if P["prompts"].exists():
        for f in P["prompts"].glob("v*.md"):
            try:
                mx = max(mx, int(f.stem[1:]))
            except Exception:
                pass
    return f"v{mx + 1:03d}"


def ensure_seeded(base_text: str, base: Path = EVODIR):
    """First run: v001 = current base prompt. Returns version string."""
    P = evo_paths(base)
    P["prompts"].mkdir(parents=True, exist_ok=True)
    if (P["current"]).exists():
        evo = _read_jsonl(P["evolution"])
        cur = next((e for e in reversed(evo) if e.get("kind") in ("seed", "rule", "rollback", "rebase")), None)
        return cur.get("to", "v001") if cur else "v001"
    bh = sha1(base_text)
    (P["prompts"] / "v001.md").write_text(base_text, encoding="utf-8")
    P["current"].write_text(base_text, encoding="utf-8")
    _append_jsonl(P["evolution"], {"ts": utcnow(), "kind": "seed", "from": None,
                                   "to": "v001", "base_hash": bh,
                                   "reason": "initial snapshot of system_prompt.txt"})
    return "v001"


def current_version(base: Path = EVODIR) -> str:
    P = evo_paths(base)
    evo = _read_jsonl(P["evolution"])
    cur = next((e for e in reversed(evo) if e.get("kind") in ("seed", "rule", "rollback", "rebase")), None)
    return cur.get("to", "v001") if cur else "v001"


def active_prompt_text(base_text: str, base: Path = EVODIR) -> str:
    """Prompt the main agent actually loads: evolved layer over base.

    Auto-rebases (new version) if system_prompt.txt changed since seeding.
    Never raises — falls back to base text."""
    try:
        P = evo_paths(base)
        if not P["current"].exists():
            return base_text
        bh = sha1(base_text)
        evo = _read_jsonl(P["evolution"])
        seed = next((e for e in evo if e.get("kind") == "seed"), None)
        last_rebase = next((e for e in reversed(evo) if e.get("kind") == "rebase"), None)
        known = (last_rebase or seed or {}).get("base_hash", (seed or {}).get("base_hash"))
        if known and known != bh:
            rules = _active_rules(base)
            v = _next_version(base)
            rendered = _render_prompt(base_text, rules)
            (P["prompts"] / f"{v}.md").write_text(rendered, encoding="utf-8")
            P["current"].write_text(rendered, encoding="utf-8")
            _append_jsonl(P["evolution"], {"ts": utcnow(), "kind": "rebase", "from": current_version(base),
                                           "to": v, "base_hash": bh,
                                           "reason": "system_prompt.txt changed; rules re-applied"})
            return rendered
        return P["current"].read_text(encoding="utf-8")
    except Exception:
        return base_text


def apply_fact(repo: Path, decision: dict, task_ref: str, base: Path = EVODIR):
    P = evo_paths(base)
    repo = Path(repo)
    rel = (decision.get("file") or "").strip().replace("\\", "/")
    mems = _read_jsonl(P["memory"])
    fid = f"mem_{len(mems) + 1:05d}"
    entry = {"id": fid, "ts": utcnow(), "repository": repo.name,
             "file": rel, "symbol": decision.get("symbol", ""),
             "lines": [], "anchor": decision.get("anchor", "")[:160],
             "hash": "", "excerpt": "", "fact": decision.get("text", ""),
             "reason": decision.get("reason", "")[:200] or f"observed ({task_ref})",
             "created_by_task": task_ref, "confidence": 0.8, "status": "active", "found": False}
    if rel:
        loc = resolve_anchor(repo, rel, symbol=entry["symbol"] or None,
                             anchor=entry["anchor"] or None)
        if loc.get("found"):
            entry.update({"found": True, "lines": loc["lines"], "hash": loc["hash"],
                          "excerpt": loc.get("excerpt", "")})
    _append_jsonl(P["memory"], entry)
    return entry


def apply_rule(base_text: str, decision: dict, evidence, base: Path = EVODIR):
    P = evo_paths(base)
    v = _next_version(base)
    rules = _active_rules(base)
    new_rule = {"v": v, "text": decision.get("text", ""),
                "reason": decision.get("reason", "")[:200],
                "evidence": (decision.get("evidence", []) or evidence or [])[:4],
                "ts": utcnow(), "active": True}
    rules.append(new_rule)
    _append_jsonl(P["rules"], new_rule)
    rendered = _render_prompt(base_text, [r for r in _active_rules(base)])
    (P["prompts"] / f"{v}.md").write_text(rendered, encoding="utf-8")
    P["current"].write_text(rendered, encoding="utf-8")
    _append_jsonl(P["evolution"], {"ts": utcnow(), "kind": "rule",
                                   "from": current_version(base), "to": v,
                                   "reason": new_rule["reason"],
                                   "change": {"added": [new_rule["text"]]},
                                   "evidence": new_rule["evidence"]})
    _append_jsonl(P["root"] / "decisions.jsonl",
                   {"ts": utcnow(), "decision": "adopt_rule", "version": v,
                    "reason": new_rule["reason"],
                    "alternatives_considered": "skip (duplicate/noise handled upstream)",
                    "evidence": new_rule["evidence"]})
    # prune old full snapshots
    vers = sorted(P["prompts"].glob("v*.md"))
    for old in vers[:-PROMPT_KEEP_VERSIONS]:
        try:
            old.unlink()
        except Exception:
            pass
    return v


def rollback(base_text: str, target: str, base: Path = EVODIR):
    P = evo_paths(base)
    target = target if target.startswith("v") else f"v{int(target):03d}"
    src = P["prompts"] / f"{target}.md"
    if not src.exists():
        return {"ok": False, "error": f"unknown version {target}"}
    # deactivate rules newer than target, then point current at the snapshot
    kept = []
    rules = _read_jsonl(P["rules"])
    P["rules"].write_text("", encoding="utf-8")
    for r in rules:
        try:
            keep = r.get("v", "v999") <= target and r.get("active", True)
        except Exception:
            keep = False
        r["active"] = bool(keep)
        _append_jsonl(P["rules"], r)
        if keep:
            kept.append(r)
    P["current"].write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    _append_jsonl(P["evolution"], {"ts": utcnow(), "kind": "rollback",
                                   "from": current_version(base), "to": target,
                                   "reason": "manual rollback"})
    _append_jsonl(P["root"] / "decisions.jsonl",
                   {"ts": utcnow(), "decision": "rollback", "version": target,
                    "reason": "manual rollback",
                    "alternatives_considered": "forward-fix rejected in favor of restore",
                    "evidence": []})
    return {"ok": True, "to": target, "active_rules": len(kept)}


def history(base: Path = EVODIR, limit: int = 15):
    P = evo_paths(base)
    evo = _read_jsonl(P["evolution"])[-limit:]
    cycles = _read_jsonl(P["cycles"])[-5:]
    return {"current": current_version(base),
            "transitions": evo,
            "recent_cycles": [{"ts": c.get("ts"), "observations": len(c.get("observations", [])),
                               "applied": c.get("applied", {})} for c in cycles],
            "rules": _active_rules(base)}


# ============================================================
#  EVOLVE CYCLE — observe -> judge -> apply (facts + versioned rules)
# ============================================================

def _record_failures(observations, base: Path):
    P = evo_paths(base)
    n = 0
    for o in observations:
        if o.get("kind") in ("repeated_failure", "timeout") or str(o.get("kind", "")).startswith("log_model_error"):
            _append_jsonl(P["failures"], {"ts": utcnow(), "kind": o["kind"],
                                          "detail": o.get("detail", "")[:300],
                                          "evidence": o.get("evidence", [])[:2]})
            n += 1
    return n


async def evolve_once(sessions_root, repo_root, base_prompt_text: str, llm,
                      base: Path = EVODIR, dry: bool = False, task_ref: str = "manual",
                      log_fn=None):
    """One full evolution cycle. Returns the cycle record. Never raises."""
    P = evo_paths(base)
    P["root"].mkdir(parents=True, exist_ok=True)
    log = log_fn or (lambda *a, **k: None)
    cur = ensure_seeded(base_prompt_text, base)
    log(f"evolution cycle start (prompt {cur})")

    snap = take_snapshot(repo_root, base)
    old, new = _latest_snapshot(base)
    changes = diff_snapshots(old, new) if old and new else []
    if changes:
        for c in changes[:20]:
            _append_jsonl(P["changes"], {"ts": utcnow(), **c, "task_ref": task_ref})
        log(f"repo changes since last cycle: {len(changes)} files")

    obs = mine_sessions(sessions_root) + mine_log(Path(repo_root) / "nexus.log")
    # Phase 7: aggregate past failures into behavior defects (occurrences matter).
    for g in aggregate_defects(base):
        if g["occurrences"] >= 2:
            obs.append({"kind": "behavior_defect", "count": g["occurrences"],
                        "detail": f"REPEATED x{g['occurrences']} ({g['impact']} impact): "
                                  f"{g['signature'][:220]}",
                        "evidence": g["evidence"]})
    # Phase 17: the loop watches itself first.
    obs = evolution_selfcheck(base) + obs
    obs = obs[:MAX_OBSERVATIONS_PER_CYCLE * 2]
    log(f"observations mined: {len(obs)}")

    index = {k: v for k, v in (snap.get("files", {}) or {}).items()}
    rules = [r.get("text", "") for r in _active_rules(base)]
    decisions = await judge(llm, obs, index, rules, session_note=f"task={task_ref}")
    log(f"judge verdicts: {[d.get('verdict') for d in decisions]}")

    applied = {"facts": [], "rules": [], "skipped": 0}
    if not dry:
        for d in decisions:
            try:
                if d.get("verdict") == "fact" and d.get("text") and d.get("file"):
                    applied["facts"].append(apply_fact(repo_root, d, task_ref, base)["id"])
                elif d.get("verdict") == "rule" and d.get("text"):
                    # exact + near-duplicate guard (Phase 13 lite): never stack
                    # a rule the prompt already carries.
                    if is_duplicate_rule(d["text"], _active_rules(base)):
                        applied["skipped"] += 1
                        log("duplicate rule skipped")
                    else:
                        applied["rules"].append(apply_rule(base_prompt_text, d, d.get("evidence", []), base))
                else:
                    applied["skipped"] += 1
            except Exception as e:
                applied["skipped"] += 1
                log(f"apply failed: {e}")
        fails = _record_failures(obs, base)
        log(f"applied facts={len(applied['facts'])} rules={len(applied['rules'])} failures_logged={fails}")
    cycle = {"ts": utcnow(), "task": task_ref, "dry": dry,
             "prompt_before": cur, "prompt_after": current_version(base),
             "observations": [{"kind": o.get("kind"), "detail": str(o.get("detail", ""))[:200]} for o in obs],
             "decisions": decisions, "applied": applied,
             "repo_changes": len(changes)}
    _append_jsonl(P["cycles"], cycle)
    drift = []
    if not dry:
        if applied.get("facts") or applied.get("rules"):
            commit_evolution(base, applied)
            log(f"generation now {runtime_state(base)['generation']}")
        else:
            st = runtime_state(base)
            st["turns_since_evolve"] = 0
            st["last_cycle_ts"] = utcnow()
            _save_state(base, st)
            log("idle cycle — trigger counter reset")
        # Phase 14: does the new state still match the repo?
        try:
            drift = check_drift(base)
            if drift:
                log(f"drift: {len(drift)} rule(s) reference drifted files")
            cycle["drift_flags"] = drift
            # re-write cycle with drift info (cheap: rewrite last line)
            lines = P["cycles"].read_text(encoding="utf-8").splitlines()
            if lines:
                lines[-1] = json.dumps(cycle, ensure_ascii=False)
                P["cycles"].write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception as e:
            log(f"drift check failed: {e}")
    return cycle


# ============================================================
#  SCHEMAS (the 4 contracts — RunEvent, RepositoryMemory, CodeChange,
#  PromptEvolution) + structured RUN RECORDER + typed memory engine
# ============================================================
#
#  RunEvent:        {event_id, run_id, ts, type, agent, ...payload}
#    types: task_started, plan, thought, tool_call, tool_error, test_passed,
#           test_failed, delegate, agent_message, plan_op, agent_error, run_finished
#  RepositoryMemory:{memory_id, type, content, evidence[{file,lines,hash}],
#                   reason, task_ref, ts, status}
#    types: fact, architecture, convention, decision, failure, success,
#           behavior, change, warning.  NO evidence -> rejected, no exceptions.
#  CodeChange:      {task_id, file, before{lines,hash}, after{lines,hash},
#                   change_type, diff, reason}
#  PromptEvolution: {from, to, reason, change{added[]}, evidence[]}
#  Run record:      {run_id, sid, turn, task, ts_start, ts_end, status,
#                   actions[], files[], tools{}, errors[], tests[], final{}}

MEMORY_TYPES = {"fact", "architecture", "convention", "decision", "failure",
                "success", "behavior", "change", "warning"}

SOURCE_SUFFIXES = (".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".md",
                   ".json", ".txt", ".ps1", ".sh", ".yaml", ".yml", ".toml", ".cfg")


def files_from_params(params, repo_index=None) -> list:
    """Extract repo-relative file refs from tool params. Never raises."""
    found, seen = [], set()
    try:
        blob = json.dumps(params or {}, ensure_ascii=False)
    except Exception:
        blob = str(params or "")
    index_paths = set((repo_index or {}).keys())
    for tok in re.split(r"[\s\"'`,;()|&<>]+", blob):
        t = tok.strip().lstrip("./").replace("\\", "/")
        if not t or len(t) > 180:
            continue
        low = t.lower()
        if not low.endswith(SOURCE_SUFFIXES):
            continue
        cand = t if t in index_paths else None
        if cand is None:
            for p in index_paths:
                if p.endswith("/" + t) or p == t:
                    cand = p
                    break
        if cand and cand not in seen:
            seen.add(cand)
            found.append(cand)
    return found[:8]


def _is_test_command(cmd: str) -> bool:
    c = (cmd or "").lower()
    return any(k in c for k in ("pytest", "unittest", "npm test", "jest ", "go test", "cargo test"))


class EvolutionRecorder:
    """Listener that rebuilds every turn step-by-step into runs/{run_id}.json."""

    def __init__(self, base: Path = EVODIR):
        self.base = base
        self.open_runs = {}
        self._wsnap = {}  # sid -> {relpath: sha1} workspace snapshot at run_start

    def _paths(self):
        P = evo_paths(self.base)
        (P["root"] / "runs").mkdir(parents=True, exist_ok=True)
        return P

    @staticmethod
    def scan_workspace(workspace) -> dict:
        """Lightweight {relpath: sha1} listing (files <2MB, max 500). Never raises."""
        out = {}
        try:
            root = Path(workspace)
            if not root.is_dir():
                return out
            n = 0
            for p in sorted(root.rglob("*")):
                if n >= 500 or not p.is_file():
                    continue
                try:
                    if p.stat().st_size > 2_000_000:
                        continue
                    h = hashlib.sha1(p.read_bytes()).hexdigest()[:8]
                    out[p.relative_to(root).as_posix()] = h
                    n += 1
                except Exception:
                    continue
        except Exception:
            pass
        return out

    def run_start(self, sid: str, turn: int, task: str, workspace=None):
        try:
            old = self.open_runs.pop(sid, None)
            if old:
                old["status"] = "interrupted"
                old["ts_end"] = utcnow()
                self._flush(old)
            rid = f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}_{str(sid)[-8:]}_t{turn}"
            self.open_runs[sid] = {
                "run_id": rid, "sid": sid, "turn": turn,
                "task": str(task or "")[:1000], "ts_start": utcnow(), "ts_end": None,
                "status": "running", "actions": [], "files": [], "tools": {},
                "errors": [], "tests": [], "final": {}, "_last_tool": None,
            }
            self._wsnap[sid] = self.scan_workspace(workspace) if workspace else {}
        except Exception:
            pass

    def _run(self, sid: str):
        return self.open_runs.get(sid)

    def _push(self, run: dict, ev: dict):
        try:
            ev = dict(ev or {})
            ev["event_id"] = f"evt_{len(run['actions']) + 1:04d}"
            ev["run_id"] = run["run_id"]
            ev["ts"] = utcnow()
            run["actions"].append(ev)
        except Exception:
            pass

    def _flush(self, run: dict):
        try:
            P = self._paths()
            clean = {k: v for k, v in run.items() if not k.startswith("_")}
            with (P["root"] / "runs" / f"{run['run_id']}.json").open("w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=1)
        except Exception:
            pass

    def listener(self, sid: str, event: str, data: dict):
        """Fan-out target: attach per-agent via closure. Never raises."""
        try:
            run = self._run(sid)
            if not run:
                return
            d = data or {}
            if event == "iteration":
                self._push(run, {"type": "plan", "agent": "leader",
                                 "n": d.get("n"), "max": d.get("max")})
            elif event == "thinking":
                self._push(run, {"type": "thought", "agent": d.get("who", "?"),
                                 "text": str(d.get("text", ""))[:500]})
            elif event in ("tool_result", "wtool_result"):
                res = d.get("result", {}) or {}
                ok = bool(res.get("success", False))
                err = str(res.get("error", "") or "")[:300]
                who = d.get("who", "leader")
                name = d.get("name", "?")
                tkey = (who, name, _norm_params(d.get("params")))
                retry = (run.get("_last_tool") == tkey)
                run["_last_tool"] = tkey
                cmd = ""
                try:
                    cmd = (d.get("params", {}) or {}).get("command", "") or \
                          (d.get("params", {}) or {}).get("code", "")[:50]
                except Exception:
                    pass
                is_test = _is_test_command(cmd or "")
                # schema types: test_* > tool_error > retry > command(exec) > tool_call
                if not ok:
                    etype = "tool_error"
                elif is_test:
                    etype = "test_passed" if ok else "test_failed"
                elif retry:
                    etype = "retry"
                elif name in ("exec.shell", "exec.code"):
                    etype = "command"
                else:
                    etype = "tool_call"
                self._push(run, {"type": etype,
                                 "agent": who, "round": d.get("round", ""),
                                 "tool": name, "retry": retry,
                                 "params": str(json.dumps(d.get("params", {}),
                                                          ensure_ascii=False))[:400],
                                 "success": ok, "elapsed": d.get("elapsed", 0),
                                 "error": err})
                if not ok:
                    run["errors"].append({"tool": d.get("name", "?"), "error": err,
                                          "agent": who})
                t = run["tools"].setdefault(d.get("name", "?"), {"calls": 0, "fails": 0})
                t["calls"] += 1
                if not ok:
                    t["fails"] += 1
                for fp in files_from_params(d.get("params")):
                    if fp not in run["files"]:
                        run["files"].append(fp)
                if is_test:
                    run["tests"].append({"command": str(cmd)[:160],
                                         "passed": ok, "agent": who})
            elif event == "delegate_start":
                self._push(run, {"type": "delegate", "agent": "leader",
                                 "round": d.get("round", ""),
                                 "workers": d.get("ids", [])})
            elif event in ("subagent_start", "subagent_done"):
                done = event == "subagent_done"
                txt = ("started: " + str(d.get("brief", ""))[:160]) if not done \
                    else ("done ok=%s tools=%s" % (d.get("success"), d.get("tools_used", 0)))
                self._push(run, {"type": "agent_message", "agent": d.get("id", "?"),
                                 "round": d.get("round", ""), "text": txt})
                if done and not d.get("success"):
                    out_txt = str(d.get("output", "") or d.get("error", ""))[:300]
                    run["errors"].append({"worker": d.get("id", "?"),
                                          "round": d.get("round", ""),
                                          "error": out_txt or "worker failed"})
                    if "budget exhausted" in out_txt:
                        run["errors"][-1]["kind"] = "budget_exhausted"
            elif event == "radio":
                self._push(run, {"type": "agent_message", "agent": d.get("from", "?"),
                                 "round": d.get("round", ""),
                                 "to": d.get("to", "all"),
                                 "text": str(d.get("text", ""))[:300]})
            elif event == "plan":
                self._push(run, {"type": "plan_op", "agent": d.get("by", "?"),
                                 "round": d.get("round", ""),
                                 "action": d.get("action", ""),
                                 "step": d.get("step", "")})
            elif event == "final":
                content = d.get("content", "") or ""
                run["final"] = {"len": len(content), "preview": content[:2000]}
                run["status"] = "error" if content.startswith("[") and "error" in content[:60].lower() \
                    else "ok"
                run["ts_end"] = utcnow()
                self._push(run, {"type": "finish", "agent": "leader",
                                 "status": run["status"],
                                 "actions": len(run["actions"]),
                                 "errors": len(run["errors"])})
                # workspace before/after -> file_created / file_modified / file_deleted
                try:
                    before = self._wsnap.pop(run["sid"], {}) or {}
                    after = self.scan_workspace(d.get("workspace") or "") if d.get("workspace") \
                        else {}
                    if before or after:
                        for fp in sorted(set(after) - set(before)):
                            self._push(run, {"type": "file_created", "agent": "team",
                                             "file": fp, "hash": after[fp]})
                            if fp not in run["files"]:
                                run["files"].append(fp)
                        for fp in sorted(set(before) & set(after)):
                            if before[fp] != after[fp]:
                                self._push(run, {"type": "file_modified", "agent": "team",
                                                 "file": fp, "before": before[fp],
                                                 "after": after[fp]})
                                if fp not in run["files"]:
                                    run["files"].append(fp)
                        for fp in sorted(set(before) - set(after)):
                            self._push(run, {"type": "file_deleted", "agent": "team",
                                             "file": fp})
                except Exception:
                    pass
                self._flush(run)
                self.open_runs.pop(run["sid"], None)
        except Exception:
            pass


def remember_typed(mem_type: str, content: str, evidence: list, reason: str,
                   task_ref: str, repo, base: Path = EVODIR):
    """Typed repo memory. Evidence (existing files) is MANDATORY — rejected otherwise."""
    if mem_type not in MEMORY_TYPES:
        return {"ok": False, "error": f"unknown memory type {mem_type!r}"}
    content = (content or "").strip()[:600]
    if not content:
        return {"ok": False, "error": "empty content"}
    P = evo_paths(base)
    repo = Path(repo)
    ev_out = []
    for ev in evidence or []:
        rel = str((ev or {}).get("file", "")).strip().replace("\\", "/")
        if not rel or not (repo / rel).exists():
            continue
        loc = resolve_anchor(repo, rel, symbol=(ev or {}).get("symbol") or None,
                             anchor=(ev or {}).get("anchor") or None)
        ev_out.append({"file": rel, "lines": loc.get("lines", []),
                       "hash": loc.get("hash", ""), "found": bool(loc.get("found"))})
    if not ev_out:
        return {"ok": False, "error": "memory without evidence is forbidden — attach an existing file"}
    mems = _read_jsonl(P["memory"])
    entry = {"id": f"mem_{len(mems) + 1:05d}", "ts": utcnow(), "type": mem_type,
             "content": content, "evidence": ev_out,
             "reason": (reason or "")[:250], "task_ref": task_ref,
             "repository": repo.name, "confidence": 0.8, "status": "active"}
    _append_jsonl(P["memory"], entry)
    return {"ok": True, "id": entry["id"]}


def revalidate(repo, base: Path = EVODIR):
    """Re-resolve every active memory anchor. moved/stale tracked, history kept."""
    P = evo_paths(base)
    repo = Path(repo)
    mems = _read_jsonl(P["memory"])
    moved = stale = ok = skipped = 0
    for m in mems:
        if m.get("status") not in ("active", None):
            skipped += 1
            continue
        changed = False
        # legacy entries (from apply_fact) carry file/symbol/anchor at top level
        evs = m.get("evidence") or ([{"file": m.get("file", "")}] if m.get("file") else [])
        new_evs = []
        for ev in evs:
            rel = str(ev.get("file", ""))
            loc = resolve_anchor(repo, rel, symbol=ev.get("symbol") or m.get("symbol"),
                                 anchor=ev.get("anchor") or m.get("anchor"))
            if not loc.get("found"):
                new_evs.append({**ev, "stale": True})
                continue
            if ev.get("hash") and loc.get("hash") != ev.get("hash"):
                m["status"] = "moved"
                m["moved_to"] = {"lines": loc["lines"], "hash": loc["hash"]}
                moved += 1
                changed = True
            new_evs.append({"file": rel, "lines": loc["lines"], "hash": loc["hash"],
                            "found": True})
        if m.get("status") == "active" and all(e.get("stale") for e in new_evs):
            m["status"] = "stale"
            stale += 1
            changed = True
        elif new_evs and m.get("status") == "active":
            if [ (e.get("lines"), e.get("hash")) for e in new_evs] != \
               [ (e.get("lines"), e.get("hash")) for e in evs if not e.get("stale")]:
                ok += 1
            m["evidence"] = new_evs
            changed = True
        else:
            m["evidence"] = new_evs
    if moved or stale or ok:
        P["memory"].write_text("\n".join(json.dumps(m, ensure_ascii=False) for m in mems) + "\n",
                               encoding="utf-8")
    return {"moved": moved, "stale": stale, "reconfirmed": ok, "skipped": skipped,
            "total": len(mems)}


def _tokens(s: str) -> set:
    return set(w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(w) > 3)


def is_duplicate_rule(text: str, active_rules) -> bool:
    """Exact or near-duplicate (>=0.8 token Jaccard) of an active rule."""
    norm = " ".join((text or "").lower().split())
    if not norm:
        return True
    for r in active_rules or []:
        other = " ".join(str(r.get("text", "")).lower().split())
        if not other:
            continue
        if norm == other:
            return True
        a, b = _tokens(norm), _tokens(other)
        if a and b and len(a & b) / max(len(a | b), 1) >= 0.8:
            return True
    return False


def retrieve(task_text: str, base: Path = EVODIR, limit: int = 3, max_chars: int = 900) -> str:
    """Keyword retrieval: task -> relevant active memories. For per-turn injection."""
    mems = [m for m in _read_jsonl(evo_paths(base)["memory"]) if m.get("status") == "active"]
    if not mems or not (task_text or "").strip():
        return ""
    qtok = _tokens(task_text)
    scored = []
    for m in mems:
        blob = " ".join([m.get("content", ""), m.get("type", ""),
                         " ".join(e.get("file", "") for e in (m.get("evidence") or []))])
        st = _tokens(blob)
        s = len(qtok & st)
        if m.get("type") in ("warning", "behavior") and s:
            s += 1
        if s:
            scored.append((s, m))
    scored.sort(key=lambda x: -x[0])
    lines = []
    total = 0
    for _, m in scored[:limit]:
        line = f"[{m.get('type')}] {m.get('content', '')[:220]}"
        ev = (m.get("evidence") or [{}])[0]
        if ev.get("file"):
            line += f" ({ev['file']}" + \
                    (f":{ev['lines'][0]}-{ev['lines'][1]}" if ev.get("lines") else "") + ")"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    if not lines:
        return ""
    return "RELEVANT REPO MEMORY (learned from past runs — heed warnings):\n" + "\n".join(lines)


def recent_runs(base: Path = EVODIR, limit: int = 8):
    try:
        runs = sorted((evo_paths(base)["root"] / "runs").glob("*.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        out = []
        for r in runs:
            try:
                d = json.loads(r.read_text(encoding="utf-8"))
            except Exception:
                continue
            out.append({"run_id": d.get("run_id"), "status": d.get("status"),
                        "task": str(d.get("task", ""))[:80],
                        "actions": len(d.get("actions", [])),
                        "tools": sum(t.get("calls", 0) for t in (d.get("tools", {}) or {}).values()),
                        "errors": len(d.get("errors", [])),
                        "files": d.get("files", [])[:6]})
        return out
    except Exception:
        return []

def repo_file_count(base: Path = EVODIR) -> int:
    try:
        fp = evo_paths(base)["files"]
        if not fp.exists():
            return 0
        return len(json.loads(fp.read_text(encoding="utf-8")) or {})
    except Exception:
        return 0

# ============================================================
#  RUNTIME LAYER — persistent supervisor state (Phases 1/9/10)
#  The process itself (web server / TUI) is the Runtime that never
#  restarts; generations are prompt+memory epochs, advanced here.
#  state.json: {generation, prompt_version, memory_version,
#               turns_since_evolve, last_cycle_ts}
# ============================================================

def runtime_state(base: Path = EVODIR) -> dict:
    try:
        fp = evo_paths(base)["root"] / "state.json"
        if fp.exists():
            d = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return {"generation": int(d.get("generation", 1)),
                        "prompt_version": str(d.get("prompt_version", current_version(base))),
                        "memory_version": int(d.get("memory_version", 0)),
                        "turns_since_evolve": int(d.get("turns_since_evolve", 0)),
                        "last_cycle_ts": d.get("last_cycle_ts")}
    except Exception:
        pass
    return {"generation": 1, "prompt_version": current_version(base),
            "memory_version": 0, "turns_since_evolve": 0, "last_cycle_ts": None}


def _save_state(base: Path, st: dict):
    try:
        P = evo_paths(base)
        P["root"].mkdir(parents=True, exist_ok=True)
        (P["root"] / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
    except Exception:
        pass


def note_turn_completed(base: Path = EVODIR):
    try:
        st = runtime_state(base)
        st["turns_since_evolve"] = int(st.get("turns_since_evolve", 0)) + 1
        _save_state(base, st)
    except Exception:
        pass


def _active_memory_count(base: Path) -> int:
    try:
        return sum(1 for m in _read_jsonl(evo_paths(base)["memory"])
                   if m.get("status") == "active")
    except Exception:
        return 0


def commit_evolution(base: Path, applied: dict):
    """Phase 9: generation+1, versions synced, evolution.completed recorded."""
    st = runtime_state(base)
    st["generation"] = int(st.get("generation", 1)) + 1
    st["prompt_version"] = current_version(base)
    st["memory_version"] = _active_memory_count(base)
    st["turns_since_evolve"] = 0
    st["last_cycle_ts"] = utcnow()
    _save_state(base, st)
    _append_jsonl(evo_paths(base)["evolution"],
                   {"event": "evolution.completed", "kind": "completed",
                    "ts": st["last_cycle_ts"], "generation": st["generation"],
                    "prompt_version": st["prompt_version"],
                    "memory_version": st["memory_version"],
                    "applied": {k: (v if isinstance(v, int) else len(v))
                                for k, v in (applied or {}).items()}})
    return st


def last_run_summary(sid: str, base: Path = EVODIR):
    """Newest recorded run for a session (for the auto-trigger). Never raises."""
    try:
        runs = sorted((evo_paths(base)["root"] / "runs").glob("*.json"),
                      key=lambda p: p.stat().st_mtime, reverse=True)[:20]
        for r in runs:
            try:
                d = json.loads(r.read_text(encoding="utf-8"))
            except Exception:
                continue
            if d.get("sid") == sid:
                return d
    except Exception:
        pass
    return None


def should_auto_evolve(last_run=None, base: Path = EVODIR):
    """Phase 5 trigger: N turns elapsed OR failure pattern last turn."""
    try:
        if os.getenv("EVOLVE_AUTO", "1").strip().lower() not in ("1", "true", "yes", "on"):
            return False, "auto disabled"
        st = runtime_state(base)
        try:
            every = max(1, int(os.getenv("EVOLVE_EVERY_TURNS", "10") or 10))
        except Exception:
            every = 10
        if int(st.get("turns_since_evolve", 0)) >= every:
            return True, f"{st['turns_since_evolve']} turns since last evolve"
        if os.getenv("EVOLVE_ON_FAILURE", "1").strip().lower() in ("1", "true", "yes", "on") \
                and isinstance(last_run, dict):
            errs = last_run.get("errors", []) or []
            budget = sum(1 for e in errs if "budget exhausted" in str(e.get("error", "")))
            if budget >= 2:
                return True, f"{budget} worker budget-exhaustions last turn"
            if len(errs) >= 3:
                return True, f"{len(errs)} tool errors last turn"
    except Exception:
        pass
    return False, ""

# ============================================================
#  DEEP LAYERS — behavior defects, drift, lineage, self-check
#  (plan Phases 7/14/15/16/17)
# ============================================================

def aggregate_defects(base: Path = EVODIR):
    """Group failures.jsonl into behavior defects with occurrences + impact."""
    groups = {}
    for f in _read_jsonl(evo_paths(base)["failures"]):
        detail = str(f.get("detail", ""))
        key = (f.get("kind", "?"), re.sub(r"\d+", "#", detail[:120]))
        g = groups.setdefault(key, {"kind": f.get("kind", "?"), "signature": detail[:160],
                                    "occurrences": 0, "first_ts": f.get("ts", ""),
                                    "last_ts": f.get("ts", ""), "evidence": []})
        g["occurrences"] += 1
        g["last_ts"] = f.get("ts", g["last_ts"])
        if len(g["evidence"]) < 3:
            g["evidence"].append(detail[:160])
    out = []
    for g in groups.values():
        g["impact"] = "high" if g["occurrences"] >= 3 else ("medium" if g["occurrences"] == 2 else "low")
        out.append(g)
    out.sort(key=lambda g: -g["occurrences"])
    return out


def _rule_file_refs(rule: dict):
    refs = set()
    for blob in [rule.get("text", ""), rule.get("reason", "")]:
        for m in re.finditer(r"[\w\-/]+\.(?:py|js|jsx|ts|tsx|md|json|html|css|txt)", blob or ""):
            refs.add(m.group(0).split("/")[-1])
    return refs


def check_drift(base: Path = EVODIR):
    """Rules whose referenced files went stale/moved in repo memory."""
    mems = _read_jsonl(evo_paths(base)["memory"])
    bad_files = set()
    for m in mems:
        if m.get("status") in ("stale", "moved"):
            for ev in (m.get("evidence") or []):
                if ev.get("file"):
                    bad_files.add(ev["file"].split("/")[-1])
            if m.get("file"):
                bad_files.add(str(m["file"]).split("/")[-1])
    flags = []
    for r in _active_rules(base):
        hit = sorted(_rule_file_refs(r) & bad_files)
        if hit:
            flags.append({"rule": r.get("v", "?"), "text": (r.get("text", "") or "")[:140],
                          "reason": "evidence files drifted: " + ", ".join(hit),
                          "files": hit})
    return flags


def lineage_for_rule(version: str, base: Path = EVODIR):
    """Answer 'why does the prompt say this?': rule -> failures -> tasks -> files."""
    P = evo_paths(base)
    version = version if str(version).startswith("v") else f"v{int(version):03d}"
    rule = next((r for r in _active_rules(base) + _read_jsonl(P["rules"])
                 if r.get("v") == version), None)
    if not rule:
        return {"ok": False, "error": f"unknown rule version {version}"}
    refs = _rule_file_refs(rule)
    linked_failures = []
    for f in _read_jsonl(P["failures"]):
        blob = json.dumps(f, ensure_ascii=False)
        if any(rf in blob for rf in refs) or \
           any(w in blob.lower() for w in _tokens(rule.get("text", "")) if len(w) > 5):
            linked_failures.append({"ts": f.get("ts"), "kind": f.get("kind"),
                                    "detail": str(f.get("detail", ""))[:160]})
            if len(linked_failures) >= 5:
                break
    linked_memories = []
    for m in _read_jsonl(P["memory"]):
        mfiles = {str((e or {}).get("file", "")).split("/")[-1] for e in (m.get("evidence") or [])}
        if refs & mfiles or (m.get("file", "") or "").split("/")[-1] in refs:
            linked_memories.append({"id": m.get("id"), "type": m.get("type"),
                                    "content": str(m.get("content", ""))[:140]})
            if len(linked_memories) >= 5:
                break
    evo = [e for e in _read_jsonl(P["evolution"]) if e.get("to") == version]
    return {"ok": True, "version": version, "rule": rule.get("text", ""),
            "reason": rule.get("reason", ""), "evidence": rule.get("evidence", []),
            "linked_failures": linked_failures, "linked_memories": linked_memories,
            "evolution_record": (evo or [{}])[-1]}


def evolution_selfcheck(base: Path = EVODIR):
    """Phase 17 lite: the loop watches itself. Returns meta-observations."""
    cycles = _read_jsonl(evo_paths(base)["cycles"])[-5:]
    if len(cycles) < 2:
        return []
    obs = []
    judge_fails = sum(1 for c in cycles for d in (c.get("decisions", []) or [])
                      if isinstance(d, dict) and str(d.get("reason", "")).startswith("judge failed"))
    if judge_fails >= 2:
        obs.append({"kind": "evolution_selfcheck", "count": judge_fails,
                    "detail": f"judge LLM failed {judge_fails}x in last {len(cycles)} cycles "
                              f"(malformed JSON?) — simplify the judge contract or retry parsing",
                    "evidence": []})
    idle = [c for c in cycles if c.get("observations") and
            not (c.get("applied", {}) or {}).get("facts") and
            not (c.get("applied", {}) or {}).get("rules")]
    if len(idle) >= 3:
        obs.append({"kind": "evolution_selfcheck", "count": len(idle),
                    "detail": f"{len(idle)} straight cycles observed but applied nothing — "
                              f"thresholds may be too strict or miners too quiet",
                    "evidence": []})
    return obs
