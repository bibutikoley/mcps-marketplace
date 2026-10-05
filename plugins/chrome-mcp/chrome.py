"""Backend: drive the user's real Chrome over CDP (remote-debugging port).

No extension, no separate browser process, no outbound network: everything
is local HTTP to ``127.0.0.1:<port>/json`` plus CDP over a minimal stdlib
WebSocket client. Bookmarks/History come from the Chrome profile on disk.

Start Chrome once with a debugger port (or set CHROME_MCP_PORT to match)::

    google-chrome --remote-debugging-port=9222

Every public helper raises :class:`ChromeError` on failure so ``main.py``
can render ``CallToolResult`` errors uniformly. Functions that touch the
network/browser are small and mockable (tests patch ``_http_get_json`` /
``cdp_call`` / ``evaluate``).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import socket
import sqlite3
import struct
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ChromeError(Exception):
    """All backend failures surface as this (caught in main.py)."""


# ---- config ---------------------------------------------------------------

_HOST = os.environ.get("CHROME_MCP_HOST", "127.0.0.1")
_PORT = int(os.environ.get("CHROME_MCP_PORT", "9222"))
_TIMEOUT_MS = int(os.environ.get("CHROME_MCP_TIMEOUT_MS", "15000"))
_DATA_DIR = Path(os.environ.get("CHROME_MCP_DATA_DIR", "") or "")
_DOWNLOAD_DIR = Path(os.environ.get("CHROME_MCP_DOWNLOAD_DIR", "") or "")

_URL_RE = re.compile(r"^https?://[^/\s]+.*$", re.IGNORECASE)
_SELECTOR_MAX = 1000
_COORD_MAX = 10000

_STATIC_EXTS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".css",
    ".map",
)

_NET_SESSIONS: dict[str, dict[str, Any]] = {}
_TRACE_SESSIONS: dict[str, dict[str, Any]] = {}
_GIF_SESSIONS: dict[str, dict[str, Any]] = {}


def debug_base() -> str:
    return f"http://{_HOST}:{_PORT}"


def timeout_s(override_ms: int | None = None) -> float:
    ms = override_ms if override_ms is not None else _TIMEOUT_MS
    return max(1.0, min(120.0, ms / 1000.0))


def data_dir() -> Path:
    if _DATA_DIR:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        return _DATA_DIR
    d = Path(tempfile.gettempdir()) / "chrome-mcp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_dir() -> Path:
    if _DOWNLOAD_DIR:
        return _DOWNLOAD_DIR
    return Path.home() / "Downloads"


# ---- validation -----------------------------------------------------------


def require_http_url(url: str, field: str = "url") -> str:
    url = url.strip()
    if not _URL_RE.match(url):
        raise ChromeError(f"{field} must be an http(s) URL, got {url!r}")
    if len(url) > 8000:
        raise ChromeError(f"{field} too long (max 8000 chars)")
    return url


def require_selector(selector: str) -> str:
    if not selector or len(selector) > _SELECTOR_MAX:
        raise ChromeError("selector must be 1..1000 chars")
    return selector


def require_coords(x: int, y: int) -> tuple[int, int]:
    if not (0 <= x <= _COORD_MAX and 0 <= y <= _COORD_MAX):
        raise ChromeError(f"coordinates out of range 0..{_COORD_MAX}")
    return x, y


# ---- raw HTTP to the debugger ---------------------------------------------


def _http_get_json(path: str, timeout: float = 10.0) -> Any:
    try:
        with urllib.request.urlopen(debug_base() + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except ChromeError:
        raise
    except Exception as e:
        raise ChromeError(
            f"Chrome debugger unreachable at {debug_base()} "
            f"(launch with --remote-debugging-port={_PORT}): {e}"
        ) from e


def _http_get(path: str, timeout: float = 10.0) -> bytes:
    try:
        with urllib.request.urlopen(debug_base() + path, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        raise ChromeError(f"debugger request {path} failed: {e}") from e


# ---- minimal WebSocket client (stdlib only) --------------------------------


def _ws_send_frame(sock: socket.socket, payload: bytes) -> None:
    mask = os.urandom(4)
    frame = bytearray([0x81])
    if len(payload) < 126:
        frame.append(0x80 | len(payload))
    else:
        frame.append(0x80 | 126)
        frame += struct.pack("!H", len(payload))
    frame += mask
    frame += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    sock.sendall(bytes(frame))


def _ws_recv_frame(sock: socket.socket, deadline: float) -> tuple[int, bytes] | None:
    sock.settimeout(max(0.1, deadline - time.time()))
    try:
        hdr = sock.recv(2)
    except (socket.timeout, OSError):
        return None
    if len(hdr) < 2:
        return None
    opcode = hdr[0] & 0x0F
    length = hdr[1] & 0x7F
    if length == 126:
        ext = sock.recv(2)
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = sock.recv(8)
        length = struct.unpack("!Q", ext)[0]
    if hdr[1] & 0x80:
        mask = sock.recv(4)
    else:
        mask = b""
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            break
        data += chunk
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return opcode, data


def _ws_connect(ws_url: str, timeout: float) -> socket.socket:
    parts = urllib.parse.urlparse(ws_url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
        f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    sock.settimeout(timeout)
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            break
        resp += chunk
    if b"101" not in resp.split(b"\r\n", 1)[0]:
        sock.close()
        raise ChromeError(f"websocket handshake failed for {ws_url}")
    accept_expect = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    )
    if accept_expect not in resp:
        sock.close()
        raise ChromeError("websocket accept key mismatch")
    return sock


def cdp_call(
    ws_url: str,
    method: str,
    params: dict[str, Any] | None = None,
    timeout_ms: int | None = None,
) -> Any:
    """Single CDP round-trip: send command, wait for the matching id."""
    timeout = timeout_s(timeout_ms)
    sock = _ws_connect(ws_url, timeout)
    try:
        msg_id = 1
        _ws_send_frame(
            sock, json.dumps({"id": msg_id, "method": method, "params": params or {}}).encode()
        )
        deadline = time.time() + timeout
        while time.time() < deadline:
            frame = _ws_recv_frame(sock, deadline)
            if frame is None:
                continue
            opcode, data = frame
            if opcode == 0x8:
                break
            if opcode == 0x9:
                _ws_send_frame(sock, b"")
                continue
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(data.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise ChromeError(f"CDP {method} failed: {msg['error']}")
                return msg.get("result", {})
        raise ChromeError(f"CDP {method} timed out after {timeout:.0f}s")
    finally:
        try:
            sock.close()
        except OSError:
            pass


def cdp_collect(
    ws_url: str,
    enables: list[tuple[str, dict[str, Any]]],
    want_prefixes: tuple[str, ...],
    duration_s: float,
) -> list[dict[str, Any]]:
    """Enable domains, collect matching events for duration_s seconds."""
    timeout = max(1.0, min(30.0, duration_s + 5.0))
    sock = _ws_connect(ws_url, timeout)
    events: list[dict[str, Any]] = []
    try:
        for i, (method, params) in enumerate(enables, start=1):
            _ws_send_frame(sock, json.dumps({"id": i, "method": method, "params": params}).encode())
        deadline = time.time() + duration_s
        end = deadline + 2.0
        while time.time() < deadline:
            if time.time() >= deadline:
                break
            frame = _ws_recv_frame(sock, deadline + 2.0)
            if frame is None:
                if time.time() >= end:
                    break
                continue
            opcode, data = frame
            if opcode == 0x8:
                break
            if opcode == 0x9:
                _ws_send_frame(sock, b"")
                continue
            if opcode != 0x1:
                continue
            try:
                msg = json.loads(data.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if "method" in msg and str(msg["method"]).startswith(want_prefixes):
                events.append(msg)
        return events
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---- targets ---------------------------------------------------------------


def targets() -> list[dict[str, Any]]:
    items = _http_get_json("/json/list")
    if not isinstance(items, list):
        raise ChromeError("unexpected /json/list response")
    return [t for t in items if isinstance(t, dict) and t.get("type") == "page"]


def resolve_target(
    tab_id: int | None = None,
    target_id: str | None = None,
    url_contains: str | None = None,
) -> dict[str, Any]:
    pages = targets()
    if not pages:
        raise ChromeError("no open pages (is Chrome running?)")
    if target_id:
        for t in pages:
            if t.get("id") == target_id:
                return t
        raise ChromeError(f"no tab with target id {target_id!r}")
    if tab_id is not None:
        # tabId here is the 1-based index shown by get_windows_and_tabs,
        # kept stable across calls by sorting on target id.
        ordered = sorted(pages, key=lambda t: str(t.get("id")))
        if not (1 <= tab_id <= len(ordered)):
            raise ChromeError(f"tabId {tab_id} out of range (1..{len(ordered)})")
        return ordered[tab_id - 1]
    if url_contains:
        for t in pages:
            if url_contains in str(t.get("url", "")):
                return t
        raise ChromeError(f"no tab matching {url_contains!r}")
    # default: active tab = first with a debugger URL
    for t in pages:
        if t.get("webSocketDebuggerUrl"):
            return t
    raise ChromeError("active tab has no debugger URL")


def ws_url_for(target: dict[str, Any]) -> str:
    ws = target.get("webSocketDebuggerUrl")
    if not ws:
        raise ChromeError("target is not debuggable (chrome:// pages cannot attach)")
    return str(ws)


def health() -> dict[str, Any]:
    try:
        pages = targets()
        version = _http_get_json("/json/version")
    except ChromeError as e:
        return {"ok": False, "error": str(e), "host": _HOST, "port": _PORT}
    info = version if isinstance(version, dict) else {}
    return {
        "ok": True,
        "host": _HOST,
        "port": _PORT,
        "pages": len(pages),
        "browser": info.get("Browser", "?"),
        "protocol": info.get("Protocol-Version", "?"),
    }


def activate_target(target: dict[str, Any]) -> None:
    _http_get("/json/activate/" + urllib.parse.quote(str(target.get("id", ""))))


def new_tab(url: str) -> dict[str, Any]:
    require_http_url(url)
    qs = urllib.parse.urlencode({"url": url})
    try:
        with urllib.request.urlopen(
            debug_base() + "/json/new?" + qs,
            timeout=10.0,
        ) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        raise ChromeError(f"open tab failed: {e}") from e


def close_target(target: dict[str, Any]) -> None:
    _http_get("/json/close/" + urllib.parse.quote(str(target.get("id", ""))))


# ---- evaluate / screenshot -------------------------------------------------


def evaluate(
    target: dict[str, Any],
    expression: str,
    await_promise: bool = False,
    timeout_ms: int = 15000,
) -> Any:
    if len(expression) > 200_000:
        raise ChromeError("expression too long (max 200k chars)")
    res = cdp_call(
        ws_url_for(target),
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
            "userGesture": True,
        },
        timeout_ms,
    )
    exc = res.get("exceptionDetails")
    if exc:
        text = str(exc.get("exception", {}).get("description", exc))[:2000]
        raise ChromeError(f"page threw: {text}")
    result = res.get("result", {})
    if result.get("type") == "undefined":
        return None
    if "value" in result:
        return result["value"]
    return result.get("description")


def screenshot(target: dict[str, Any], full_page: bool = True) -> bytes:
    res = cdp_call(
        ws_url_for(target),
        "Page.captureScreenshot",
        {
            "format": "png",
            "captureBeyondViewport": full_page,
        },
    )
    data = res.get("data", "")
    try:
        raw = base64.b64decode(data)
    except Exception as e:
        raise ChromeError(f"screenshot decode failed: {e}") from e
    if not raw:
        raise ChromeError("empty screenshot")
    return raw


# ---- page model (refs like the extension's read_page) ----------------------

_READ_JS = """(() => {
  const els = [...document.querySelectorAll(
    'a,button,input,select,textarea,[role=button],[role=link],[onclick]')];
  const out = []; const seen = new Set();
  const vw = {w: innerWidth, h: innerHeight, x: scrollX, y: scrollY};
  let n = 0;
  for (const el of els) {
    if (n >= 200) break;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    if (r.bottom < 0 || r.top > innerHeight) continue;
    const label = (el.innerText || el.value || el.getAttribute('aria-label')
      || el.getAttribute('placeholder') || el.tagName).trim().slice(0, 80);
    const key = el.tagName + '|' + label + '|' + Math.round(r.x) + ',' + Math.round(r.y);
    if (seen.has(key)) continue; seen.add(key); n++;
    out.push({ref: 'ref_' + n, tag: el.tagName.toLowerCase(),
      label, x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.h ?? r.height)});
  }
  return {title: document.title, url: location.href, viewport: vw, refs: out};
})()"""


def read_page(target: dict[str, Any], interactive_only: bool = True) -> dict[str, Any]:
    del interactive_only  # selector already targets interactive elements
    try:
        data = evaluate(target, _READ_JS)
    except ChromeError as e:
        raise ChromeError(f"read_page failed (page may block scripting): {e}") from e
    if not isinstance(data, dict):
        raise ChromeError("read_page got no page model")
    refs = data.get("refs", [])
    lines = [f"# {data.get('title', '?')} <{data.get('url', '?')}>"]
    for r in refs:
        lines.append(
            f"- [{r.get('ref')}] <{r.get('tag')}> {r.get('label')!r} at ({r.get('x')},{r.get('y')})"
        )
    return {
        "title": data.get("title"),
        "url": data.get("url"),
        "viewport": data.get("viewport", {}),
        "refs": refs,
        "ref_count": len(refs),
        "tree_text": "\n".join(lines),
    }


def find_ref(
    target: dict[str, Any],
    ref: str | None,
    selector: str | None,
) -> dict[str, Any]:
    if ref:
        if not re.fullmatch(r"ref_\d+", ref):
            raise ChromeError(f"bad ref {ref!r}, expected ref_N from chrome_read_page")
        model = read_page(target)
        for r in model["refs"]:
            if r.get("ref") == ref:
                return {
                    "x": int(r["x"]) + max(1, int(r["w"]) // 2),
                    "y": int(r["y"]) + max(1, int(r["h"]) // 2),
                }
        raise ChromeError(f"{ref} not on current page (re-run chrome_read_page)")
    if selector:
        require_selector(selector)
        pos = evaluate(
            target,
            f"""(() => {{
          const el = document.querySelector({json.dumps(selector)});
          if (!el) return null;
          const r = el.getBoundingClientRect();
          return {{x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)}};
        }})()""",
        )
        if not isinstance(pos, dict):
            raise ChromeError(f"no element for selector {selector!r}")
        return pos
    raise ChromeError("provide ref (from chrome_read_page) or selector")


def web_content(
    target: dict[str, Any],
    selector: str | None,
    want_html: bool,
    want_text: bool,
) -> dict[str, Any]:
    sel = json.dumps(selector) if selector else "null"
    js = f"""(() => {{
      const root = {sel} ? document.querySelector({sel}) : document.body;
      if (!root) return null;
      return {{html: root.outerHTML.slice(0, 200000),
               text: root.innerText.slice(0, 200000)}};
    }})()"""
    data = evaluate(target, js)
    if not isinstance(data, dict):
        raise ChromeError("no content (bad selector?)")
    out: dict[str, Any] = {"url": target.get("url")}
    if want_html:
        out["html"] = data.get("html", "")
    if want_text or not want_html:
        out["text"] = data.get("text", "")
    return out


# ---- lightweight cross-tab search (TF-IDF, fully local) ---------------------


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]{2,}", s.lower())


def tfidf_rank(query: str, docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Score docs [{id,title,url,text}] against query. Pure stdlib."""
    qterms = _tokens(query)
    if not qterms:
        raise ChromeError("query must contain letters/digits")
    df: dict[str, int] = {}
    doc_terms: list[dict[str, int]] = []
    for d in docs:
        counts: dict[str, int] = {}
        for tok in _tokens(str(d.get("text", "")) + " " + str(d.get("title", ""))):
            counts[tok] = counts.get(tok, 0) + 1
        doc_terms.append(counts)
        for tok in set(counts):
            df[tok] = df.get(tok, 0) + 1
    n = max(1, len(docs))
    scored = []
    for d, counts in zip(docs, doc_terms):
        total = sum(counts.values()) or 1
        score = 0.0
        for q in qterms:
            tf = counts.get(q, 0) / total
            idf = math.log(1 + n / (1 + df.get(q, 0)))
            score += tf * idf
        snippet = str(d.get("text", ""))[:300]
        scored.append({**d, "score": round(score, 4), "snippet": snippet})
    scored.sort(key=lambda d: float(d["score"]), reverse=True)
    return scored


# ---- Chrome profile: history + bookmarks ------------------------------------


def profile_dir() -> Path:
    if os.environ.get("CHROME_MCP_PROFILE_DIR"):
        return Path(os.environ["CHROME_MCP_PROFILE_DIR"]).expanduser()
    home = Path.home()
    if sys_platform() == "darwin":
        return home / "Library/Application Support/Google/Chrome"
    if sys_platform() == "win32":
        local = os.environ.get("LOCALAPPDATA", "")
        return Path(local) / "Google/Chrome/User Data" if local else home
    return home / ".config/google-chrome"


def sys_platform() -> str:
    import sys

    return sys.platform


def _webkit_to_unix(micros: int) -> float:
    return micros / 1_000_000 - 11_644_473_600


def history_search(
    text: str | None,
    start_iso: str | None,
    end_iso: str | None,
    max_results: int = 100,
) -> list[dict[str, Any]]:
    db = profile_dir() / "Default/History"
    if not db.is_file():
        raise ChromeError(f"History DB not found at {db} (is Chrome installed?)")
    max_results = max(1, min(1000, max_results))
    tmp = Path(tempfile.mkstemp(prefix="chrome-mcp-history-", suffix=".db")[1])
    try:
        shutil.copy2(db, tmp)
        con = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            clauses: list[str] = []
            args: list[Any] = []
            if text:
                clauses.append("(url LIKE ? OR title LIKE ?)")
                args += [f"%{text}%", f"%{text}%"]
            if start_iso:
                clauses.append("last_visit_time >= ?")
                args.append(_iso_to_webkit(start_iso))
            if end_iso:
                clauses.append("last_visit_time <= ?")
                args.append(_iso_to_webkit(end_iso))
            where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = con.execute(
                f"SELECT url, title, visit_count, last_visit_time FROM urls "
                f"{where} ORDER BY last_visit_time DESC LIMIT ?",
                (*args, max_results),
            ).fetchall()
        finally:
            con.close()
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    out = []
    for row in rows:
        url, title, visits, micro = row[0], row[1], row[2], row[3]
        try:
            ts = datetime.fromtimestamp(_webkit_to_unix(int(micro)), tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            ts = ""
        out.append({"url": url, "title": title or "", "visits": visits, "last_visit": ts})
    return out


def _iso_to_webkit(iso: str) -> int:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError as e:
        raise ChromeError(f"bad ISO date {iso!r} (use YYYY-MM-DD or full ISO)") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int((dt.timestamp() + 11_644_473_600) * 1_000_000)


def _bookmarks_path() -> Path:
    p = profile_dir() / "Default/Bookmarks"
    if not p.is_file():
        raise ChromeError(f"Bookmarks file not found at {p}")
    return p


def _walk_bookmarks(node: dict[str, Any], path: str) -> list[dict[str, Any]]:
    found = []
    ntype = node.get("type")
    name = str(node.get("name", ""))
    cur = f"{path}/{name}" if path else name
    if ntype == "url":
        found.append(
            {"id": str(node.get("id")), "title": name, "url": node.get("url", ""), "folder": path}
        )
    for child in node.get("children", []) or []:
        if isinstance(child, dict):
            found += _walk_bookmarks(child, cur)
    return found


def bookmark_search(
    query: str | None,
    folder: str | None,
    max_results: int = 50,
) -> list[dict[str, Any]]:
    data = json.loads(_bookmarks_path().read_text(encoding="utf-8"))
    all_marks = []
    for root in (data.get("roots") or {}).values():
        if isinstance(root, dict):
            all_marks += _walk_bookmarks(root, "")
    q = (query or "").lower()
    out = [
        b
        for b in all_marks
        if (not q or q in b["title"].lower() or q in b["url"].lower())
        and (not folder or folder.lower() in b["folder"].lower())
    ]
    return out[: max(1, min(500, max_results))]


def _find_folder(node: dict[str, Any], wanted: str) -> dict[str, Any] | None:
    if node.get("type") == "folder" and (
        str(node.get("id")) == wanted or str(node.get("name")) == wanted
    ):
        return node
    for child in node.get("children", []) or []:
        if isinstance(child, dict) and child.get("type") == "folder":
            hit = _find_folder(child, wanted)
            if hit is not None:
                return hit
    return None


def bookmark_add(
    url: str,
    title: str,
    parent: str | None,
    create_folder: bool,
) -> dict[str, Any]:
    require_http_url(url)
    path = _bookmarks_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    roots = data.get("roots", {})
    bar = roots.get("bookmark_bar")
    if not isinstance(bar, dict):
        raise ChromeError("bookmark_bar root missing")
    folder_node = bar
    if parent:
        hit = None
        for root in roots.values():
            if isinstance(root, dict):
                hit = _find_folder(root, parent)
                if hit is not None:
                    break
        if hit is None:
            if not create_folder:
                raise ChromeError(f"folder {parent!r} not found (pass create_folder=true)")
            hit = {"id": _next_bookmark_id(data), "name": parent, "type": "folder", "children": []}
            bar.setdefault("children", []).append(hit)
        folder_node = hit
    node = {"id": _next_bookmark_id(data), "name": title or url, "type": "url", "url": url}
    folder_node.setdefault("children", []).append(node)
    _write_bookmarks(path, data)
    return node


def _next_bookmark_id(data: dict[str, Any]) -> str:
    best = 0
    stack = list((data.get("roots") or {}).values())
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        try:
            best = max(best, int(str(node.get("id", 0))))
        except ValueError:
            pass
        stack += [c for c in node.get("children", []) or [] if isinstance(c, dict)]
    return str(best + 1)


def _write_bookmarks(path: Path, data: dict[str, Any]) -> None:
    backup = path.with_suffix(".bak")
    try:
        shutil.copy2(path, backup)
    except OSError:
        pass
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def bookmark_delete(
    bookmark_id: str | None,
    url: str | None,
    title: str | None,
) -> dict[str, Any]:
    if not bookmark_id and not url:
        raise ChromeError("provide bookmark_id or url")
    path = _bookmarks_path()
    data = json.loads(path.read_text(encoding="utf-8"))

    def _remove(children: list[Any]) -> dict[str, Any] | None:
        for i, child in enumerate(children):
            if not isinstance(child, dict):
                continue
            if bookmark_id and str(child.get("id")) == bookmark_id:
                return children.pop(i)
            if url and child.get("url") == url and (not title or child.get("name") == title):
                return children.pop(i)
            if isinstance(child.get("children"), list):
                hit = _remove(child["children"])
                if hit is not None:
                    return hit
        return None

    removed = None
    for root in (data.get("roots") or {}).values():
        if isinstance(root, dict) and isinstance(root.get("children"), list):
            removed = _remove(root["children"])
            if removed is not None:
                break
    if removed is None:
        raise ChromeError("bookmark not found")
    _write_bookmarks(path, data)
    return {"id": str(removed.get("id")), "title": removed.get("name"), "url": removed.get("url")}


# ---- console / network / tracing -------------------------------------------

_CONSOLE_JS_FALLBACK = "(()=>JSON.stringify(performance.getEntriesByType('resource').slice(-100).map(r=>({name:r.name, ms:Math.round(r.duration)}))))()"


def console_snapshot(
    target: dict[str, Any],
    pattern: str | None,
    only_errors: bool,
    max_messages: int,
) -> list[dict[str, Any]]:
    events = cdp_collect(
        ws_url_for(target),
        [("Log.enable", {}), ("Runtime.enable", {})],
        ("Log.entryAdded", "Runtime.consoleAPICalled", "Runtime.exceptionThrown"),
        2.0,
    )
    msgs: list[dict[str, Any]] = []
    for e in events:
        method = str(e.get("method", ""))
        p = e.get("params", {})
        if method == "Log.entryAdded":
            entry = p.get("entry", {})
            msgs.append(
                {
                    "level": entry.get("level"),
                    "text": entry.get("text", "")[:2000],
                    "url": entry.get("url", ""),
                }
            )
        else:
            args = p.get("args", []) if isinstance(p, dict) else []
            text = " ".join(
                str(a.get("value", a.get("type"))) for a in args if isinstance(a, dict)
            )[:2000]
            msgs.append(
                {"level": "error" if "exception" in method else "log", "text": text, "url": ""}
            )
    if only_errors:
        msgs = [m for m in msgs if m["level"] in ("error", "warning", "violations")]
    if pattern:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ChromeError(f"bad pattern regex: {e}") from e
        msgs = [m for m in msgs if rx.search(m["text"])]
    return msgs[: max(1, min(500, max_messages))]


def resource_timing(target: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        raw = evaluate(target, _CONSOLE_JS_FALLBACK)
    except ChromeError:
        return []
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return []
    return items if isinstance(items, list) else []


def network_capture_start(url: str | None, max_ms: int) -> dict[str, Any]:
    sid = f"cap-{int(time.time() * 1000)}"
    if url:
        require_http_url(url)
        new_tab(url)
    _NET_SESSIONS[sid] = {"started": time.time(), "max_ms": max_ms, "url": url}
    return {"capture_id": sid, "started": True}


def network_capture_stop(
    include_static: bool,
    max_entries: int = 200,
) -> dict[str, Any]:
    if not _NET_SESSIONS:
        raise ChromeError("no capture running (call with action=start first)")
    sid = sorted(_NET_SESSIONS)[-1]
    session = _NET_SESSIONS.pop(sid)
    elapsed = time.time() - session["started"]
    entries: list[dict[str, Any]] = []
    for t in targets():
        for r in resource_timing(t):
            name = str(r.get("name", ""))
            if not include_static and name.lower().endswith(_STATIC_EXTS):
                continue
            entries.append({"tab": t.get("title", "")[:80], "url": name, "ms": r.get("ms", 0)})
            if len(entries) >= max_entries:
                break
    return {
        "capture_id": sid,
        "elapsed_s": round(elapsed, 1),
        "requests": entries[:max_entries],
        "count": len(entries),
        "note": "resource-timing view (no bodies); use chrome_network_request for bodies",
    }


def network_request_via_page(
    target: dict[str, Any],
    url: str,
    method: str,
    headers: dict[str, str],
    body: str | None,
    timeout_ms: int,
) -> dict[str, Any]:
    require_http_url(url)
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
        raise ChromeError(f"unsupported method {method!r}")
    js = f"""(async () => {{
      const ctrl = new AbortController();
      const t = setTimeout(() => ctrl.abort(), {min(timeout_ms, 60000)});
      try {{
        const res = await fetch({json.dumps(url)}, {{
          method: {json.dumps(method)},
          headers: {json.dumps(headers)},
          body: {json.dumps(body) if body is not None else "undefined"},
          signal: ctrl.signal, credentials: 'include'}});
        const text = await res.text();
        return {{status: res.status, text: text.slice(0, 100000),
                 truncated: text.length > 100000}};
      }} finally {{ clearTimeout(t); }}
    }})()"""
    data = evaluate(target, js, await_promise=True, timeout_ms=timeout_ms)
    if not isinstance(data, dict):
        raise ChromeError("request produced no response")
    return data


# ---- downloads / dialogs / uploads -----------------------------------------


def wait_for_download(
    filename_contains: str | None,
    timeout_ms: int,
) -> dict[str, Any]:
    d = download_dir()
    deadline = time.time() + max(1.0, min(300.0, timeout_ms / 1000.0))
    start = time.time()
    while time.time() < deadline:
        try:
            files = sorted(d.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        for f in files:
            if f.suffix == ".crdownload":
                continue
            if f.stat().st_mtime < start - 1:
                continue
            if filename_contains and filename_contains not in f.name:
                continue
            return {"filename": f.name, "path": str(f), "bytes": f.stat().st_size, "complete": True}
        time.sleep(1.0)
    raise ChromeError(f"no download arrived within {timeout_ms}ms in {d}")


def handle_dialog(action: str, prompt_text: str | None) -> dict[str, Any]:
    if action not in ("accept", "dismiss"):
        raise ChromeError("action must be accept|dismiss")
    for t in targets():
        try:
            cdp_call(
                ws_url_for(t),
                "Page.handleJavaScriptDialog",
                {
                    "accept": action == "accept",
                    "promptText": prompt_text or "",
                },
            )
            return {"handled": True, "action": action, "tab": t.get("title", "")[:120]}
        except ChromeError:
            continue
    raise ChromeError("no open JavaScript dialog found")


def upload_file(
    target: dict[str, Any],
    selector: str,
    file_path: str | None,
    file_url: str | None,
) -> dict[str, Any]:
    require_selector(selector)
    if file_url:
        require_http_url(file_url, "file_url")
        # Download to temp so CDP can reference a local path.
        tmp = Path(tempfile.mkstemp(prefix="chrome-mcp-upload-")[1])
        try:
            with urllib.request.urlopen(file_url, timeout=30.0) as r:
                tmp.write_bytes(r.read())
            local = str(tmp)
        except Exception as e:
            raise ChromeError(f"download for upload failed: {e}") from e
    elif file_path:
        local = str(Path(file_path).expanduser())
        p = Path(local)
        if not p.is_file() or p.is_symlink():
            raise ChromeError("file_path must be an existing real file")
    else:
        raise ChromeError("provide file_path or file_url")
    node = cdp_call(ws_url_for(target), "DOM.getDocument", {"depth": -1})
    root = node.get("root", {}).get("nodeId")
    found = cdp_call(
        ws_url_for(target), "DOM.querySelector", {"nodeId": root, "selector": selector}
    )
    node_id = found.get("nodeId", 0)
    if not node_id:
        raise ChromeError(f"no file input for selector {selector!r}")
    cdp_call(ws_url_for(target), "DOM.setFileInputFiles", {"nodeId": node_id, "files": [local]})
    return {"uploaded": True, "selector": selector, "file": local}


# ---- userscripts / flows / inject ------------------------------------------


def _store_path(kind: str) -> Path:
    p = data_dir() / f"{kind}.json"
    if not p.exists():
        p.write_text(json.dumps({}), encoding="utf-8")
    return p


def _store_load(kind: str) -> dict[str, Any]:
    try:
        data = json.loads(_store_path(kind).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _store_save(kind: str, data: dict[str, Any]) -> None:
    _store_path(kind).write_text(json.dumps(data, indent=2), encoding="utf-8")


def userscript_dispatch(action: str, args: dict[str, Any]) -> dict[str, Any]:
    scripts = _store_load("userscripts")
    if action == "list":
        return {"scripts": list(scripts.values()), "count": len(scripts)}
    if action == "create":
        code = str(args.get("script") or args.get("code") or "")
        name = str(args.get("name") or f"script-{len(scripts) + 1}")
        if not code:
            raise ChromeError("script/code is required")
        sid = f"us-{int(time.time() * 1000)}"
        scripts[sid] = {
            "id": sid,
            "name": name,
            "script": code[:200_000],
            "enabled": True,
            "matches": args.get("matches", ["<all_urls>"]),
        }
        _store_save("userscripts", scripts)
        return {"id": sid, "name": name}
    sid = str(args.get("id") or "")
    if action == "get":
        if sid not in scripts:
            raise ChromeError(f"unknown userscript {sid!r}")
        return scripts[sid]
    if action in ("enable", "disable"):
        if sid not in scripts:
            raise ChromeError(f"unknown userscript {sid!r}")
        scripts[sid]["enabled"] = action == "enable"
        _store_save("userscripts", scripts)
        return {"id": sid, "enabled": scripts[sid]["enabled"]}
    if action == "update":
        if sid not in scripts:
            raise ChromeError(f"unknown userscript {sid!r}")
        for key in ("name", "script", "matches"):
            if args.get(key) is not None:
                scripts[sid][key] = args[key]
        _store_save("userscripts", scripts)
        return scripts[sid]
    if action == "remove":
        if sid not in scripts:
            raise ChromeError(f"unknown userscript {sid!r}")
        del scripts[sid]
        _store_save("userscripts", scripts)
        return {"removed": sid}
    raise ChromeError(f"unknown userscript action {action!r}")


def flows_list() -> list[dict[str, Any]]:
    flows = _store_load("flows")
    return list(flows.values())


def flow_run(
    target: dict[str, Any],
    flow_id: str,
    variables: dict[str, Any],
    timeout_ms: int,
) -> dict[str, Any]:
    flows = _store_load("flows")
    flow = flows.get(flow_id)
    if flow is None:
        raise ChromeError(f"unknown flow {flow_id!r} (see record_replay_list_published)")
    steps = flow.get("steps", [])
    if not isinstance(steps, list) or not steps:
        raise ChromeError(f"flow {flow_id!r} has no steps")
    logs: list[str] = []
    last: Any = None
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        code = str(step.get("js") or step.get("code") or "")
        for key, val in variables.items():
            code = code.replace(f"{{{{{key}}}}}", json.dumps(val))
        try:
            last = evaluate(target, code, await_promise=True, timeout_ms=min(timeout_ms, 60000))
            logs.append(f"step {i}: ok")
        except ChromeError as e:
            logs.append(f"step {i}: FAILED: {e}")
            return {"flow": flow_id, "ok": False, "logs": logs, "last": None}
    return {"flow": flow_id, "ok": True, "logs": logs, "last": last}


def inject_script(
    target: dict[str, Any],
    js: str,
    persist: bool,
) -> dict[str, Any]:
    if not js or len(js) > 200_000:
        raise ChromeError("js must be 1..200k chars")
    if persist:
        cdp_call(ws_url_for(target), "Page.addScriptToEvaluateOnNewDocument", {"source": js})
    result = evaluate(target, js)
    text = json.dumps(result)[:5000] if result is not None else "ok"
    return {"injected": True, "persistent": persist, "result": text}


def send_command_to_page(
    target: dict[str, Any],
    event_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_:-]{1,120}", event_name):
        raise ChromeError("bad event_name")
    evaluate(
        target,
        "window.dispatchEvent(new CustomEvent("
        + json.dumps(event_name)
        + ", {detail: "
        + json.dumps(payload)
        + "}))",
    )
    return {"dispatched": event_name}


# ---- tracing / gif ----------------------------------------------------------


def trace_start(reload: bool) -> dict[str, Any]:
    sid = f"tr-{int(time.time() * 1000)}"
    target = resolve_target()
    cdp_call(
        ws_url_for(target),
        "Tracing.start",
        {
            "categories": ["devtools.timeline", "loading", "scripting", "rendering", "paint"],
            "transferMode": "ReturnAsStream",
        },
    )
    if reload:
        cdp_call(ws_url_for(target), "Page.reload", {})
    _TRACE_SESSIONS[sid] = {"started": time.time(), "ws": ws_url_for(target)}
    return {"trace_id": sid, "started": True}


def trace_stop(save: bool, prefix: str) -> dict[str, Any]:
    if not _TRACE_SESSIONS:
        raise ChromeError("no trace running (call performance_start_trace first)")
    sid = sorted(_TRACE_SESSIONS)[-1]
    session = _TRACE_SESSIONS.pop(sid)
    try:
        cdp_call(session["ws"], "Tracing.end", {})
    except ChromeError:
        pass
    events = cdp_collect(
        session["ws"], [], ("Tracing.dataCollected", "Tracing.tracingComplete"), 5.0
    )
    path = ""
    if save:
        path = str(data_dir() / f"{prefix or 'trace'}-{sid}.json")
        Path(path).write_text(json.dumps(events)[:5_000_000], encoding="utf-8")
    return {"trace_id": sid, "events": len(events), "path": path}


def trace_analyze(events_path: str | None) -> dict[str, Any]:
    if events_path:
        try:
            events = json.loads(Path(events_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise ChromeError(f"cannot read trace {events_path}: {e}") from e
    else:
        events = []
    totals: dict[str, float] = {}
    count = 0
    items = events if isinstance(events, list) else []
    for evt in items:
        if not isinstance(evt, dict):
            continue
        params = evt.get("params", {})
        for chunk in [params] if isinstance(params, dict) else []:
            for ev in chunk.get("traceEvents", []) or []:
                if not isinstance(ev, dict):
                    continue
                count += 1
                key = str(ev.get("name", "?"))
                totals[key] = totals.get(key, 0.0) + float(ev.get("dur", 0) or 0)
    top = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:10]
    return {
        "events": count,
        "top_ms": [{"name": k, "ms": round(v / 1000, 1)} for k, v in top],
        "note": "summary only; open the saved trace in chrome://tracing for flame graphs",
    }


def gif_start(fps: int, duration_ms: int, width: int) -> dict[str, Any]:
    fps = max(1, min(10, fps))
    duration_ms = max(500, min(30000, duration_ms))
    sid = f"gif-{int(time.time() * 1000)}"
    _GIF_SESSIONS[sid] = {
        "fps": fps,
        "duration_ms": duration_ms,
        "width": max(100, min(1280, width)),
        "frames": [],
    }
    return {"recording_id": sid, "fps": fps, "duration_ms": duration_ms}


def gif_capture(recording_id: str) -> dict[str, Any]:
    session = _GIF_SESSIONS.get(recording_id)
    if session is None:
        raise ChromeError(f"unknown recording {recording_id!r}")
    target = resolve_target()
    want = max(1, min(100, int(session["duration_ms"] / 1000 * int(session["fps"]))))
    frames: list[bytes] = session["frames"]
    interval = 1.0 / float(session["fps"])
    while len(frames) < want:
        frames.append(screenshot(target, full_page=False))
        if len(frames) < want:
            time.sleep(interval)
    session["frames"] = frames
    return {"recording_id": recording_id, "frames": len(frames)}


def gif_export(recording_id: str, filename: str | None) -> dict[str, Any]:
    session = _GIF_SESSIONS.get(recording_id)
    if session is None:
        raise ChromeError(f"unknown recording {recording_id!r}")
    frames: list[bytes] = session["frames"]
    if not frames:
        raise ChromeError("no frames (call gif_capture first)")
    out_dir = data_dir() / recording_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for i, png in enumerate(frames):
        (out_dir / f"frame-{i:03d}.png").write_bytes(png)
    gif_path = ""
    try:
        from PIL import Image  # type: ignore[import-not-found]

        images = []
        for i in range(len(frames)):
            img = Image.open(out_dir / f"frame-{i:03d}.png")
            img = img.copy()
            images.append(img)
        gif_path = str(out_dir / (filename or "recording.gif"))
        images[0].save(
            gif_path,
            save_all=True,
            append_images=images[1:],
            duration=int(1000 / int(session["fps"])),
            loop=0,
        )
    except ImportError:
        gif_path = ""
    result = {
        "recording_id": recording_id,
        "frames": len(frames),
        "dir": str(out_dir),
        "gif": gif_path,
    }
    if not gif_path:
        result["note"] = (
            "Pillow not installed: PNG sequence saved; pip install pillow to also emit GIF"
        )
    _GIF_SESSIONS.pop(recording_id, None)
    return result


def gif_status(recording_id: str) -> dict[str, Any]:
    session = _GIF_SESSIONS.get(recording_id)
    if session is None:
        raise ChromeError(f"unknown recording {recording_id!r}")
    return {
        "recording_id": recording_id,
        "frames": len(session["frames"]),
        "fps": session["fps"],
        "duration_ms": session["duration_ms"],
    }


def keyboard_map(key: str) -> dict[str, Any]:
    """Map friendly key names to CDP Input.dispatchKeyEvent params."""
    named = {
        "Enter": 13,
        "Tab": 9,
        "Escape": 27,
        "Backspace": 8,
        "Delete": 46,
        "ArrowLeft": 37,
        "ArrowUp": 38,
        "ArrowRight": 39,
        "ArrowDown": 40,
        "Home": 36,
        "End": 35,
    }
    if key in named:
        return {"key": key, "windowsVirtualKeyCode": named[key]}
    if len(key) == 1:
        return {"key": key}
    raise ChromeError(
        "keys must be single chars or Enter/Tab/Escape/Backspace/Delete/"
        "Arrows/Home/End (combine with e.g. 'ctrl+a' via modifiers)"
    )
