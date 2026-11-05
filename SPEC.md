# SPEC: Live traffic dashboard for proxy.py

## Goal

While `proxy.py` is running, open `http://localhost:8042` and watch the conversation between the agent (Claude Code) and the model as it happens, readable at a glance. When the agent is working, messages pile up fast. The page must let you:

- tell apart, instantly, **your prompt**, **the model's reply**, **a tool call**, and **a tool result**;
- follow the conversation as it grows, without rereading what you've already seen;
- drop down to the raw request/response whenever you need the exact bytes.

Readability is the main requirement, not polish added at the end.

## What the proxy sees (input)

- Requests: `POST /v1/messages?beta=true` with a JSON body: `model`, `system`, `tools`, `messages` (the **entire** conversation so far), `max_tokens`, `thinking`, `stream: true`, `metadata`. Headers include `X-Claude-Code-Session-Id`.
- Responses: Server-Sent Events: `message_start`, `content_block_start`, `content_block_delta` (`text_delta`, `input_json_delta`, `thinking_delta`), `content_block_stop`, `message_delta` (stop reason, usage), `message_stop`, `ping`, `error`. Errors may also come back as plain JSON (e.g. the 429 `rate_limit_error`).
- Side traffic: `HEAD /api/hello` (connectivity check), a `max_tokens: 1` "quota" probe, a small session-title call with no tools.

## Decisions made

| # | Decision | Choice |
|---|----------|--------|
| D1 | Main view | **One growing thread per session.** Each call adds only the messages that are new since the previous call; a per-call "raw" toggle shows the full request and response. |
| D2 | Architecture | **Inside proxy.py, on its own port.** Forwarding stays on 8041; the dashboard is served on 8042 (`--ui-port`). One process, Python standard library only. |
| D3 | Side calls | **Collapsed, filterable.** Shown as one-line rows in the timeline, hidden by default behind a "show side calls" toggle. |
| D4 | Streaming | **Live.** Reply text and tool-call arguments fill in as the stream arrives, with a "streaming…" marker until `message_stop`. |

## Design

### Server (changes to proxy.py)

1. **Forwarding is unchanged.** Bytes still go to the agent exactly as they arrive. The dashboard is a side effect and must never delay, alter, or break forwarding; any dashboard error is caught and logged to the terminal.
2. **Event bus.** The proxy records each call as a sequence of events and keeps them in an in-memory ring buffer (default: last 2,000 events). Each connected browser gets a queue.
3. **SSE parsing on the side.** While forwarding each response chunk, the proxy also feeds it to an incremental SSE parser (split on blank lines, read `event:` / `data:`), which emits dashboard events.
4. **Dashboard server** on `127.0.0.1:8042` (localhost only):
   - `GET /` serves the page: one self-contained HTML file with inline CSS and JS, no build step and no CDN, so it works offline.
   - `GET /events` is a Server-Sent Events stream: first a replay of the buffer, then live events. Reconnects resume with `Last-Event-ID`.
5. The terminal printout stays as it is.

### Event model (server → page)

| Event | Fields |
|-------|--------|
| `call_start` | `call_id`, `session_id`, `time`, `method`, `path`, `model`, `kind` (`main` / `side`), `new_messages` (only the delta, see below), `system_summary` (length plus first line), `tools` (names only), `request_raw_ref` |
| `block_start` | `call_id`, `index`, `type` (`text` / `tool_use` / `thinking`), `tool_name`, `tool_use_id` |
| `block_delta` | `call_id`, `index`, `text` or `partial_json` |
| `block_stop` | `call_id`, `index` |
| `call_end` | `call_id`, `status`, `stop_reason`, `usage` (input, output, cache read, cache write), `duration_ms`, `error` (if any) |

Raw request and response bodies are kept server-side and fetched on demand (`GET /raw/<call_id>`), so the live stream stays light.

### Building one thread from repeated history (D1)

- Group calls by `X-Claude-Code-Session-Id`.
- For each session, remember a hash of every message already shown. On a new call, walk `messages` from the start: the matching prefix is old, and everything after it is new and gets rendered.
- If the prefix doesn't match (context was compacted or cleared), insert a divider ("history rewritten by the agent: N messages replaced") and continue from the new history. Never silently drop or duplicate messages.

### Classifying side calls (D3)

A call is **side** if any of these hold: it isn't `POST /v1/messages`; `max_tokens <= 1`; or it has no `tools` and its `system` is short (title generation). Everything else is **main**. The classification is shown on the row so it can be checked by eye.

### Page layout

- **Header:** connection status (live / reconnecting), session picker (latest session by default), filters (side calls, thinking, system reminders), and a search box.
- **Thread** (main area), one entry per message part, each with a distinct look that relies on **label + icon + color**, never color alone:

| Part | Look |
|------|------|
| Your prompt (user text) | right-aligned bubble, label "You" |
| Model reply (assistant text) | left-aligned, label "Model"; Markdown rendered (code blocks, lists) |
| Tool call (`tool_use`) | compact card: tool name as title, key argument inline (e.g. the command or file path), full JSON input collapsible |
| Tool result (`tool_result`) | card attached under its call (matched by `tool_use_id`), output in monospace, collapsed past ~15 lines, red border if `is_error` |
| Thinking | dim, collapsed, "thinking (N tokens)"; shows "redacted" when the content is omitted |
| System reminders / injected context inside user turns | dim, collapsed, labelled "injected by agent" |

- **Call footer**, a thin line after each model turn: model, duration, tokens in/out, cache read/write, stop reason, and a "raw" link.
- **Following the conversation:** auto-scroll to the newest message while you're at the bottom. If you scroll up, auto-scroll pauses and a "N new ↓" button appears. A streaming reply shows a blinking cursor.
- **Big content stays scannable:** the system prompt and tool definitions appear once per session in a collapsed "Setup" panel, not in the thread. Long text is truncated with "show more".
- Works in a normal browser window down to ~800px wide; light and dark themes follow the OS.

### Privacy and safety

- Both servers bind to `127.0.0.1` only.
- `Authorization` and `x-api-key` are redacted before anything reaches the event bus or raw store, just as in the terminal printout.
- Nothing is written to disk by default.

## Non-goals (for this version)

- Persisting traffic across proxy restarts.
- Editing, replaying, or blocking requests (the proxy stays read-only).
- Non-Anthropic formats (e.g. Codex via `--upstream`): those calls appear as raw-only rows.
- Authentication or remote access.

## Acceptance criteria

1. With the proxy on 8041 and the page open on 8042, a prompt typed in Claude Code appears in the thread within 1 s, and the reply streams in live.
2. In a session with at least 10 tool calls, each of the four kinds (prompt, reply, tool call, tool result) is identifiable without reading the text, and each tool result sits under its own call.
3. Each message appears exactly once even though every request repeats the full history.
4. Side calls (hello check, quota probe, title call) are hidden by default and appear as one-line rows when the filter is on.
5. Reloading the page restores the current session from the buffer.
6. Closing the browser or a dashboard crash never affects the agent; forwarded bytes are identical to before.
7. Still standard library only: `python proxy.py` with no installs.

## Open decisions (recommended default in **bold**)

1. **Where the page lives:** **inline string in proxy.py (single file)** vs. a separate `dashboard.html` next to it.
2. **Multiple sessions at once:** **session picker showing one at a time** vs. all sessions interleaved with color tags.
3. **Markdown rendering without libraries:** **a small hand-written subset (code blocks, inline code, lists, bold)** vs. plain text.
4. **Buffer size / memory cap:** **2,000 events, large raw bodies kept only for the last 50 calls** vs. unbounded.
5. **Export:** **none in v1** vs. a "download session as JSON" button.
6. **Token / cost summary per session** (totals of input, output, cache): **show totals in the header** vs. omit.

## Requested update: four implementation missions

1. **Hide conversation context by default.** Agent-injected conversation context,
   system reminders, developer instructions, and setup metadata must not appear in
   the normal conversation thread, previews, or search. Keep original bytes in the
   per-call raw view. An explicit context toggle may reveal context separately.
2. **Hide environment context by default.** Treat `<environment_context>` blocks
   (including working directory, shell, date, timezone, and workspace details) as
   injected context, including mixed prompt/context messages and partial streamed
   blocks. Preserve the actual user prompt and raw payloads.
3. **Support Claude Code and Codex together.** Provide explicit provider selection,
   correct upstream defaults and launch instructions for both, and readable live
   prompts, replies, tool calls/results, and usage for Anthropic Messages and
   OpenAI Responses. Keep forwarding unchanged and use only the standard library.
4. **Remove protocol tags from the readable conversation.** Hide known agent
   wrapper delimiters such as `<environment_context>`, `<system-reminder>`, and
   context wrappers; retain meaningful enclosed user text. Do not strip arbitrary
   HTML/XML or source code from ordinary messages, code blocks, or tool output.
   Raw views retain all original tags. Role labels and tool names remain useful.

### Update verification

- Mixed user text and injected context retains the user text and hides context.
- Hidden context is absent from the default display, preview, and search; explicit
  context reveal works independently of ordinary messages.
- Environment context and known protocol wrappers are covered by regression tests.
- Both providers have tested request parsing, streamed replies, tool pairing,
  upstream URL routing, and byte-preserving forwarding.
- The first worktree's checks have a 240-second limit; the second worktree's
  checks have no overall time limit. Four agents implement the missions in parallel.

These requirements supersede the original Anthropic-only non-goal and default
visibility of injected context. “Tags” currently means protocol markup wrappers;
if the requester clarifies a different meaning, update mission 4 accordingly.

## Next five improvements

Every implementation commit for this update must include its corresponding
SPEC.md acceptance/status update. Runtime remains Python standard library only;
browser testing dependencies are development-only.

1. **Dashboard provider selector.** Choose All, Codex, or Claude in the dashboard.
   Filter sessions to that provider and show its launch command. Recognize the
   protocol of each API request and route it to the matching provider upstream,
   so switching agents does not require restarting the proxy with terminal flags.
   Keep per-call provider identity and existing sessions intact; custom upstream
   overrides continue to take precedence.
2. **Reliable Codex history.** Deduplicate repeated Responses items across tool
   cycles, resumed sessions, and context compaction. Remember streamed replies as
   individual normalized messages, preserve intentionally repeated user prompts,
   and mark history rewrites instead of silently duplicating prior messages.
3. **Actionable connection errors.** Distinguish a disconnected dashboard, missing
   or failed proxy connection, upstream connectivity failure, authentication
   failure (401/403), and rate limits (429). Show recovery instructions, preserve
   raw details, and restore the live indicator after reconnecting.
4. **Single startup command.** Start the proxy, wait for readiness, open its
   dashboard, and launch the selected installed CLI using existing login. Support
   Codex and Claude, custom ports, headless use, clean shutdown, and useful errors
   for missing CLIs or occupied ports. Never terminate an unrelated process.
5. **Real browser regression tests.** Exercise streaming, hidden context, search,
   reconnect/replay, provider selection, and connection errors in a real browser.
   Run entirely against local fixtures without paid API calls or credentials.

### Progress and verification

- Specification recorded before implementation; all five improvements pending.
- Keep main-worktree validation within 240 seconds per check run; run the second
  worktree checks without an overall timeout. Record actual verification results.
