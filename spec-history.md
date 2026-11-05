# Lab 4 — Specification history

Sources: `git log -p SPEC.md`, associated implementation commits, regression
fixtures, and the current `CHANGELOG.md`. Reasons below are marked as recorded
or inferred; historical test counts are reported records, not new test runs.

## Spec revisions — 5 November 2026

- **`ec134c6` — Initial specification and first recorded build.** Introduced a
  readable per-session thread, incremental streaming, tool/result pairing,
  repeated-history deduplication, raw inspection, side-call filtering, and
  loopback-only operation. The original scope was Anthropic Messages. The
  commit message, “inital commi”, gives no build-derived reason. The spec's
  input section records repeated conversation history, streaming blocks, quota
  probes, and title calls; these explain the design, but their observation
  session is not available in the repository. Spec and implementation were
  committed together, so this commit cannot prove a separate spec-first build.

- **`0f83daf` — Context visibility and dual-provider support.** Added four
  missions: hide injected conversation context, hide environment blocks, support
  Claude and Codex, and remove known protocol wrappers from readable messages
  while retaining original raw payloads. The message records the requested
  scope, not a specific failed build. The inferred motivation is clutter in the
  readable thread and the original Codex raw-only limitation (source: spec diff).
  Implementation `fe51d32` subsequently changed the existing proxy, added context
  filters and launchers, and introduced provider/context regression coverage.

- **`16fc7be` — Five further improvements.** Specified provider selection and
  automatic routing, reliable Codex history, actionable connection errors,
  one-command startup, and real-browser regression tests. The message lists the
  scope but does not report a particular live failure. The inferred gaps are
  manual provider setup, duplicate history during tool cycles/resume/compaction,
  and insufficient startup/error feedback (source: added requirements).

- **`87f4bf8` — Implementation status and verification.** Updated the same spec
  to record all five improvements as implemented, with 30 Python tests,
  37 JavaScript assertions, and 9 Chromium tests reported passing. The associated
  code and local browser fixtures cover history, routing, context visibility,
  reconnects, and error recovery. This is a recorded implementation/validation
  milestone; fixture results do not establish successful live-provider use.

- **Current spec snapshot — startup reliability.** Adds the rebuild verification
  section: monitor an owned proxy while the agent runs, fail clearly if it exits,
  and clean up only owned processes. The recorded reason is that the launcher
  previously waited only for the agent and could leave it without a working
  connection (source: `CHANGELOG.md` and current `start.py`/test changes). Records
  report 33 Python tests, 37 JavaScript assertions, 9 Chromium tests, and a local
  startup/shutdown smoke check. Live authentication was explicitly not tested.

## Rebuilding versus editing

The exact switch and its reason were not recorded. Existing-code editing is
demonstrable by `fe51d32`, which patches the proxy from `ec134c6`; later commits
continue that approach. No available evidence establishes repeated clean-folder
builds or justifies inventing a reason for the switch.

## Findings in the traffic

The initial spec records that one short prompt carries system instructions,
tool definitions, and repeated history; replies arrive as streaming events, and
tool results refer back to earlier calls. Connectivity/quota/title requests also
create side traffic. Later regression fixtures model injected environment
context and Codex history rewrites, showing why readable/raw views must differ
and why deduplication matters. These are documented inputs and fixture-backed
findings; fresh live traffic and a working live demonstration remain to be shown
in the lab. Both partners should upload the final `SPEC.md` and this file to
**Lab 4 report**, each under their own name.
