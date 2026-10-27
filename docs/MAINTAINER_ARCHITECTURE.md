# Maintainer architecture and roadmap

Status: **design baseline**. This document describes intended isolation and
evolution, **not** proof that privileged broker functionality is deployed.

This is a maintained fork of [rchurro/kasm-use](https://github.com/rchurro/kasm-use).
Preserve its MIT license and attribution, VNC-driven desktop interaction and
operator takeover. Do not merge unrelated privileged MCP servers into the
desktop entrypoint.

## Purpose

Enable multiple AI clients to use **disposable Kasm workspaces** for visual
computer-use and controlled development tasks, without granting unrestricted
access to the Kasm host, other sessions, production network or storage.

The design has **one maintained project** but independent runtime security
boundaries. A common client experience does not mean one shared server process.

| Component | Responsibility | Isolation and capabilities |
| --- | --- | --- |
| Desktop MCP (this Python package, SDK 1.x) | Start, look, click, type, drag, scroll, stop; optional sanitized read-only status | Independent service process, minimal API permissions, session allowlist, no exec/file/admin tools |
| Execution MCP (separate package/service, SDK 2.x initially) | Grant/revoke a verified lease, bounded run/read/write in a verified disposable worker | Separate process/UID/venv/port/credentials, trusted caller identity, session and full container ID proof, audit |
| Diagnostics | Frame and bottleneck metrics, sanitized session health | Read-only owner-scoped endpoints; privilege reviewed before exposure |
| Host administration | Kasm user, group, license, node and deployment changes | Independent explicit operator/admin approval; never exposed to general development agents |
| Artifact broker | Import/export reviewed task artifacts to durable storage | Trusted transfer service; no worker-mounted NAS, no Docker socket in workers |

The desktop endpoint **must not** accept privileged execution requests. The
execution endpoint **must not** accept the desktop's shared bearer token as
proof of agent identity. SDK 1 and 2 must not share a Python environment merely
to present a single application name.

## Source of truth and promotion

- **GitHub fork:** public, vendor-neutral code, tests, architecture decisions
  and safe fixtures. Keep the upstream git remote distinct and periodically
  compare/upstream sync via reviewed pull requests.
- **Private deployment inventory:** credentials, internal IPs, host paths,
  deployment policies, firewall rules and trusted runtime materials belong
  outside this public repository.
- **Durable artifact storage:** test reports, development snapshots, larger
  task outputs and non-public operational instructions belong in an approved
  managed artifact store, with a separately managed access policy.
- **Environments:** development -> disposable test -> explicit operator review
  -> deployment. No merge-to-main step deploys production services by itself.

The existing desktop registry guard is defense in depth: it catches accidental
new tool names; it **does not authenticate any caller**. The Kasm API key is
server-wide and returned session tokens may be valid for all sessions of the
configured user. Continue enforcing process-created session scope and prefer a
dedicated Kasm user for agents. Do not treat MCP tool descriptions as policy.

## Execution trust requirements

Before enabling execution or file writes:

1. Authenticate each MCP client with a verifiable trusted issuer, audience,
   subject and operation scopes. Distinguish human user identity from actual
   agent/client authorization. Reject unsigned caller-supplied IDs.
2. Admit only verified sessions owned by the caller and confirm exact Docker
   container identity, image eligibility, user, runtime security and network.
3. Grant short-lived, operation-scoped, revocable leases bound to client,
   session, container, worker and broker instance. Revalidate before execution.
4. Execute unprivileged code within the disposable worker using bounded argv
   transport, exit status, deadline, cancellation, concurrency and output caps.
   Avoid string-filtered shell commands as an authorization mechanism.
5. Validate file paths and archives against the **worker filesystem**, enforce
   quotas, reject link traversal, and separate read/write from artifact publish.
6. Treat any browser or repository content as untrusted input. Apply egress
   isolation and avoid credentials within throwaway workers.
7. Prove negative tests: foreign session, expired/revoked lease, different
   client, swapped container, Docker outage, helper restart, forbidden target,
   oversized output, transport interruption and wrong-endpoint tool calls.
8. Audit sensitive actions without secrets, and support reconciliation, worker
   expiration and cleanup after outages.

OAuth/Cloudflare Access, private agent network connections and cloud clients
are deployment choices; do not infer that network presence or an HTTP header
alone proves a trusted agent principal.

## Carefully selected external ideas

**From a separate Kasm administration MCP:** only consider reimplementing
frame timing, bottleneck metrics and recording metadata from the official Kasm
API, once their permission requirements and ownership mapping are verified.
Do not import its raw shell command/file implementation or passwordless-login
tools into this desktop package.

**From Shellby MCP:** adopt the *user experience* of request IDs, polling,
bounded transcript cursors, named shell contexts and idle cleanup **inside
isolated, leased Kasm workers**. Do not adopt its model of arbitrary shell access
to a persistent personal Mac or its remote subject-binding implementation.

## Staged work and acceptance gates

1. **Repository baseline (this phase):** preserve upstream behavior, add
   explicit desktop tool registration boundary, test forbidden tool names,
   establish design/review docs, and keep production untouched.
2. **Reconcile prototypes:** review legacy experimental desktop additions.
   Retain optional sanitized session inspection/capability reporting; move
   image/profile attestation and archive/transfer logic to the broker rather
   than duplicating authorization in the desktop server.
3. **Execution broker test deployment:** prove real isolated worker command
   stdout/stderr/exit-code, cancellation and owner verification with a trusted
   identity route. Keep the data plane disabled until all gates pass.
4. **Multi-client admission:** add independent identities for ChatGPT Web and
   local coding agents, with separate cloud compatibility work only as needed.
5. **Diagnostics and artifacts:** implement owner-scoped frame/bottleneck
   statistics; durable, quota-limited artifact broker and verified upload
   flow, preserving explicit security boundaries.
6. **Maintenance:** pin/test dependencies, scan releases, keep upstream
   attribution, run CI across supported Python versions, version changes,
   review PRs and verify backwards compatibility before promoting.

**Non-goals now:** deploying an OAuth issuer, exposing new public endpoints,
rewriting KasmVNC, adopting third-party shell/admin code wholesale, or changing
existing Kasm production services.
