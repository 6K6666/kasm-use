# Changelog

## 0.5.9 — 2026-11-27

From ChatGPT's second stress test of the three desktops.

- **With several sessions of your own, say which.** A call without `session_id` went to the session
  started last; with Ubuntu ready and Windows preparing, a look and a key press meant for Ubuntu
  went to Windows. Such a call is now refused with the IDs and workspaces to choose from. A client
  with one session is unaffected.
- **A desktop can say what it cannot do.** `kasm_paste` and `kasm_scroll` answered `ok` on a macOS
  guest, which has no clipboard link and ignores the wheel, and nothing happened. The status file
  may carry `unsupported` (tool name to sentence); the tool then refuses with that sentence and
  sends nothing.
- **`verify_after` returns the screen's size** (`screen`), so an agent notices when the desktop was
  resized, for example because the owner opened the session in a browser.
- A session whose status is `failed` is "not usable", no longer "failed to start": the guest may
  have run for an hour before it stopped.
- Descriptions: `kasm_wait` says that a timeout outside 5 to 90 is brought into range;
  `verify_after` says that a still screen is not a finished command; `kasm_paste` mentions a
  terminal's multi-line paste confirmation.

## 0.5.8 — 2026-11-27

- **`kasm_attach`: take over a session the owner launched from the dashboard.** The owner starts a
  desktop under the instance's own Kasm user, signs in to something there, and hands the desktop
  to the agent without sharing a credential. Until now only Windows pilot sessions could be
  claimed; an Ubuntu session prepared this way was out of reach (found by ChatGPT). Off unless
  `KASM_ATTACH=true`. With it on, `kasm_list` shows the user's untracked running sessions under
  `attachable`. The session must be running, belong to `KASM_USER_ID` and be a container
  workspace; Kasm's connection link is checked against the session and the user as for a pilot.
  `kasm_stop` **releases** an attached session and never ends it, and an attachment is not written
  to the state file, so no other client process can mistake it for a session kasm-use may end.

## 0.5.7 — 2026-11-27

Found by a usability stress test of Ubuntu Noble, Windows 11 and macOS Sequoia desktops, driven by
ChatGPT through this server.

- **`kasm_stop` succeeds for a session Kasm already ended.** Whatever prepares a desktop may end the
  Kasm session of a launch that failed; `kasm_stop` then returned Kasm's HTTP 400 ("Invalid
  kasm_id") and kept the record. It now asks Kasm's session list: a session that is no longer there
  is reported stopped, with a note. If the list cannot be read or still shows the session, the
  error is returned as before.
- **`kasm_start` waits for a prepared desktop's first status.** A queued session was reported
  `preparing`, because `kasm_start` answered before the controller's next pass had written the
  status file. It waits up to 10 s for that file, returns its `detail`, and says so when the
  session is queued.
- **`verify_after` returns the screen once it has stopped changing.** The screenshot was taken the
  instant the input was sent (seven of them showed a screen that had not reacted yet or was in
  mid-animation). It is now sampled every half second until a sample equals one of the two before
  it, so a blinking cursor counts as still, for 4 s at most; the result says `settled` and
  `waited_seconds`. A program that takes seconds to open can still appear later.
  One look at a 1280x800 desktop takes half a second, so the interval counts the look itself.
- `server.json` carries the package version again (it said 0.4.0).

## 0.5.6 — 2026-11-27

- **`press_enter` waits half a second after the text.** XFCE's application finder on a 1024x768
  desktop ignored an Enter sent 30 ms after the last character (3 of 3 tries), so `kasm_type` with
  `press_enter` typed a program's name and nothing started. With a pause of 0.15 s or 0.5 s it
  started every time (3 of 3 each); a 1280x800 desktop did not show the problem. Found by ChatGPT
  driving Ubuntu Noble.

## 0.5.5 — 2026-11-27

- **Status files for more than one workspace.** `KASM_STATUS_WORKSPACE_IDS` lists the image IDs of
  further workspaces whose desktops an outside controller prepares; their sessions are gated on
  `<session_id>.json` like the one named by `KASM_PILOT_WORKSPACE_ID`. Added for a macOS workspace
  served next to the Windows one. The two "controller" failure messages no longer say Windows.

## 0.5.4 — 2026-11-27

Found by driving macOS Sequoia 15 (a QEMU guest) through a Server (VNC) workspace and through a
container workspace whose viewer runs a VNC client. Verified live on both, and re-checked on Ubuntu
Noble, Windows 11 (Notepad, through its FreeRDP viewer) and an xrdp desktop over Guacamole.

- **Typing presses Shift itself for capitals and shifted symbols**, as a keyboard does. QEMU's VNC
  server presses the key a character sits on and adds no Shift: `abc ABC 123 !@# <>? ~ (x) {y} |` arrived
  as ``abc abc 123 123 §./ ` 9x0 [y] \``, and through a KasmVNC viewer capitals and `(`, `)`, `<` were
  lost. A line holding every shifted US symbol now arrives exactly on all five desktops.
  Shifted characters take four key events, so they type at half the speed of the others.
- **A drag on a Server (Guacamole) desktop moves in steps.** Press, one jump and release were sent
  in one burst, which left a macOS window where it was. It now presses, pauses 150 ms, moves in 12
  steps 30 ms apart, pauses and releases, like the VNC transport (about 0.7 s for a drag).
- **`cmd`, `command` and `option` are key names** (`cmd+space`, `cmd+q`). They are `super` and `alt`.
- Not fixable here: the mouse wheel does not reach a macOS guest (the same result with clicks sent
  one at a time, and through the viewer). `page_up` and `page_down` scroll. There is no clipboard
  link to the guest, so `kasm_paste` cannot be used; `kasm_type` is exact for ASCII.

## 0.5.3 — 2026-11-27

Found by driving a Windows 11 desktop through a Server (Guacamole) workspace; all three were
measured there and are covered by browser tests.

- **Typing no longer loses Shift or Unicode characters on Windows.** Key events were sent in one
  burst; Notepad received `<ok> (a)` as `,ok. 9a0` and cut the text short. Each key event is now
  followed by a 20 ms pause (about 25 characters a second), which was byte-exact.
- **The first input after attaching is no longer dropped.** Windows ignored everything sent within
  about a second of a new RDP connection showing its desktop. The first click or keystroke after an
  attach now waits until 3 s after the first drawn frame.
- **A solid black first frame counts as blank.** Windows sends one before the desktop; the
  screenshot now waits for a frame that is not one flat colour (a new attach only), and `kasm_look`
  says "Screen is blank" when none arrives.

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
