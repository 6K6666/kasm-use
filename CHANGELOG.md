# Changelog

## 0.5.2 — 2026-11-27

- **The first screenshot of a Server (Guacamole) desktop is no longer empty.** Right after the
  browser attached, the display had its size but no frame, and `kasm_look` returned a fully
  transparent image without saying so. The screenshot now waits up to 8 s for the first drawn
  pixel, and `kasm_look` adds its "Screen is blank" note when none arrives. Found in the first
  live run of this transport on 0.5.x, against an xrdp desktop.

## 0.5.1 — 2026-11-26

- **`kasm_start` answers within about a minute.** It used to wait up to five minutes for Kasm to
  report the session running, then keep trying a desktop it could not reach, which outlasts a
  public proxy's 100 s limit. It now returns `state: "preparing"` and points to `kasm_wait`.
  `kasm_wait` reports `preparing` (not `failed`) while Kasm itself is still starting the session.

## 0.5.0 — 2026-11-26

Fixes from an agent driving Ubuntu and Windows desktops end to end. Nothing here is specific to one
workspace: every new behaviour is switched on by an optional environment variable.

- **Out-of-range coordinates are refused.** `kasm_click`, `kasm_drag` and `kasm_scroll` used to
  clamp them silently, so a wrong x clicked something at the edge of the screen. The error names
  the screenshot size.
- **`kasm_start` says whether the desktop is ready.** It waits up to 20 s for the first drawn frame
  and returns `ready` and `state`. `kasm_look` says when the screen is still blank.
- **`kasm_wait`**, and a status file for slow workspaces. With `KASM_PILOT_WORKSPACE_ID` and
  `KASM_SESSION_STATUS_DIR`, sessions of that workspace take their state (`queued`, `preparing`,
  `ready`, `failed`) from `<dir>/<session_id>.json`. Until it is `ready`, `kasm_look` and the input
  tools refuse with an error that points to `kasm_wait`; a session nobody reports fails after 60 s.
  `kasm_list` shows each session's state.
- **`kasm_paste`**: sets the session clipboard (KasmVNC's binary clipboard message) and presses the
  paste shortcut. Fast, and exact for any characters.
- **`kasm_type` refuses what a session cannot type.** When the status file says `"typing": "ascii"`,
  text with other characters is refused, naming them, instead of being typed with holes in it.
- **Several client processes can share `KASM_SESSION_STATE_FILE`.** Saves take a file lock and
  merge; every tool call re-reads the file. Before, each process overwrote the others' records.
  The file gains an optional `image_id` per session; 0.4.0 cannot read a file that has one.
- **`KASM_USE_SCREEN=WIDTHxHEIGHT`** asks each new desktop for that size. The client now announces
  ExtendedDesktopSize and follows resizes; on Kasm 1.19 that means a full-frame request is answered
  with the size alone and the pixels come with the next incremental request.
- **Key names**: `plus`, `minus`, `equal`, `comma`, `period`, `slash`, `backslash`, `capslock`. The
  `kasm_key` description and the unknown-key error list every name.
- **`KASM_WORKSPACE_ALLOW`** limits which workspaces are listed and startable. `kasm_start` has no
  default workspace any more, and its examples no longer name workspaces you may not have.
- **A call that names no session means one this client process started** (or was given by ID), not
  the newest session in a state file shared with other clients. After a restart, a single tracked
  session is still the default; with several, the call must pass `session_id`.
- `kasm_wait` reports `failed` with the reason when a desktop could never be reached, and a status
  file left at `queued` or `preparing` for 15 minutes counts as a dead controller.
- **Sessions an agent is using no longer expire after an hour.** Each connection renews the Kasm
  keepalive (at most every five minutes), as an open browser tab would.
- Messages KasmVNC sends unprompted (another viewer joining or leaving, clipboard contents) are
  skipped instead of ending the call with "unexpected VNC message".

## 0.4.0 — 2026-10-10

- **`kasm_drag`**: press at one point, move with the button held, release at another. For
  dragging menu items, tabs and windows, moving sliders, selecting text, and drag and drop. It
  moves in small steps with short pauses at each end, since many toolkits ignore a single jump.
  Verified on Kasm 1.19.0 Chrome: selecting a line of text and reordering tabs.

## 0.3.1 — 2026-10-02

- Listed in the official MCP Registry as `io.github.rchurro/kasm-use` (`server.json`; releases
  publish there automatically after PyPI). No code changes.

## 0.3.0 — 2026-10-02

Scope hardening, prompted by a question about what an agent could reach.

- **Tools only act on sessions kasm-use started** (in this process). `kasm_look`, `kasm_click`,
  `kasm_type`, `kasm_key`, `kasm_scroll` and `kasm_stop` refuse any other `session_id`, and
  `kasm_list` only shows kasm-use's own sessions. Before, an agent could screenshot or stop any
  session the API key could see, including other users'. **Breaking:** "stop all my sessions"
  style requests now only cover sessions kasm-use started.
- **Removed the exec + `xdotool` fallback and the screenshot-API path.** On stock images the
  fallback's input silently did nothing while reporting success. Everything now goes over VNC.
  The API key no longer needs **Sessions Modify** or **Session Recordings View**.
- `kasm_start` refuses to continue (and cleans up) if Kasm doesn't return a session token.
- Documented the security model: Kasm API keys are server-wide, and session tokens are per-user.
- Added offline tests (scope rules, DES, ZRLE, pointer format) and a CI workflow.

## 0.2.0 — 2026-10-01

kasm-use now drives sessions through KasmVNC directly instead of `xdotool`.

- **Input over VNC.** Clicks, typing, keys and scrolling go to the session's KasmVNC server through
  Kasm's proxy. No more installing `xdotool` as root at session start, so `kasm_start` returns as
  soon as the session is running (about 90 s faster), and it works with any image.
- **Live screenshots over VNC.** `kasm_look` reads the framebuffer directly (~1 s, PNG). Kasm's
  screenshot API returned stale frames whenever nobody was viewing the session, which made agents
  retry actions that had already worked.
- **4–5x faster end to end.** A three-workspace demo (Chrome to a website, Terminal commands,
  Minetest world creation) went from 191–402 s per workspace to 39–77 s, with no retries.
- Sessions started outside kasm-use fall back to the 0.1.0 behaviour (exec + `xdotool`).
- Still standard-library only, so it installs with `--no-deps` alongside other agents.

## 0.1.0 — 2026-09-30

First release: eight tools for starting, viewing and driving Kasm sessions (screenshot API +
`xdotool` via exec); stdio and streamable-HTTP transports; Hermes Agent plugin.
