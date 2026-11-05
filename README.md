# Agent live traffic dashboard

Supports **Codex** (OpenAI Responses) and **Claude Code** (Anthropic Messages).
Python 3 standard library only; no packages or build step.

## Codex

```bash
python3 proxy.py --provider codex
# In another terminal, using your existing Codex login:
./codex-proxy
```

Open http://localhost:8042. The launcher uses per-command provider overrides,
retains your existing Codex authentication, and selects HTTP streaming so the
proxy can display Responses API text, reasoning summaries, tool calls/results,
and token usage. It does not change global Codex settings. Start a new session
through the launcher to capture its traffic; existing sessions are unaffected.
Encrypted reasoning stays private. Other response item types remain in raw data.

## Claude Code

```bash
python3 proxy.py --provider claude
# In another terminal, using your existing Claude Code login:
./claude-proxy
```

The Claude launcher sets `ANTHROPIC_BASE_URL` for that process only. The dashboard
renders Anthropic prompts, streamed replies, thinking, tool calls/results and usage.

## Run both at once

Use separate ports for each upstream. For example, keep Codex on 8041/8042 and
run Claude Code from the other worktree on 8051/8052:

```bash
python3 proxy.py --provider claude --port 8051 --ui-port 8052
./claude-proxy --proxy-port 8051
```

The parsers recognize both protocols; each proxy process forwards to its selected
upstream. `--upstream URL` overrides the selected provider's upstream.
Defaults: Codex uses https://chatgpt.com/backend-api/codex (ChatGPT sign-in),
and Claude uses https://api.anthropic.com.
For OpenAI API-key authentication, use `--upstream https://api.openai.com/v1`
and configure a matching Codex provider with `env_key="OPENAI_API_KEY"`.

Injected conversation/environment context is hidden by default in the readable
thread and search. Enable the context filter to inspect it, or use a call's raw
view for the original request/response. Known protocol tags are presentation-only;
forwarded request and response bytes are preserved.

## Checks

```bash
python3 -m unittest discover -s tests
node --test tests/*.js
```

The main worktree's checks can be bounded with `timeout -k 5s 240s`; the second
worktree can run the same commands without an overall timeout.
