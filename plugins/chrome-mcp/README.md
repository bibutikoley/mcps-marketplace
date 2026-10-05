# chrome-mcp

Local Chrome automation for AI coding agents. Drives **your real Chrome**
(current profile, logins, settings intact) over the Chrome DevTools Protocol —
no extension install, no separate browser process, no cloud, no downloads.

Pinned to `v0.5.4` (recommended — reproducible; substitute a newer tag to upgrade):

```bash
claude mcp add chrome-mcp -s user -- uvx --from "git+https://github.com/bibutikoley/mcps-marketplace@v0.5.4#subdirectory=plugins/chrome-mcp" chrome-mcp
```

To track `main` instead (mutable — you get updates without bumping, but
builds are not reproducible), drop the `@v0.5.4` from the URL.

Any MCP client over stdio (no marketplace needed):

```json
{
  "mcpServers": {
    "chrome-mcp": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/bibutikoley/mcps-marketplace@v0.5.4#subdirectory=plugins/chrome-mcp", "chrome-mcp"]
    }
  }
}
```

## Prerequisites

- Desktop Chrome or Chromium (macOS, Linux, Windows).
- Launch it once with a debugger port (or point `CHROME_MCP_PORT` at yours):

```bash
google-chrome --remote-debugging-port=9222
```

- `uv` installed (provides `uvx`). Python 3.12+.
- Bookmarks/History are read from your local Chrome profile; no automation
  permission prompts, no Full Disk Access.

## First run

1. Start Chrome with the flag above.
2. Call `health_check` — it reports browser, debugger host/port, page count.
3. Call `get_windows_and_tabs` — note the 1-based `tabId`s.
4. `chrome_read_page` before every action; coordinates come from `ref_*` centers.

## Tools (31)

Parity port of the `mcp-chrome` extension toolset, adapted to CDP:

| Area | Tools |
|------|-------|
| Tabs | `get_windows_and_tabs`, `chrome_navigate` (url/refresh/back/forward/newWindow), `chrome_close_tabs` (confirm), `chrome_switch_tab` |
| Observe | `chrome_screenshot` (inline PNG), `chrome_read_page` (`ref_N` tree), `chrome_get_web_content` (text/HTML), `search_tabs_content` (local TF-IDF, no vector downloads) |
| Interact | `chrome_computer` (click/scroll/type/key/hover/screenshot), `chrome_click_element`, `chrome_fill_or_select`, `chrome_keyboard`, `chrome_request_element_selection` (returns candidates; no overlay without the TS extension) |
| Script | `chrome_javascript` (confirm), `chrome_console` (2s snapshot + regex/level filters), `chrome_upload_file`, `chrome_inject_script` (confirm), `chrome_send_command_to_inject_script`, `chrome_userscript` (local create/list/get/enable/disable/update/remove) |
| Network | `chrome_network_capture` (start/stop resource-timing summary), `chrome_network_request` (page-context fetch, non-GET needs confirm) |
| Data | `chrome_history` (local History copy), `chrome_bookmark_search`, `chrome_bookmark_add`, `chrome_bookmark_delete` (confirm) |
| System | `chrome_handle_dialog`, `chrome_handle_download` (polls the download dir) |
| Perf/media | `performance_start_trace`, `performance_stop_trace`, `performance_analyze_insight`, `chrome_gif_recorder` (start/capture/status/export; GIF file needs Pillow, otherwise PNG sequence) |
| Flows | `record_replay_list_published`, `record_replay_flow_run` (confirm; JS steps with `{{var}}` substitution) |

## Differences from the extension build

- `tabId` is the 1-based index from `get_windows_and_tabs`, not the Chrome
  numeric tab id (CDP `/json` has no window grouping).
- `chrome://` pages cannot attach a debugger; navigate to http(s) first.
- Network capture reports resource-timing (URL + duration), not full bodies —
  use `chrome_network_request` for bodies.
- GIF export writes a PNG sequence always; the `.gif` file additionally
  requires Pillow (`uv pip install pillow` — optional, never required).
- `chrome_request_element_selection` returns candidates instead of opening an
  overlay picker.

## Access scope

No allowlist variables for reads: tabs, history, and bookmarks are read-only
views of your own browser. Writes are the bookmark file plus tab navigation.

| Variable | Default | Effect |
|----------|---------|--------|
| `CHROME_MCP_HOST` | `127.0.0.1` | Debugger host (keep loopback; never expose over a network) |
| `CHROME_MCP_PORT` | `9222` | Debugger port |
| `CHROME_MCP_TIMEOUT_MS` | `15000` | CDP round-trip timeout |
| `CHROME_MCP_PROFILE_DIR` | auto | Override Chrome profile root (testing) |
| `CHROME_MCP_DOWNLOAD_DIR` | `~/Downloads` | Where `chrome_handle_download` polls |
| `CHROME_MCP_DATA_DIR` | temp | Userscripts, flows, traces, GIF frames |

## Destructive tools (need `confirm=true`)

`chrome_close_tabs`, `chrome_javascript`, `chrome_inject_script`,
`chrome_network_request` (non-GET), `chrome_bookmark_delete`,
`record_replay_flow_run`. Everything else acts immediately — keep your MCP
client's tool-approval prompts on.
