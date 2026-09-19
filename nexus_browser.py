#!/usr/bin/env python3
"""
NEXUS browser tool — the FULL Playwright library behind ONE agent tool.

Tool name: `browser`. Every capability is an `action` (+ flat params).
State persists across calls inside the running event loop: named sessions,
each with its own browser + context + tabs. First use auto-launches.

Action groups
  lifecycle : launch, close, tabs, new_tab, use_tab, close_tab
  navigate  : goto, reload, back, forward
  observe   : snapshot, text, html, title, url, console, network, dialogs, downloads, videos
  act       : click, dblclick, hover, focus, fill, type, press, select, check,
              uncheck, set_file, drag, mouse, scroll, eval, wait
  capture   : screenshot, pdf
  state     : cookies, storage, state_save, viewport, offline, headers,
              block, unblock, trace_start, trace_stop, request,eval

Nothing here ever raises: every failure is a {"success": False, ...} dict.
Requires: pip install playwright && python -m playwright install chromium
"""

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

SNAP_MAX = 12000
TEXT_MAX = 12000
HTML_MAX = 15000
LOG_KEEP = 200

_pw = None            # async_playwright() instance (lazy)
_sessions: Dict[str, "BrowserSession"] = {}
_shot_seq = 0


# ---------------------------------------------------------------- helpers

def _short(s: Any, n: int = 4000) -> str:
    try:
        s = str(s)
    except Exception:
        s = "<unprintable>"
    if len(s) <= n:
        return s
    return s[:n] + f"...[truncated {len(s) - n} chars]"


def _ok(action: str, session: str, tab: int, **payload) -> dict:
    out = {"success": True, "action": action, "session": session, "tab": tab}
    out.update(payload)
    return out


def _err(action: str, session: str, error: Any, **payload) -> dict:
    out = {"success": False, "action": action, "session": session,
           "error": _short(error, 600)}
    out.update(payload)
    return out


def _need_pw():
    """Import playwright lazily so main.py loads without it installed."""
    global _pw
    if _pw is not None:
        return _pw
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        raise RuntimeError("Playwright is not installed — run: pip install playwright "
                           "&& python -m playwright install chromium")
    return async_playwright  # class, started by ensure()


async def _ensure_pw():
    global _pw
    if _pw is not None and not isinstance(_pw, type):
        return _pw
    mod = _need_pw()
    _pw = await mod().start()
    return _pw


def _ws_dir(session_workspace: Optional[str]) -> Path:
    if session_workspace:
        p = Path(session_workspace)
    else:
        p = Path.home() / ".nexus" / "browser-shots"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


# ---------------------------------------------------------------- session

class BrowserSession:
    """One named browser + context + tab list. Never raises on helpers."""

    def __init__(self, label: str):
        self.label = label
        self.browser = None
        self.context = None
        self.pages: List[Any] = []
        self.active = 0
        self.headless = True
        self.browser_type = "chromium"
        self.dialog_log: List[dict] = []
        self.console_log: List[dict] = []
        self.net_log: List[dict] = []
        self.page_errs: List[dict] = []
        self.downloads: List[dict] = []
        self.videos_dir: Optional[Path] = None
        self.dialog_mode = "dismiss"
        self.ws: Optional[Path] = None

    # -- lifecycle -------------------------------------------------

    async def launch(self, ws: Path, browser: str = "chromium", headless: Optional[bool] = None,
                     viewport_w: int = 1366, viewport_h: int = 900,
                     user_agent: str = "", locale: str = "", timezone: str = "",
                     geolocation: str = "", permissions: Optional[List[str]] = None,
                     device: str = "", storage_state: str = "", proxy: str = "",
                     proxy_user: str = "", proxy_pass: str = "",
                     record_video: bool = False, dialogs: str = "dismiss",
                     ignore_https: bool = False) -> dict:
        pw = await _ensure_pw()
        if headless is None:
            headless = os.getenv("BROWSER_HEADFUL", "0") != "1"
        browser = (browser or "chromium").strip().lower()
        if browser not in ("chromium", "firefox", "webkit"):
            return {"ok": False, "error": f"unknown browser '{browser}' (chromium|firefox|webkit)"}
        if self.browser:
            await self.close()
        self.ws = ws
        self.headless = bool(headless)
        self.browser_type = browser
        self.dialog_mode = (dialogs or "dismiss").lower()
        try:
            launcher = getattr(pw, browser)
            self.browser = await launcher.launch(headless=self.headless)
        except Exception as e:
            self.browser = None
            hint = ""
            if "Executable doesn't exist" in str(e) or "executable" in str(e).lower():
                hint = f" — browser binary missing, run: python -m playwright install {browser}"
            return {"ok": False, "error": f"launch failed: {e}{hint}"}
        ctx_kw: Dict[str, Any] = {"accept_downloads": True}
        if viewport_w and viewport_h:
            ctx_kw["viewport"] = {"width": int(viewport_w), "height": int(viewport_h)}
        if user_agent:
            ctx_kw["user_agent"] = user_agent
        if locale:
            ctx_kw["locale"] = locale
        if timezone:
            ctx_kw["timezone_id"] = timezone
        if geolocation:
            try:
                lat_s, lon_s = geolocation.replace(" ", "").split(",")[:2]
                ctx_kw["geolocation"] = {"latitude": float(lat_s), "longitude": float(lon_s)}
                perms = list(permissions or [])
                if "geolocation" not in perms:
                    perms.append("geolocation")
                permissions = perms
            except Exception as e:
                return {"ok": False, "error": f"bad geolocation 'lat,lon': {e}"}
        if permissions:
            ctx_kw["permissions"] = permissions
        if device:
            try:
                ctx_kw.update(pw.devices[device])
            except KeyError:
                return {"ok": False,
                        "error": f"unknown device '{device}'. "
                                 f"Examples: iPhone 14, Pixel 7, Desktop Chrome"}
        if storage_state:
            sp = Path(storage_state)
            if not sp.is_absolute() and self.ws:
                sp = self.ws / storage_state
            if not sp.exists():
                return {"ok": False, "error": f"storage_state not found: {sp}"}
            ctx_kw["storage_state"] = str(sp)
        if proxy:
            pr: Dict[str, Any] = {"server": proxy}
            if proxy_user:
                pr["username"] = proxy_user
            if proxy_pass:
                pr["password"] = proxy_pass
            ctx_kw["proxy"] = pr
        if ignore_https:
            ctx_kw["ignore_https_errors"] = True
        if record_video:
            self.videos_dir = ws / "videos"
            self.videos_dir.mkdir(parents=True, exist_ok=True)
            ctx_kw["record_video_dir"] = str(self.videos_dir)
        else:
            self.videos_dir = None
        try:
            self.context = await self.browser.new_context(**ctx_kw)
        except Exception as e:
            await self.close()
            return {"ok": False, "error": f"context failed: {e}"}
        self.pages, self.active = [], 0
        self.dialog_log, self.console_log = [], []
        self.net_log, self.page_errs, self.downloads = [], [], []
        await self._new_page()
        return {"ok": True, "browser": browser, "headless": self.headless,
                "tabs": len(self.pages)}

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        except Exception:
            pass
        try:
            if self.browser:
                await self.browser.close()
        except Exception:
            pass
        self.context, self.browser, self.pages = None, None, []
        self.active = 0

    # -- tabs ------------------------------------------------------

    def _hook_page(self, page):
        mode = self.dialog_mode

        async def _on_dialog(dlg):
            try:
                self.dialog_log.append({"type": dlg.type, "message": _short(dlg.message, 500),
                                        "default": _short(dlg.default_value or "", 200),
                                        "ts": _stamp()})
                self.dialog_log = self.dialog_log[-LOG_KEEP:]
                if mode == "accept":
                    await dlg.accept(dlg.default_value or "")
                else:
                    await dlg.dismiss()
            except Exception:
                pass

        def _on_console(msg):
            try:
                self.console_log.append({"type": msg.type, "text": _short(msg.text, 500)})
                del self.console_log[:-LOG_KEEP]
            except Exception:
                pass

        def _on_err(exc):
            try:
                self.page_errs.append({"error": _short(exc, 500)})
                del self.page_errs[:-LOG_KEEP]
            except Exception:
                pass

        def _on_req(req):
            try:
                self.net_log.append({"dir": ">>", "method": req.method,
                                     "url": _short(req.url, 300)})
                del self.net_log[:-LOG_KEEP]
            except Exception:
                pass

        def _on_resp(resp):
            try:
                self.net_log.append({"dir": "<<", "status": resp.status,
                                     "url": _short(resp.url, 300)})
                del self.net_log[:-LOG_KEEP]
            except Exception:
                pass

        def _on_reqfail(req):
            try:
                self.net_log.append({"dir": "!!", "method": req.method,
                                     "url": _short(req.url, 300),
                                     "error": _short(req.failure or "", 200)})
                del self.net_log[:-LOG_KEEP]
            except Exception:
                pass

        async def _on_download(dl):
            try:
                dd = (self.ws or Path(".")) / "downloads"
                dd.mkdir(parents=True, exist_ok=True)
                dest = dd / (dl.suggested_filename or f"dl_{_stamp()}")
                await dl.save_as(str(dest))
                self.downloads.append({"name": dest.name, "path": str(dest),
                                       "size": dest.stat().st_size if dest.exists() else 0})
            except Exception as e:
                self.downloads.append({"error": _short(e, 300)})

        try:
            page.on("dialog", _on_dialog)
            page.on("console", _on_console)
            page.on("pageerror", _on_err)
            page.on("request", _on_req)
            page.on("response", _on_resp)
            page.on("requestfailed", _on_reqfail)
            page.on("download", _on_download)
        except Exception:
            pass

    async def _new_page(self, url: str = "about:blank"):
        if not self.context:
            return None, "no context — launch first"
        try:
            page = await self.context.new_page()
            self._hook_page(page)
            self.pages.append(page)
            self.active = len(self.pages) - 1
            if url and url != "about:blank":
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass
            return page, ""
        except Exception as e:
            return None, str(e)

    def page(self):
        if not self.pages:
            return None, "no tabs — use new_tab first"
        if self.active < 0 or self.active >= len(self.pages):
            self.active = 0
        try:
            if self.pages[self.active].is_closed():
                pass
        except Exception:
            pass
        return self.pages[self.active], ""

    async def meta(self, page) -> dict:
        try:
            return {"url": page.url, "title": _short(await page.title(), 300)}
        except Exception:
            try:
                return {"url": page.url, "title": ""}
            except Exception:
                return {"url": "", "title": ""}


def _get(label: str) -> BrowserSession:
    label = (label or "main").strip() or "main"
    ses = _sessions.get(label)
    if not ses:
        ses = BrowserSession(label)
        _sessions[label] = ses
    return ses


async def _ready(label: str, ws: Path, need_page: bool = True):
    """Return (session, page, error). Auto-launches on first use."""
    ses = _get(label)
    if not ses.browser or not ses.context:
        r = await ses.launch(ws)
        if not r.get("ok"):
            return ses, None, r.get("error", "launch failed")
    if need_page and not ses.pages:
        _, err = await ses._new_page()
        if err:
            return ses, None, err
    page, err = ses.page() if need_page else (None, "")
    return ses, page, err


# ---------------------------------------------------------------- AX tree

def _compact_ax(node: Any, depth: int = 0, max_depth: int = 14) -> List[str]:
    """Accessibility tree -> short indented lines. Never raises."""
    lines: List[str] = []
    try:
        if node is None or depth > max_depth:
            return lines
        if isinstance(node, list):
            for ch in node:
                lines.extend(_compact_ax(ch, depth, max_depth))
            return lines
        if not isinstance(node, dict):
            return lines
        role = str(node.get("role", ""))
        name = str(node.get("name", "") or "")
        if role in ("generic", "none", "presentation") and not name and depth > 4:
            for ch in node.get("children", []) or []:
                lines.extend(_compact_ax(ch, depth, max_depth))
            return lines
        bits = [role] if role else []
        if name:
            bits.append(f'"{name[:80]}"')
        for k in ("value", "checked", "disabled", "selected", "expanded",
                  "pressed", "level", "valuetext"):
            v = node.get(k)
            if v is True or (isinstance(v, str) and v) or (isinstance(v, (int, float)) and k == "level"):
                bits.append(f"{k}={v}" if not isinstance(v, bool) else k)
        if bits:
            lines.append("  " * min(depth, 10) + " ".join(bits))
        for ch in node.get("children", []) or []:
            lines.extend(_compact_ax(ch, depth + 1, max_depth))
    except Exception:
        pass
    return lines


# ---------------------------------------------------------------- dispatcher

async def tool_browser(action: str = "", session: str = "main",
                       session_workspace: Optional[str] = None, **kw) -> dict:
    """One Playwright tool, many actions. Never raises."""
    action = (action or "").strip().lower()
    label = (session or "main").strip() or "main"
    ws = _ws_dir(session_workspace)
    try:
        return await _dispatch(action, label, ws, kw)
    except Exception as e:
        return _err(action or "?", label, f"{type(e).__name__}: {e}")


async def _dispatch(action: str, label: str, ws: Path, kw: dict) -> dict:
    ses = _get(label)
    timeout = int(kw.get("timeout", 15000) or 15000)
    sel = (kw.get("selector") or "").strip()

    # read-only actions must never auto-launch a browser just to report emptiness
    if action in ("tabs", "console", "network", "dialogs", "downloads", "videos") \
            and (not ses.browser or not ses.context):
        if action == "tabs":
            return _ok(action, label, -1, tabs=[])
        if action == "videos":
            return _ok(action, label, -1, entries=_saved_videos(ws))
        return _ok(action, label, -1, entries=[], note="no live browser session")

    def loc(page, selector: str = ""):
        s = (selector or sel).strip()
        if not s:
            raise ValueError("selector is required for this action")
        return page.locator(s)

    # -- lifecycle ------------------------------------------------
    if action == "launch":
        r = await ses.launch(
            ws, browser=kw.get("browser", "chromium"),
            headless=None if kw.get("headless") in (None, "") else bool(kw.get("headless")),
            viewport_w=int(kw.get("viewport_w", 1366) or 1366),
            viewport_h=int(kw.get("viewport_h", 900) or 900),
            user_agent=kw.get("user_agent", "") or "", locale=kw.get("locale", "") or "",
            timezone=kw.get("timezone", "") or "", geolocation=kw.get("geolocation", "") or "",
            permissions=kw.get("permissions") or None, device=kw.get("device", "") or "",
            storage_state=kw.get("storage_state", "") or "",
            proxy=kw.get("proxy", "") or "", proxy_user=kw.get("proxy_user", "") or "",
            proxy_pass=kw.get("proxy_pass", "") or "",
            record_video=bool(kw.get("record_video", False)),
            dialogs=kw.get("dialogs", "dismiss") or "dismiss",
            ignore_https=bool(kw.get("ignore_https", False)))
        if not r.get("ok"):
            return _err(action, label, r.get("error"))
        if kw.get("url"):
            try:
                await ses.pages[ses.active].goto(
                    kw["url"], wait_until=kw.get("wait", "domcontentloaded") or "domcontentloaded",
                    timeout=timeout)
            except Exception as e:
                return _ok(action, label, ses.active, warning=f"launched, goto failed: {e}", **r,
                           **await ses.meta(ses.pages[ses.active]))
        return _ok(action, label, ses.active, **r,
                   **await ses.meta(ses.pages[ses.active]))

    if action == "close":
        if kw.get("which", "") == "all":
            for s in list(_sessions.values()):
                await s.close()
            _sessions.clear()
            return _ok(action, label, -1, closed="all sessions",
                       videos=_saved_videos(ws))
        await ses.close()
        _sessions.pop(label, None)
        # videos finalize on context close -> scan the workspace AFTER closing
        return _ok(action, label, -1, closed=label, videos=_saved_videos(ws))

    if action in ("tabs", "new_tab", "use_tab", "close_tab"):
        ses, _, err = await _ready(label, ws, need_page=False)
        if err and action != "new_tab":
            return _err(action, label, err)
        if action == "tabs":
            out = []
            for i, p in enumerate(ses.pages):
                try:
                    closed = p.is_closed()
                except Exception:
                    closed = True
                m = await ses.meta(p) if not closed else {"url": "?", "title": "?"}
                out.append({"index": i, "active": i == ses.active,
                            "closed": closed, **m})
            return _ok(action, label, ses.active, tabs=out)
        if action == "new_tab":
            page, err2 = await ses._new_page(kw.get("url", "") or "about:blank")
            if err2:
                return _err(action, label, err2)
            return _ok(action, label, ses.active, opened=kw.get("url", "") or "about:blank",
                       **await ses.meta(page))
        if action == "use_tab":
            want = str(kw.get("index", kw.get("tab", kw.get("url", "")))).strip()
            if want.isdigit():
                i = int(want)
                if 0 <= i < len(ses.pages):
                    ses.active = i
                else:
                    return _err(action, label, f"tab {i} out of range 0..{len(ses.pages) - 1}")
            elif want:
                hit = next((i for i, p in enumerate(ses.pages)
                            if want.lower() in (p.url or "").lower()), None)
                if hit is None:
                    return _err(action, label, f"no tab URL contains '{want}'")
                ses.active = hit
            page, _ = ses.page()
            return _ok(action, label, ses.active, **await ses.meta(page))
        # close_tab
        idx = kw.get("index", kw.get("tab", ""))
        i = ses.active if str(idx).strip() in ("", "active") else -1
        if i == -1:
            try:
                i = int(str(idx).strip())
            except ValueError:
                return _err(action, label, f"bad tab index '{idx}'")
        if not (0 <= i < len(ses.pages)):
            return _err(action, label, f"tab {i} out of range")
        try:
            await ses.pages[i].close()
        except Exception:
            pass
        ses.pages.pop(i)
        ses.active = min(ses.active, max(0, len(ses.pages) - 1))
        return _ok(action, label, ses.active, closed_tab=i, tabs_left=len(ses.pages))

    # -- from here a live tab is required --------------------------
    ses, page, err = await _ready(label, ws, need_page=True)
    if err:
        return _err(action, label, err)
    tab = ses.active

    async def cur_meta() -> dict:
        return await ses.meta(page)

    # -- navigate ---------------------------------------------------
    if action == "goto":
        url = (kw.get("url") or "").strip()
        if not url:
            return _err(action, label, "url is required")
        try:
            resp = await page.goto(
                url, wait_until=(kw.get("wait") or "domcontentloaded") or "domcontentloaded",
                timeout=timeout, referer=kw.get("referer") or None)
            status = resp.status if resp else None
        except Exception as e:
            return _err(action, label, f"goto failed: {e}", **await cur_meta())
        return _ok(action, label, tab, http_status=status, **await cur_meta())

    if action in ("reload", "back", "forward"):
        try:
            if action == "reload":
                await page.reload(wait_until=(kw.get("wait") or "domcontentloaded") or "domcontentloaded",
                                  timeout=timeout)
            elif action == "back":
                await page.go_back(wait_until="domcontentloaded", timeout=timeout)
            else:
                await page.go_forward(wait_until="domcontentloaded", timeout=timeout)
        except Exception as e:
            return _err(action, label, f"{action} failed: {e}", **await cur_meta())
        return _ok(action, label, tab, **await cur_meta())

    # -- observe ----------------------------------------------------
    if action == "snapshot":
        # aria ai-mode: compact YAML tree with [ref=eN] visual anchors.
        # (aria-ref= selectors are NOT resolvable by Playwright itself, so
        # actions still use CSS/XPath/text= selectors.)
        txt, how = "", "aria"
        try:
            try:
                txt = await page.aria_snapshot(mode="ai", timeout=timeout)
            except TypeError:
                txt = await page.aria_snapshot(timeout=timeout)
        except Exception as e:
            how = f"aria_failed:{e}"
        if not txt and hasattr(page, "accessibility"):
            try:
                tree = await page.accessibility.snapshot()
                txt = "\n".join(_compact_ax(tree))
                how = "accessibility"
            except Exception:
                pass
        if not txt:
            return _err(action, label, f"snapshot failed ({how})", **await cur_meta())
        txt = txt or "(empty page)"
        cut = len(txt) > SNAP_MAX
        return _ok(action, label, tab, snapshot=_short(txt, SNAP_MAX),
                   truncated=cut, mode=how,
                   hint="[ref=eN] tags are visual anchors only — act with CSS selectors",
                   **await cur_meta())

    if action == "text":
        try:
            txt = await (page.locator(sel).inner_text(timeout=timeout)
                         if sel else page.inner_text("body", timeout=timeout))
        except Exception as e:
            return _err(action, label, f"text failed: {e}", **await cur_meta())
        return _ok(action, label, tab, text=_short(txt or "", TEXT_MAX),
                   truncated=len(txt or "") > TEXT_MAX, **await cur_meta())

    if action == "html":
        try:
            raw = await (page.locator(sel).inner_html(timeout=timeout)
                         if sel else page.content())
        except Exception as e:
            return _err(action, label, f"html failed: {e}", **await cur_meta())
        return _ok(action, label, tab, html=_short(raw or "", HTML_MAX),
                   truncated=len(raw or "") > HTML_MAX, **await cur_meta())

    if action == "title":
        return _ok(action, label, tab, **await cur_meta())

    if action == "find":
        # Log-found fix: models kept calling a hallucinated `web_find` tool.
        # Give them real in-page find: count + scroll + context snippet.
        # Log-found fix 2: models pass selector="Some Text" or url="..." with
        # find — accept selector as a text alias and auto-navigate when a url
        # is supplied (10+ FAILs in nexus.log from this exact misuse).
        needle = (kw.get("text", kw.get("query", kw.get("selector", ""))) or "")
        if isinstance(needle, str):
            needle = needle.strip()
        if not needle:
            return _err(action, label, "text is required (pass text/query, or selector text to search for)",
                        hint="find searches VISIBLE TEXT, not CSS — e.g. {\"action\":\"find\",\"text\":\"Islamabad\"}",
                        **await cur_meta())
        want_url = (kw.get("url") or "").strip()
        if want_url:
            try:
                cur = (page.url or "")
            except Exception:
                cur = ""
            if want_url not in cur:
                try:
                    await page.goto(want_url,
                                    wait_until=(kw.get("wait") or "domcontentloaded") or "domcontentloaded",
                                    timeout=timeout)
                except Exception as e:
                    return _err(action, label, f"find goto failed: {e}", **await cur_meta())
        nth = max(1, int(kw.get("nth", 1) or 1))
        js = """([needle, nth]) => {
          const low = String(needle).toLowerCase();
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          let count = 0, target = null;
          let node;
          while ((node = walker.nextNode())) {
            const tl = (node.nodeValue || "").toLowerCase();
            let from = 0, i;
            while ((i = tl.indexOf(low, from)) >= 0) {
              count++;
              if (count === nth && !target) target = node;
              from = i + low.length;
              if (count > 500) break;
            }
            if (count > 500) break;
          }
          let context = "", tag = "";
          if (target && target.parentElement) {
            const el = target.parentElement;
            tag = el.tagName.toLowerCase();
            try { el.scrollIntoView({block: "center"}); } catch (e) {}
            const full = (el.innerText || "").trim().replace(/\\s+/g, " ");
            const at = Math.max(0, full.toLowerCase().indexOf(low) - 60);
            context = full.slice(at, at + 220);
          }
          return {count: Math.min(count, 501), nth, tag, context};
        }"""
        try:
            res = await page.evaluate(js, [needle, nth])
        except Exception as e:
            return _err(action, label, f"find failed: {e}", **await cur_meta())
        if not isinstance(res, dict):
            res = {"count": 0}
        return _ok(action, label, tab, needle=needle, matches=res.get("count", 0),
                   nth=nth if res.get("count", 0) >= nth else 0,
                   tag=res.get("tag", ""), context=_short(res.get("context", ""), 400),
                   **await cur_meta())

    if action == "url":
        return _ok(action, label, tab, **await cur_meta())

    if action == "console":
        n = int(kw.get("last", 30) or 30)
        out = (ses.console_log + [{"pageerror": e["error"]} for e in ses.page_errs])[-n:]
        if kw.get("clear"):
            ses.console_log, ses.page_errs = [], []
        return _ok(action, label, tab, entries=out, **await cur_meta())

    if action == "network":
        n = int(kw.get("last", 40) or 40)
        only = (kw.get("only", "") or "").lower()
        logs = ses.net_log[-n * 2:]
        if only:
            logs = [e for e in logs if only in (e.get("url", "") or "").lower()
                    or only in str(e.get("status", "") or "")]
        if kw.get("clear"):
            ses.net_log = []
        return _ok(action, label, tab, entries=logs[-n:], **await cur_meta())

    if action == "dialogs":
        return _ok(action, label, tab, entries=ses.dialog_log[-int(kw.get("last", 10) or 10):],
                   **await cur_meta())

    if action == "downloads":
        return _ok(action, label, tab, entries=ses.downloads[-20:], **await cur_meta())

    if action == "videos":
        return _ok(action, label, tab, entries=_saved_videos(ws), **await cur_meta())

    # -- act ---------------------------------------------------------
    if action in ("click", "dblclick", "hover", "focus"):
        x, y = kw.get("x"), kw.get("y")
        try:
            if x is not None and y is not None:
                if action == "click":
                    await page.mouse.click(int(x), int(y),
                                           button=kw.get("button", "left") or "left",
                                           click_count=int(kw.get("click_count", 1) or 1))
                elif action == "dblclick":
                    await page.mouse.dblclick(int(x), int(y))
                elif action == "hover":
                    await page.mouse.move(int(x), int(y))
                else:
                    return _err(action, label, "focus needs a selector, not x/y")
            else:
                el = loc(page)
                if action == "click":
                    await el.click(timeout=timeout, button=kw.get("button", "left") or "left",
                                   click_count=int(kw.get("click_count", 1) or 1),
                                   modifiers=_mods(kw.get("modifiers", "")))
                elif action == "dblclick":
                    await el.dblclick(timeout=timeout)
                elif action == "hover":
                    await el.hover(timeout=timeout)
                else:
                    await el.focus(timeout=timeout)
        except Exception as e:
            return _err(action, label, f"{action} failed: {e}", **await cur_meta())
        return _ok(action, label, tab, **await cur_meta())

    if action in ("fill", "type"):
        text = kw.get("text", "")
        if text is None:
            return _err(action, label, "text is required")
        try:
            el = loc(page)
            if action == "fill":
                await el.fill(str(text), timeout=timeout)
            else:
                await el.press_sequentially(str(text), delay=int(kw.get("delay_ms", 0) or 0),
                                            timeout=timeout)
        except Exception as e:
            return _err(action, label, f"{action} failed: {e}", **await cur_meta())
        return _ok(action, label, tab, **await cur_meta())

    if action == "press":
        key = (kw.get("key") or "").strip()
        if not key:
            return _err(action, label, "key is required (e.g. Enter, Tab, Control+a, Escape)")
        try:
            if sel:
                await loc(page).press(key, timeout=timeout)
            else:
                await page.keyboard.press(key)
        except Exception as e:
            return _err(action, label, f"press failed: {e}", **await cur_meta())
        return _ok(action, label, tab, **await cur_meta())

    if action == "select":
        vals = kw.get("values", kw.get("value", ""))
        if isinstance(vals, str):
            vals = [vals]
        if not vals:
            return _err(action, label, "values is required (string or list)")
        try:
            picked = await loc(page).select_option(vals, timeout=timeout)
        except Exception as e:
            return _err(action, label, f"select failed: {e}", **await cur_meta())
        return _ok(action, label, tab, picked=picked, **await cur_meta())

    if action in ("check", "uncheck"):
        try:
            el = loc(page)
            if action == "check":
                await el.check(timeout=timeout)
            else:
                await el.uncheck(timeout=timeout)
        except Exception as e:
            return _err(action, label, f"{action} failed: {e}", **await cur_meta())
        return _ok(action, label, tab, **await cur_meta())

    if action == "set_file":
        files = kw.get("files", kw.get("paths", ""))
        if isinstance(files, str):
            files = [files]
        if not files:
            return _err(action, label, "files is required (path or list, relative to workspace ok)")
        resolved = []
        for f in files:
            p = Path(f)
            if not p.is_absolute():
                p = ws / f
            if not p.exists():
                return _err(action, label, f"upload file not found: {f} (looked in {p})")
            resolved.append(str(p))
        try:
            await loc(page).set_input_files(resolved, timeout=timeout)
        except Exception as e:
            return _err(action, label, f"set_file failed: {e}", **await cur_meta())
        return _ok(action, label, tab, uploaded=resolved, **await cur_meta())

    if action == "drag":
        src = (kw.get("from_selector", kw.get("from", "")) or sel).strip()
        dst = (kw.get("to_selector", kw.get("to", "")) or "").strip()
        try:
            if src and (dst or (kw.get("to_x") is not None)):
                if dst:
                    await page.locator(src).drag_to(page.locator(dst), timeout=timeout)
                else:
                    el = page.locator(src)
                    box = await el.bounding_box()
                    if not box:
                        return _err(action, label, "drag source not visible")
                    sx, sy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                    await page.mouse.move(sx, sy)
                    await page.mouse.down()
                    await page.mouse.move(int(kw["to_x"]), int(kw["to_y"]),
                                          steps=int(kw.get("steps", 12) or 12))
                    await page.mouse.up()
            elif kw.get("from_x") is not None:
                await page.mouse.move(int(kw["from_x"]), int(kw["from_y"]))
                await page.mouse.down()
                await page.mouse.move(int(kw["to_x"]), int(kw["to_y"]),
                                      steps=int(kw.get("steps", 12) or 12))
                await page.mouse.up()
            else:
                return _err(action, label, "drag needs from_selector (+to_selector or to_x/to_y) "
                                           "or from_x/from_y/to_x/to_y")
        except Exception as e:
            return _err(action, label, f"drag failed: {e}", **await cur_meta())
        return _ok(action, label, tab, **await cur_meta())

    if action == "mouse":
        op = (kw.get("op") or "click").strip().lower()
        try:
            m = page.mouse
            if op == "click":
                await m.click(int(kw.get("x", 0)), int(kw.get("y", 0)),
                              button=kw.get("button", "left") or "left")
            elif op == "move":
                await m.move(int(kw.get("x", 0)), int(kw.get("y", 0)),
                             steps=int(kw.get("steps", 1) or 1))
            elif op == "down":
                await m.down(button=kw.get("button", "left") or "left")
            elif op == "up":
                await m.up(button=kw.get("button", "left") or "left")
            elif op == "wheel":
                await m.wheel(int(kw.get("dx", 0) or 0), int(kw.get("dy", 0) or 0))
            elif op == "dblclick":
                await m.dblclick(int(kw.get("x", 0)), int(kw.get("y", 0)))
            else:
                return _err(action, label, f"unknown mouse op '{op}' (click|move|down|up|wheel|dblclick)")
        except Exception as e:
            return _err(action, label, f"mouse failed: {e}", **await cur_meta())
        return _ok(action, label, tab, **await cur_meta())

    if action == "scroll":
        try:
            if sel:
                await loc(page).scroll_into_view_if_needed(timeout=timeout)
            else:
                direction = (kw.get("direction") or "").strip().lower()
                amount = int(kw.get("amount", kw.get("dy", 600)) or 600)
                dx = int(kw.get("dx", 0) or 0)
                if direction == "up":
                    await page.mouse.wheel(dx, -abs(amount))
                elif direction == "down":
                    await page.mouse.wheel(dx, abs(amount))
                elif direction == "top":
                    await page.evaluate("window.scrollTo(0,0)")
                elif direction == "bottom":
                    await page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                else:
                    await page.mouse.wheel(dx, int(kw.get("dy", amount) or amount))
        except Exception as e:
            return _err(action, label, f"scroll failed: {e}", **await cur_meta())
        return _ok(action, label, tab, **await cur_meta())

    if action == "eval":
        js = kw.get("js", kw.get("expression", kw.get("script", "")))
        if not js:
            return _err(action, label, "js is required (expression or function body)")
        try:
            import json as _json
            arg = kw.get("arg")
            if sel:
                res = await page.locator(sel).evaluate(js, arg)
            else:
                res = await page.evaluate(js, arg)
        except Exception as e:
            return _err(action, label, f"eval failed: {e}", **await cur_meta())
        try:
            shown = _json.dumps(res, ensure_ascii=False, default=str)
        except Exception:
            shown = str(res)
        return _ok(action, label, tab, result=_short(shown, 8000),
                   truncated=len(shown) > 8000, **await cur_meta())

    if action == "wait":
        try:
            if kw.get("time_ms") is not None or kw.get("ms") is not None:
                ms = int(kw.get("time_ms", kw.get("ms")) or 0)
                await page.wait_for_timeout(min(max(ms, 0), 120000))
                return _ok(action, label, tab, waited_ms=ms, **await cur_meta())
            if kw.get("url_glob") or kw.get("url"):
                await page.wait_for_url(kw.get("url_glob") or kw.get("url"),
                                        timeout=timeout)
                return _ok(action, label, tab, **await cur_meta())
            if kw.get("text"):
                await page.locator(f"text={kw['text']}").first.wait_for(timeout=timeout)
                return _ok(action, label, tab, **await cur_meta())
            if kw.get("load_state"):
                await page.wait_for_load_state(kw["load_state"], timeout=timeout)
                return _ok(action, label, tab, **await cur_meta())
            if sel:
                state = (kw.get("state") or "visible").strip().lower()
                if state not in ("visible", "hidden", "attached", "detached"):
                    return _err(action, label, f"bad state '{state}'")
                await loc(page).first.wait_for(state=state, timeout=timeout)
                return _ok(action, label, tab, **await cur_meta())
            return _err(action, label, "wait needs time_ms | selector(+state) | text | url_glob | load_state")
        except Exception as e:
            return _err(action, label, f"wait failed: {e}", **await cur_meta())

    # -- capture ------------------------------------------------------
    if action == "screenshot":
        global _shot_seq
        _shot_seq += 1
        name = (kw.get("name") or f"shot_{_stamp()}_{_shot_seq}").strip().strip("/")
        if not name.lower().endswith(".png"):
            name += ".png"
        dest = ws / "shots" / Path(name).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if sel:
                await page.locator(sel).screenshot(path=str(dest), timeout=timeout)
            else:
                await page.screenshot(path=str(dest),
                                      full_page=bool(kw.get("full_page", False)),
                                      timeout=timeout)
        except Exception as e:
            return _err(action, label, f"screenshot failed: {e}", **await cur_meta())
        return _ok(action, label, tab, path=str(dest),
                   bytes=dest.stat().st_size if dest.exists() else 0,
                   note="image saved to the session workspace (text models can't view it, "
                        "but it is kept for you/the user)",
                   **await cur_meta())

    if action == "pdf":
        name = (kw.get("name") or f"page_{_stamp()}").strip().strip("/")
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        dest = ws / "pdf" / Path(name).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            await page.pdf(path=str(dest), format=kw.get("format", "A4") or "A4")
        except Exception as e:
            return _err(action, label, f"pdf failed: {e} "
                                       f"(headless-shell builds may lack printing — "
                                       f"try browser chromium headed or another action)",
                        **await cur_meta())
        return _ok(action, label, tab, path=str(dest),
                   bytes=dest.stat().st_size if dest.exists() else 0,
                   **await cur_meta())

    # -- state ---------------------------------------------------------
    if action == "cookies":
        op = (kw.get("op") or "get").strip().lower()
        try:
            if op == "get":
                urls = kw.get("urls")
                ck = await ses.context.cookies(urls if isinstance(urls, list) else None)
                return _ok(action, label, tab, cookies=ck, **await cur_meta())
            if op == "set":
                data = kw.get("cookies", kw.get("cookie"))
                if isinstance(data, dict):
                    data = [data]
                if not data:
                    return _err(action, label, "cookies list (or cookie dict) is required for set")
                await ses.context.add_cookies(data)
                return _ok(action, label, tab, set=len(data), **await cur_meta())
            if op == "clear":
                await ses.context.clear_cookies()
                return _ok(action, label, tab, cleared=True, **await cur_meta())
            return _err(action, label, f"unknown cookies op '{op}' (get|set|clear)")
        except Exception as e:
            return _err(action, label, f"cookies failed: {e}", **await cur_meta())

    if action == "storage":
        op = (kw.get("op") or "get").strip().lower()
        area = (kw.get("area") or "local").strip().lower()
        if area not in ("local", "session"):
            return _err(action, label, "area must be local|session")
        key = kw.get("key", "")
        try:
            if op == "get":
                if key:
                    val = await page.evaluate(
                        f"window.{area}Storage.getItem({json.dumps(key)})")
                    return _ok(action, label, tab, key=key, value=val, **await cur_meta())
                dump = await page.evaluate(
                    f"Object.fromEntries(Object.entries(window.{area}Storage))")
                return _ok(action, label, tab, storage=_short(dump, 6000), **await cur_meta())
            if op == "set":
                if not key:
                    return _err(action, label, "key is required for storage set")
                await page.evaluate(
                    f"window.{area}Storage.setItem({json.dumps(key)}, "
                    f"{json.dumps(str(kw.get('value', '')))})")
                return _ok(action, label, tab, key=key, set=True, **await cur_meta())
            if op == "clear":
                await page.evaluate(f"window.{area}Storage.clear()")
                return _ok(action, label, tab, cleared=True, **await cur_meta())
            return _err(action, label, f"unknown storage op '{op}' (get|set|clear)")
        except Exception as e:
            return _err(action, label, f"storage failed: {e}", **await cur_meta())

    if action == "state_save":
        name = (kw.get("name") or "state.json").strip().strip("/")
        dest = ws / Path(name).name
        try:
            await ses.context.storage_state(path=str(dest))
        except Exception as e:
            return _err(action, label, f"state_save failed: {e}", **await cur_meta())
        return _ok(action, label, tab, path=str(dest),
                   reuse="launch with storage_state=<that path> to restore login",
                   **await cur_meta())

    if action == "viewport":
        try:
            await page.set_viewport_size({"width": int(kw.get("w", kw.get("width", 1366))),
                                          "height": int(kw.get("h", kw.get("height", 900)))})
        except Exception as e:
            return _err(action, label, f"viewport failed: {e}", **await cur_meta())
        return _ok(action, label, tab, **await cur_meta())

    if action == "offline":
        try:
            await ses.context.set_offline(bool(kw.get("enabled", True)))
        except Exception as e:
            return _err(action, label, f"offline failed: {e}", **await cur_meta())
        return _ok(action, label, tab, offline=bool(kw.get("enabled", True)),
                   **await cur_meta())

    if action == "headers":
        try:
            await ses.context.set_extra_http_headers(kw.get("headers") or {})
        except Exception as e:
            return _err(action, label, f"headers failed: {e}", **await cur_meta())
        return _ok(action, label, tab, headers=kw.get("headers") or {},
                   **await cur_meta())

    if action == "block":
        types = kw.get("types", kw.get("type", ""))
        if isinstance(types, str):
            types = [t.strip().lower() for t in types.split(",") if t.strip()]
        exts = {"image": ["png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "avif"],
                "font": ["woff", "woff2", "ttf", "otf", "eot"],
                "media": ["mp4", "webm", "mp3", "wav", "ogg", "m3u8"],
                "style": ["css"],
                "script": ["js"]}
        pats = []
        for tp in types:
            pats += [f"**/*.{e}" for e in exts.get(tp, [tp])]
        if kw.get("pattern"):
            pats.append(kw["pattern"])
        if not pats:
            return _err(action, label, "block needs types (image,font,media,style,script) and/or pattern")
        try:
            for pat in pats:
                await ses.context.route(pat, lambda route: asyncio.ensure_future(route.abort()))
        except Exception as e:
            return _err(action, label, f"block failed: {e}", **await cur_meta())
        return _ok(action, label, tab, blocked=pats, **await cur_meta())

    if action == "unblock":
        try:
            await ses.context.unroute_all(behavior="wait")
        except TypeError:
            try:
                await ses.context.unroute_all()
            except Exception as e:
                return _err(action, label, f"unblock failed: {e}", **await cur_meta())
        except Exception as e:
            return _err(action, label, f"unblock failed: {e}", **await cur_meta())
        return _ok(action, label, tab, unblocked=True, **await cur_meta())

    if action == "trace_start":
        try:
            await ses.context.tracing.start(screenshots=bool(kw.get("screenshots", True)),
                                            snapshots=bool(kw.get("snapshots", True)),
                                            sources=bool(kw.get("sources", False)))
        except Exception as e:
            return _err(action, label, f"trace_start failed: {e}", **await cur_meta())
        return _ok(action, label, tab, tracing=True, **await cur_meta())

    if action == "trace_stop":
        name = (kw.get("name") or f"trace_{_stamp()}").strip().strip("/")
        if not name.lower().endswith(".zip"):
            name += ".zip"
        dest = ws / "traces" / Path(name).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            await ses.context.tracing.stop(path=str(dest))
        except Exception as e:
            return _err(action, label, f"trace_stop failed: {e} (was trace_start called?)",
                        **await cur_meta())
        return _ok(action, label, tab, path=str(dest),
                   view="open with: playwright show-trace <path>", **await cur_meta())

    if action == "request":
        url = (kw.get("url") or "").strip()
        if not url:
            return _err(action, label, "url is required")
        method = (kw.get("method") or "GET").strip().upper()
        fetch_kw: Dict[str, Any] = {"timeout": timeout}
        if kw.get("headers"):
            fetch_kw["headers"] = kw["headers"]
        if kw.get("data") is not None:
            fetch_kw["data"] = kw["data"]
        try:
            resp = await ses.context.request.fetch(url, method=method, **fetch_kw)
            body = await resp.body()
            try:
                text = body.decode("utf-8", "replace")
            except Exception:
                text = f"<{len(body)} binary bytes>"
        except Exception as e:
            return _err(action, label, f"request failed: {e}", **await cur_meta())
        return _ok(action, label, tab, status=resp.status,
                   headers=dict(resp.headers), body=_short(text, 8000),
                   truncated=len(text) > 8000)

    return _err(action, label,
                f"unknown browser action '{action}'. "
                f"Groups: launch/close/tabs/new_tab/use_tab/close_tab, "
                f"goto/reload/back/forward, snapshot/text/html/find/title/url/console/network/dialogs/downloads/videos, "
                f"click/dblclick/hover/focus/fill/type/press/select/check/uncheck/set_file/drag/mouse/scroll/eval/wait, "
                f"screenshot/pdf, cookies/storage/state_save/viewport/offline/headers/block/unblock/trace_start/trace_stop/request",
                **await cur_meta())


def _saved_videos(ws: Path) -> List[dict]:
    """Videos live in the workspace dir, so they survive session close."""
    out = []
    try:
        vd = ws / "videos"
        if vd.exists():
            for f in sorted(vd.glob("*.webm")):
                try:
                    out.append({"name": f.name, "path": str(f), "size": f.stat().st_size})
                except Exception:
                    pass
    except Exception:
        pass
    return out


def _mods(raw: Any) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [m.strip() for m in raw.split(",")]
    ok = {"Alt", "Control", "ControlOrMeta", "Meta", "Shift"}
    return [m for m in raw if m in ok]


# ---------------------------------------------------------------- LLM spec

TOOL_SPEC_BROWSER = {
    "type": "function",
    "function": {
        "name": "browser",
        "description": (
            "Full real-browser automation (Playwright: chromium/firefox/webkit). "
            "Workflow: goto a page, snapshot to SEE it (aria tree with refs), then "
            "act with CSS selectors (click/fill/type/press/select/check/scroll), "
            "eval JavaScript, screenshot/pdf, read console+network, manage "
            "cookies/storage/tabs/downloads, block resources, trace, API requests. "
            "State persists across calls (tabs stay open). Prefer snapshot before "
            "every action; it is the browser's eyes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": (
                        "launch|close|tabs|new_tab|use_tab|close_tab|goto|reload|back|forward|"
                        "snapshot|text|html|find|title|url|console|network|dialogs|downloads|videos|"
                        "click|dblclick|hover|focus|fill|type|press|select|check|uncheck|set_file|"
                        "drag|mouse|scroll|eval|wait|screenshot|pdf|"
                        "cookies|storage|state_save|viewport|offline|headers|block|unblock|"
                        "trace_start|trace_stop|request"
                    ),
                },
                "session": {"type": "string", "description": "Named browser session (default main)"},
                "selector": {"type": "string", "description": "CSS/XPath/text= selector (click/fill/etc). For find, selector text is also accepted as the search needle."},
                "url": {"type": "string", "description": "URL for goto/new_tab/request. find also auto-navigates when url is given."},
                "text": {"type": "string", "description": "Text for fill/type/wait, and the search needle for find (query is an alias)"},
                "query": {"type": "string", "description": "Alias of text for find"},
                "key": {"type": "string", "description": "Key for press (Enter, Tab, Control+a...)"},
                "js": {"type": "string", "description": "JavaScript for eval (expression or body)"},
                "arg": {"description": "Optional JSON arg passed into eval"},
                "browser": {"type": "string", "description": "chromium|firefox|webkit (launch)"},
                "headless": {"type": "boolean", "description": "Headless on/off (launch)"},
                "timeout": {"type": "integer", "description": "Action timeout ms (default 15000)"},
            },
            "required": ["action"],
        },
    },
}
