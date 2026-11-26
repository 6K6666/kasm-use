# Dockur Windows 11, Kasm, and SDK2

## Browser pilot

Use a dedicated RDP Server and Server Workspace in the CT115 deployment zone.
Select **Web Native** and give the pilot group access to that workspace.
Give the Server the exact private RDP endpoint and the current clone's
`kasmtest` credentials. Limit it to one user and one session. Keep its
per-clone password in the supported Kasm credential configuration.
If using Kasm Desktop Service, enroll this guest with its own registration
token after credential cleanup.

After a CSV server import, check **Connection Info** in the Server editor.
Kasm 1.19.0-ca1 imported a populated JSON cell as a string during the pilot.
The resulting status request returned HTTP 500 in `get_port_map`. Saving
the value as a JSON object through the Server editor corrected the binding.

The host controller owns the Dockur container, overlay, credentials, and
endpoint. Start the private relay only after the first-boot run has proved
OOBE completion, credential rotation, setup-account cleanup, and a working
Windows desktop. Use the controller's exact session ID for both the relay and
the Kasm binding. The source candidate and POC stay powered off.

Two fresh v5 trials reproduced an Explorer crash on the first successful RDP
sign-in. When the XAML audit passes and Explorer alone is missing, sign out of
that exact clone's Windows session once, reconnect, and rerun the post-login
audit before applying the Kasm binding. A failed second audit keeps the guest
out of service. This preflight uses a real desktop session and does not
treat RDP authentication alone as readiness.

Build kasm-use with `INSTALL_GUAC=1` or install the `guac` extra and
Chromium. Test start, look, click, drag, vertical scroll, typing, key chords,
reconnect, and stop in the dedicated workspace. Test the existing KasmVNC
workspace too. Browser sessions get a separate context and a validated
Kasm login link for the same session and owner. The driver keeps the link
and browser state in memory. Persisted recovery data records whether to use
KasmVNC or Guacamole.

The first Guacamole frame can show Windows signing in. Wait for the desktop
and taskbar before sending input. Confirm a guest-side result such as text
entered in Notepad. A successful input tool response confirms dispatch.

A Kasm session ending and a host trial retiring are two steps. Wait for the
exact Kasm session to disappear, revoke the Server binding and remove the
relay, then retire that guest through the Dockur controller. Verify the
container, writable disk, credential directory, and listeners are gone.
Verify a second launch from the sealed image with a distinct SID. The
published Kasm 1.19 developer endpoints cover session operations. The pilot
also needs an administrator to manage its dedicated Server and Workspace.
See the [Kasm 1.19 developer API](https://docs.kasm.com/docs/1.19.0/reference/developer-api)
and [fixed infrastructure guide](https://docs.kasm.com/docs/1.19.0/how-to/workspaces-sessions/server-workspace/fixed-infrastructure).

## Live pilot result

On November 26, 2026, the staged kasm-use image started a Windows Server
Workspace on CT115, captured its desktop, opened Start with a click,
entered an exact marker inside Windows, reconnected, and ended the session.
Guacamole's key instruction requires numeric press and release states
(`1` and `0`). Sending booleans left the Windows keyboard inactive even
though the browser accepted the call. The Chromium regression fixture now
rejects boolean key states.

After stopping the Kasm session, the trial relay, container, writable overlay,
and private credentials were removed and checked for absence.

## SDK2

**Desktop interaction needs zero SDK2 changes.** Route look and input through
kasm-use's existing desktop tools. A Dockur session may be visible to a user
while its container and session ledger are managed independently of SDK2.

- Keep the SDK2 worker catalog and grants scoped to their existing Linux
  worker image and owners. `FixedKasm19API` in
  `src/kasm_exec_core/g1_fixed_kasm19_api.py` binds creation and observation
  to a fixed Kasm image and an allowed owner. The Windows Server Workspace
  config belongs in the Dockur integration.
- Keep the SDK2 Docker/host attestation unchanged. Its fixed backend observes
  the worker container and Linux guard state. A Dockur container contains
  QEMU, and its Windows guest has separate identity, storage, and processes.
  A Kasm Server session ID alone supplies no Windows execution evidence.
- If native Windows SDK2 execution is added later, define a separate worker
  contract for the guest. Bind the authorized owner, Kasm session ID, Dockur
  ledger session ID, container ID and guest identity before granting work.
  Add Windows-native process-tree cancellation and deadlines, constrained
  file and artifact handling, results tied to that identity, and teardown
  evidence covering the Kasm session, exact VM, overlay, credentials and
  relay. Keep raw QGA, host Docker, and arbitrary PowerShell outside the
  public SDK2 tool surface. The SDK2 broker and router would need an explicit
  new backend and capability policy for that contract.
