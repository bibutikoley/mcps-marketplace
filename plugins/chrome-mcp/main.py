"""MCP server: local Chrome automation over CDP for Claude Code.

Run:  uv run main.py
Register:  claude mcp add chrome-mcp -s user -- uv run --project <this dir> main.py

Prerequisites: desktop Chrome/Chromium launched once with
``--remote-debugging-port=9222`` (or set CHROME_MCP_PORT to match).
Bookmarks/History are read from the local Chrome profile — no extension,
no cloud, no vector downloads. Destructive tools need confirm=true.

See README.md for the full tool reference and security model.
"""

from __future__ import annotations

import base64
import json
import time
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, ImageContent, TextContent

import chrome


def _package_version() -> str:
    """Single source of truth: ``pyproject.toml`` ``version``.

    Resolved via installed package metadata when the distribution is
    installed (``uvx`` / ``uv run --project``), with a source-checkout
    fallback that parses the sibling ``pyproject.toml`` so ``python
    main.py`` from a clone reports the same version.
    """
    dist_name = "chrome-mcp"
    try:
        return _dist_version(dist_name)
    except PackageNotFoundError:
        pass
    pyproject = Path(__file__).resolve().parent / "pyproject.toml"
    try:
        import tomllib

        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        return str(data["project"]["version"])
    except Exception as e:
        raise RuntimeError(
            f"cannot determine {dist_name} version: not installed and {pyproject} unreadable ({e})"
        ) from e


mcp = MCPServer("chrome-mcp", version=_package_version())


def _ok(text: str, **extra) -> CallToolResult:
    """Tool result: human-readable text + machine-readable structured fields."""
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=extra if extra else None,
    )


def _err(text: str, **extra) -> CallToolResult:
    """Tool error: human-readable error text + is_error flag + details."""
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        is_error=True,
        structured_content=extra if extra else None,
    )


def _require_confirm(confirm: bool, op: str) -> CallToolResult | None:
    """Server-side guard for destructive ops. Returns an error result when
    confirm is not set, otherwise None (caller proceeds)."""
    if not confirm:
        return _err(
            f"{op} is destructive. Re-invoke with confirm=true after user approval.",
            operation=op,
            confirm_required=True,
        )
    return None


def _shot(text: str, png: bytes, **extra) -> CallToolResult:
    """Screenshot result: inline image (agent sees the page) + text + fields."""
    return CallToolResult(
        content=[
            ImageContent(
                type="image",
                data=base64.b64encode(png).decode(),
                mime_type="image/png",
            ),
            TextContent(type="text", text=text),
        ],
        structured_content=extra or None,
    )


def _tab_ref(t: dict, i: int) -> dict:
    return {
        "tabId": i + 1,
        "targetId": t.get("id"),
        "title": t.get("title", ""),
        "url": t.get("url", ""),
    }


# ---- health / tabs ----------------------------------------------------------


@mcp.tool()
def health_check() -> CallToolResult:
    """Verify the Chrome debugger is reachable and report pages/browser. Run this first!"""
    try:
        h = chrome.health()
        if not h.get("ok"):
            return _ok(f"FAIL: {h.get('error')}", **h)
        return _ok(
            f"OK: {h['browser']} via {h['host']}:{h['port']}, {h['pages']} page(s).",
            **h,
        )
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def get_windows_and_tabs() -> CallToolResult:
    """List open tabs (stable 1-based tabId, targetId, title, url)."""
    try:
        pages = chrome.targets()
        tabs = [_tab_ref(t, i) for i, t in enumerate(sorted(pages, key=lambda t: str(t.get("id"))))]
        lines = [f"[{t['tabId']}] {t['title'][:80]} <{t['url'][:120]}>" for t in tabs]
        return _ok(
            f"{len(tabs)} tab(s):\n" + "\n".join(lines) if lines else "(no tabs)",
            tabs=tabs,
            count=len(tabs),
        )
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_navigate(
    url: str | None = None,
    tabId: int | None = None,
    refresh: bool = False,
    back: bool = False,
    forward: bool = False,
    newWindow: bool = False,
) -> CallToolResult:
    """Navigate a tab (url), refresh it, or go back/forward. Omit tabId for the active tab."""
    try:
        if newWindow:
            if not url:
                return _err("newWindow needs url")
            chrome.require_http_url(url)
            t = chrome.new_tab(url)
            return _ok(f"Opened {url} in a new window.", targetId=t.get("id"))
        target = chrome.resolve_target(tab_id=tabId)
        ws = chrome.ws_url_for(target)
        if back or forward:
            delta = -1 if back else 1
            hist = chrome.cdp_call(ws, "Page.getNavigationHistory", {})
            idx = int(hist.get("currentIndex", 0)) + delta
            entries = hist.get("entries", [])
            if not (0 <= idx < len(entries)):
                return _err("No history entry in that direction.")
            chrome.cdp_call(ws, "Page.navigateToHistoryEntry", {"entryId": entries[idx]["id"]})
            return _ok(f"Went {'back' if back else 'forward'}.", tabId=tabId)
        if refresh:
            chrome.cdp_call(ws, "Page.reload", {})
            return _ok("Refreshed.", tabId=tabId)
        if not url:
            return _err("Provide url, refresh=true, or back/forward=true.")
        chrome.require_http_url(url)
        chrome.cdp_call(ws, "Page.navigate", {"url": url})
        return _ok(f"Navigating to {url}.", tabId=tabId, url=url)
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_close_tabs(
    tabIds: list[int] | None = None, url: str | None = None, confirm: bool = False
) -> CallToolResult:
    """Close tabs by tabId list or URL substring. DESTRUCTIVE — needs confirm=true."""
    if (denied := _require_confirm(confirm, "chrome_close_tabs")) is not None:
        return denied
    try:
        pages = sorted(chrome.targets(), key=lambda t: str(t.get("id")))
        victims = []
        if tabIds:
            for i in tabIds:
                victims.append(chrome.resolve_target(tab_id=i))
        elif url:
            victims = [t for t in pages if url in str(t.get("url", ""))]
            if not victims:
                return _err(f"No tabs matching {url!r}.")
        else:
            victims = [chrome.resolve_target()]
        for t in victims:
            chrome.close_target(t)
        return _ok(f"Closed {len(victims)} tab(s).", closed=len(victims))
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_switch_tab(tabId: int) -> CallToolResult:
    """Activate a tab by its 1-based tabId (see get_windows_and_tabs)."""
    try:
        target = chrome.resolve_target(tab_id=tabId)
        chrome.activate_target(target)
        return _ok(f"Switched to [{tabId}] {target.get('title', '')[:80]}.", tabId=tabId)
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


# ---- observe ----------------------------------------------------------------


@mcp.tool()
def chrome_screenshot(
    tabId: int | None = None, selector: str | None = None, fullPage: bool = True
) -> CallToolResult:
    """Capture the tab as PNG, returned INLINE. Prefer chrome_read_page for structure."""
    try:
        target = chrome.resolve_target(tab_id=tabId)
        if selector:
            pos = chrome.find_ref(target, None, selector)
            x, y = pos["x"], pos["y"]
            chrome.cdp_call(chrome.ws_url_for(target), "Page.bringToFront", {})
            del x, y
        png = chrome.screenshot(target, full_page=fullPage)
        return _shot(
            f"Screenshot ({len(png)} bytes, fullPage={fullPage}). "
            "Inspect it before acting; prefer chrome_read_page for re-observation.",
            png,
            tabId=tabId,
            bytes=len(png),
        )
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_read_page(tabId: int | None = None) -> CallToolResult:
    """Interactive-element tree with stable ref_N ids + viewport. THE primary observation tool."""
    try:
        target = chrome.resolve_target(tab_id=tabId)
        model = chrome.read_page(target)
        text = model["tree_text"] + f"\n({model['ref_count']} refs)"
        return _ok(
            text,
            refs=model["refs"],
            ref_count=model["ref_count"],
            viewport=model["viewport"],
            url=model["url"],
        )
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e} — fall back to chrome_screenshot.")


@mcp.tool()
def chrome_get_web_content(
    tabId: int | None = None,
    url: str | None = None,
    selector: str | None = None,
    html: bool = False,
) -> CallToolResult:
    """Extract visible text (default) or HTML from a tab or URL."""
    try:
        if url:
            chrome.require_http_url(url)
            target = chrome.new_tab(url)
            time.sleep(2.0)
            target = chrome.resolve_target(target_id=target.get("id"))
        else:
            target = chrome.resolve_target(tab_id=tabId)
        if selector:
            chrome.require_selector(selector)
        data = chrome.web_content(target, selector, want_html=html, want_text=True)
        body = data.get("html", "") if html else data.get("text", "")
        return _ok(
            f"Content from {data.get('url', '')} ({len(body)} chars).",
            url=data.get("url"),
            content=body[:20000],
            truncated=len(body) > 20000,
        )
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def search_tabs_content(query: str, max_results: int = 5) -> CallToolResult:
    """Local TF-IDF search across open tabs (no downloads, no vector DB)."""
    try:
        docs = []
        for t in chrome.targets():
            try:
                data = chrome.web_content(t, None, want_html=False, want_text=True)
                docs.append(
                    {
                        "id": t.get("id"),
                        "title": t.get("title", ""),
                        "url": t.get("url", ""),
                        "text": data.get("text", ""),
                    }
                )
            except chrome.ChromeError:
                continue
        ranked = chrome.tfidf_rank(query, docs)
        hits = [d for d in ranked if d["score"] > 0][: max(1, min(20, max_results))]
        lines = [f"- {d['title'][:70]} ({d['score']}) <{d['url'][:100]}>" for d in hits]
        return _ok(
            f"{len(hits)}/{len(docs)} tab(s) matched {query!r}:\n" + "\n".join(lines)
            if lines
            else f"No tabs matched {query!r}.",
            matches=hits,
            searched=len(docs),
        )
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


# ---- interaction --------------------------------------------------------------


@mcp.tool()
def chrome_computer(
    action: str,
    tabId: int | None = None,
    ref: str | None = None,
    selector: str | None = None,
    x: int | None = None,
    y: int | None = None,
    text: str | None = None,
    scrollDirection: str | None = None,
    scrollAmount: int = 3,
) -> CallToolResult:
    """Unified click/scroll/type/key/hover/wait/screenshot on a tab."""
    try:
        target = chrome.resolve_target(tab_id=tabId)
        ws = chrome.ws_url_for(target)
        action = action.lower()
        if action in ("left_click", "right_click", "double_click", "hover"):
            if x is None or y is None:
                pos = chrome.find_ref(target, ref, selector)
                x, y = int(pos["x"]), int(pos["y"])
            chrome.require_coords(x, y)
            btn = "right" if action == "right_click" else "left"
            click_count = 2 if action == "double_click" else 1
            mtype = "mouseMoved" if action == "hover" else "mousePressed"
            chrome.cdp_call(
                ws,
                "Input.dispatchMouseEvent",
                {"type": mtype, "x": x, "y": y, "button": btn, "clickCount": click_count},
            )
            if action != "hover":
                chrome.cdp_call(
                    ws,
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseReleased",
                        "x": x,
                        "y": y,
                        "button": btn,
                        "clickCount": click_count,
                    },
                )
            return _ok(f"{action} at ({x},{y}). Re-observe with chrome_read_page.", x=x, y=y)
        if action == "scroll":
            direction = (scrollDirection or "down").lower()
            dx, dy = 0, 0
            dist = 200 * max(1, min(10, scrollAmount))
            if direction == "up":
                dy = -dist
            elif direction == "down":
                dy = dist
            elif direction == "left":
                dx = -dist
            elif direction == "right":
                dx = dist
            else:
                return _err("scrollDirection must be up|down|left|right.")
            chrome.evaluate(target, f"window.scrollBy({dx}, {dy})")
            return _ok(f"Scrolled {direction}. Re-observe with chrome_read_page.")
        if action == "type":
            if not text:
                return _err("type needs text.")
            chrome.cdp_call(ws, "Input.insertText", {"text": text})
            return _ok(f"Typed {len(text)} char(s). Re-observe with chrome_read_page.")
        if action == "key":
            if not text:
                return _err("key needs text (e.g. 'Enter').")
            params = chrome.keyboard_map(text)
            chrome.cdp_call(ws, "Input.dispatchKeyEvent", {"type": "keyDown", **params})
            chrome.cdp_call(ws, "Input.dispatchKeyEvent", {"type": "keyUp", **params})
            return _ok(f"Pressed {text}. Re-observe with chrome_read_page.")
        if action == "screenshot":
            png = chrome.screenshot(target, full_page=False)
            return _shot(f"Screenshot ({len(png)} bytes).", png, bytes=len(png))
        return _err(f"Unknown action {action!r} (click/scroll/type/key/hover/screenshot).")
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_click_element(
    ref: str | None = None,
    selector: str | None = None,
    x: int | None = None,
    y: int | None = None,
    tabId: int | None = None,
) -> CallToolResult:
    """Click via ref (preferred), selector, or x/y coordinates."""
    try:
        target = chrome.resolve_target(tab_id=tabId)
        if x is not None and y is not None:
            chrome.require_coords(x, y)
        else:
            pos = chrome.find_ref(target, ref, selector)
            x, y = int(pos["x"]), int(pos["y"])
        assert x is not None and y is not None
        ws = chrome.ws_url_for(target)
        chrome.cdp_call(
            ws,
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        chrome.cdp_call(
            ws,
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        return _ok(f"Clicked ({x},{y}). Re-observe with chrome_read_page.", x=x, y=y)
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_fill_or_select(
    value: str, ref: str | None = None, selector: str | None = None, tabId: int | None = None
) -> CallToolResult:
    """Fill an input/textarea/select identified by ref or selector."""
    try:
        target = chrome.resolve_target(tab_id=tabId)
        if ref:
            model = chrome.read_page(target)
            if ref not in [r.get("ref") for r in model["refs"]]:
                return _err(f"{ref} not on page (re-run chrome_read_page).")
            # Focus via coordinates, then type (works without knowing the selector).
            pos = chrome.find_ref(target, ref, None)
            ws = chrome.ws_url_for(target)
            chrome.cdp_call(
                ws,
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": pos["x"],
                    "y": pos["y"],
                    "button": "left",
                    "clickCount": 1,
                },
            )
            chrome.cdp_call(
                ws,
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": pos["x"],
                    "y": pos["y"],
                    "button": "left",
                    "clickCount": 1,
                },
            )
            chrome.cdp_call(ws, "Input.insertText", {"text": value})
            return _ok(f"Filled {ref}. Re-observe with chrome_read_page.", ref=ref)
        if not selector:
            return _err("Provide ref or selector.")
        chrome.require_selector(selector)
        out = chrome.evaluate(
            target,
            f"""(() => {{
          const el = document.querySelector({json.dumps(selector)});
          if (!el) return 'missing';
          el.focus();
          el.value = {json.dumps(value)};
          el.dispatchEvent(new Event('input', {{bubbles: true}}));
          el.dispatchEvent(new Event('change', {{bubbles: true}}));
          return 'ok';
        }})()""",
        )
        if out != "ok":
            return _err(f"No element for selector {selector!r}.")
        return _ok(f"Filled {selector}. Re-observe with chrome_read_page.", selector=selector)
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_keyboard(
    keys: str, selector: str | None = None, tabId: int | None = None
) -> CallToolResult:
    """Press keys (single char or Enter/Tab/Escape/Backspace/Delete/Arrows/Home/End)."""
    try:
        target = chrome.resolve_target(tab_id=tabId)
        if selector:
            pos = chrome.find_ref(target, None, selector)
            ws = chrome.ws_url_for(target)
            chrome.cdp_call(
                ws,
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "x": pos["x"],
                    "y": pos["y"],
                    "button": "left",
                    "clickCount": 1,
                },
            )
            chrome.cdp_call(
                ws,
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "x": pos["x"],
                    "y": pos["y"],
                    "button": "left",
                    "clickCount": 1,
                },
            )
        params = chrome.keyboard_map(keys)
        ws = chrome.ws_url_for(target)
        chrome.cdp_call(ws, "Input.dispatchKeyEvent", {"type": "keyDown", **params})
        chrome.cdp_call(ws, "Input.dispatchKeyEvent", {"type": "keyUp", **params})
        return _ok(f"Pressed {keys}. Re-observe with chrome_read_page.", keys=keys)
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_request_element_selection(tabId: int | None = None) -> CallToolResult:
    """Return clickable candidates for the human to pick (no overlay without the extension)."""
    try:
        target = chrome.resolve_target(tab_id=tabId)
        model = chrome.read_page(target)
        cands = [{"ref": r["ref"], "label": r["label"][:60]} for r in model["refs"][:30]]
        return _ok(
            "Pick one ref and pass it to chrome_click_element/chrome_fill_or_select. "
            "(The extension overlay picker needs the TS build; this port returns refs.)",
            candidates=cands,
            count=len(cands),
        )
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


# ---- scripting / console / network --------------------------------------------


@mcp.tool()
def chrome_javascript(code: str, tabId: int | None = None, confirm: bool = False) -> CallToolResult:
    """Run JS in the tab (inherits logins/cookies). DESTRUCTIVE — needs confirm=true."""
    if (denied := _require_confirm(confirm, "chrome_javascript")) is not None:
        return denied
    try:
        target = chrome.resolve_target(tab_id=tabId)
        result = chrome.evaluate(target, code, await_promise=True)
        text = json.dumps(result)[:8000] if result is not None else "(no return value)"
        return _ok(text, result=result)
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_console(
    tabId: int | None = None,
    pattern: str | None = None,
    onlyErrors: bool = False,
    maxMessages: int = 100,
) -> CallToolResult:
    """Capture ~2s of console/log output with optional regex/level filters."""
    try:
        target = chrome.resolve_target(tab_id=tabId)
        msgs = chrome.console_snapshot(target, pattern, onlyErrors, maxMessages)
        lines = [f"[{m['level']}] {m['text'][:200]}" for m in msgs]
        return _ok(
            f"{len(msgs)} message(s):\n" + "\n".join(lines) if lines else "(no messages)",
            messages=msgs,
            count=len(msgs),
        )
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_network_capture(
    action: str, url: str | None = None, includeStatic: bool = False
) -> CallToolResult:
    """Unified capture: action=start (optionally load url), action=stop (resource-timing summary)."""
    try:
        if action == "start":
            info = chrome.network_capture_start(url, 180000)
            return _ok(
                f"Capture {info['capture_id']} armed." + (f" Loaded {url}." if url else ""), **info
            )
        if action == "stop":
            info = chrome.network_capture_stop(include_static=includeStatic)
            lines = [f"- {r['url'][:100]} ({r['ms']}ms)" for r in info["requests"][:20]]
            return _ok(
                f"{info['count']} request(s) in {info['elapsed_s']}s:\n" + "\n".join(lines)
                if lines
                else "(no requests observed)",
                **info,
            )
        return _err("action must be start|stop.")
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_network_request(
    url: str,
    method: str = "GET",
    headers: str | None = None,
    body: str | None = None,
    tabId: int | None = None,
    confirm: bool = False,
) -> CallToolResult:
    """Fetch from the page context (keeps cookies). Non-GET needs confirm=true."""
    try:
        if method.upper() != "GET":
            if (denied := _require_confirm(confirm, "chrome_network_request")) is not None:
                return denied
        target = chrome.resolve_target(tab_id=tabId)
        hdrs = json.loads(headers) if headers else {}
        if not isinstance(hdrs, dict):
            return _err("headers must be a JSON object.")
        res = chrome.network_request_via_page(
            target, url, method, {str(k): str(v) for k, v in hdrs.items()}, body, 30000
        )
        text = str(res.get("text", ""))
        return _ok(
            f"HTTP {res.get('status')} ({len(text)} chars).",
            status=res.get("status"),
            body=text[:20000],
            truncated=bool(res.get("truncated")),
        )
    except (chrome.ChromeError, json.JSONDecodeError) as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_upload_file(
    selector: str,
    file_path: str | None = None,
    file_url: str | None = None,
    tabId: int | None = None,
) -> CallToolResult:
    """Set a file input's files (local path must exist; symlinks refused)."""
    try:
        target = chrome.resolve_target(tab_id=tabId)
        info = chrome.upload_file(target, selector, file_path, file_url)
        return _ok(f"Upload set on {selector}.", **info)
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_inject_script(
    js: str, persist: bool = False, tabId: int | None = None, confirm: bool = False
) -> CallToolResult:
    """Evaluate JS now; persist=true also registers it for future loads. Needs confirm=true."""
    if (denied := _require_confirm(confirm, "chrome_inject_script")) is not None:
        return denied
    try:
        target = chrome.resolve_target(tab_id=tabId)
        return _ok("Injected.", **chrome.inject_script(target, js, persist))
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_send_command_to_inject_script(
    eventName: str, payload: str | None = None, tabId: int | None = None
) -> CallToolResult:
    """Dispatch a CustomEvent so injected scripts can react (no extension bus here)."""
    try:
        target = chrome.resolve_target(tab_id=tabId)
        data = json.loads(payload) if payload else {}
        if not isinstance(data, dict):
            return _err("payload must be a JSON object.")
        return _ok("Dispatched.", **chrome.send_command_to_page(target, eventName, data))
    except (chrome.ChromeError, json.JSONDecodeError) as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_userscript(
    action: str, id: str | None = None, name: str | None = None, script: str | None = None
) -> CallToolResult:
    """Local userscript store: create|list|get|enable|disable|update|remove."""
    try:
        info = chrome.userscript_dispatch(action, {"id": id, "name": name, "script": script})
        return _ok(f"userscript {action}: ok.", **info)
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


# ---- data: history + bookmarks --------------------------------------------------


@mcp.tool()
def chrome_history(
    text: str | None = None,
    startTime: str | None = None,
    endTime: str | None = None,
    maxResults: int = 100,
) -> CallToolResult:
    """Search local History (read-only copy; Chrome may be running)."""
    try:
        rows = chrome.history_search(text, startTime, endTime, maxResults)
        lines = [f"- {r['title'][:70]} <{r['url'][:110]}> ({r['last_visit'][:10]})" for r in rows]
        return _ok(
            f"{len(rows)} visit(s):\n" + "\n".join(lines) if lines else "(no matches)",
            visits=rows,
            count=len(rows),
        )
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_bookmark_search(
    query: str | None = None, folder: str | None = None, maxResults: int = 50
) -> CallToolResult:
    """Search local Bookmarks by title/URL, optionally within a folder."""
    try:
        marks = chrome.bookmark_search(query, folder, maxResults)
        lines = [f"- {m['title'][:70]} <{m['url'][:110]}> [{m['folder']}]" for m in marks]
        return _ok(
            f"{len(marks)} bookmark(s):\n" + "\n".join(lines) if lines else "(no matches)",
            bookmarks=marks,
            count=len(marks),
        )
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_bookmark_add(
    url: str | None = None,
    title: str | None = None,
    parentId: str | None = None,
    createFolder: bool = False,
    tabId: int | None = None,
) -> CallToolResult:
    """Add a bookmark (defaults to the active tab's URL/title)."""
    try:
        if url is None or title is None:
            target = chrome.resolve_target(tab_id=tabId)
            url = url or str(target.get("url", ""))
            title = title or str(target.get("title", ""))
        node = chrome.bookmark_add(url, title or url, parentId, createFolder)
        return _ok(f"Bookmarked {title}.", **node)
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_bookmark_delete(
    bookmarkId: str | None = None, url: str | None = None, confirm: bool = False
) -> CallToolResult:
    """Delete a bookmark by id or URL. DESTRUCTIVE — needs confirm=true."""
    if (denied := _require_confirm(confirm, "chrome_bookmark_delete")) is not None:
        return denied
    try:
        return _ok("Deleted.", **chrome.bookmark_delete(bookmarkId, url, None))
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


# ---- dialogs / downloads --------------------------------------------------------


@mcp.tool()
def chrome_handle_dialog(action: str, promptText: str | None = None) -> CallToolResult:
    """Accept or dismiss the open JavaScript dialog."""
    try:
        return _ok("Dialog handled.", **chrome.handle_dialog(action, promptText))
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_handle_download(
    filenameContains: str | None = None, timeoutMs: int = 60000
) -> CallToolResult:
    """Wait for a download to land in ~/Downloads (or CHROME_MCP_DOWNLOAD_DIR)."""
    try:
        return _ok("Download complete.", **chrome.wait_for_download(filenameContains, timeoutMs))
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


# ---- performance / gif / flows ----------------------------------------------------


@mcp.tool()
def performance_start_trace(reload: bool = False) -> CallToolResult:
    """Start a CDP tracing session (optionally reloading the tab)."""
    try:
        return _ok("Trace started.", **chrome.trace_start(reload))
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def performance_stop_trace() -> CallToolResult:
    """Stop tracing and save the trace JSON under the data dir."""
    try:
        info = chrome.trace_stop(save=True, prefix="trace")
        return _ok(f"Trace saved to {info['path']} ({info['events']} events).", **info)
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def performance_analyze_insight(path: str | None = None) -> CallToolResult:
    """Summarize a saved trace (top categories by time)."""
    try:
        info = chrome.trace_analyze(path)
        lines = [f"- {t['name']}: {t['ms']}ms" for t in info["top_ms"]]
        return _ok(
            "Top trace categories:\n" + "\n".join(lines) if lines else "(empty trace)", **info
        )
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def chrome_gif_recorder(
    action: str, recordingId: str | None = None, fps: int = 5, durationMs: int = 5000
) -> CallToolResult:
    """Record the tab: start -> capture -> export (PNG sequence + GIF if Pillow is present)."""
    try:
        if action == "start":
            info = chrome.gif_start(fps, durationMs, 800)
            return _ok(f"Recording {info['recording_id']} armed.", **info)
        if not recordingId:
            return _err("recordingId is required for capture/status/export.")
        if action == "capture":
            return _ok("Captured.", **chrome.gif_capture(recordingId))
        if action == "status":
            return _ok("Status.", **chrome.gif_status(recordingId))
        if action == "export":
            info = chrome.gif_export(recordingId, None)
            return _ok(f"Exported {info['frames']} frame(s) to {info['dir']}.", **info)
        return _err("action must be start|capture|status|export.")
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def record_replay_list_published() -> CallToolResult:
    """List saved flows (executed by record_replay_flow_run)."""
    try:
        flows = chrome.flows_list()
        lines = [f"- {f.get('id')}: {f.get('name', '')}" for f in flows]
        return _ok(
            "Flows:\n" + "\n".join(lines) if lines else "(no flows saved)",
            flows=flows,
            count=len(flows),
        )
    except chrome.ChromeError as e:
        return _err(f"ERROR: {e}")


@mcp.tool()
def record_replay_flow_run(
    flowId: str, variables: str | None = None, tabId: int | None = None, confirm: bool = False
) -> CallToolResult:
    """Run a saved JS-step flow. DESTRUCTIVE (runs stored code) — needs confirm=true."""
    if (denied := _require_confirm(confirm, "record_replay_flow_run")) is not None:
        return denied
    try:
        target = chrome.resolve_target(tab_id=tabId)
        args = json.loads(variables) if variables else {}
        if not isinstance(args, dict):
            return _err("variables must be a JSON object.")
        info = chrome.flow_run(target, flowId, args, 60000)
        status = "ok" if info["ok"] else "FAILED"
        return _ok(f"Flow {flowId}: {status}.\n" + "\n".join(info["logs"]), **info)
    except (chrome.ChromeError, json.JSONDecodeError) as e:
        return _err(f"ERROR: {e}")


def run() -> None:
    mcp.run()


if __name__ == "__main__":
    run()
