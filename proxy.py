#!/usr/bin/env python3
"""proxy.py: the agent-traffic proxy for Lab 4, with a live dashboard.

A reverse proxy that prints every request and response and forwards them
unchanged. Standard library only; nothing to install.

    python3 proxy.py                       # listens on http://localhost:8041
    ./codex-proxy

Everything Codex or Claude Code sends to the API passes through this program and is
printed to the terminal. The proxy changes nothing: headers go through untouched
(a subscription login needs its bearer token and its `anthropic-beta` header),
and the reply is streamed back byte for byte as it arrives.

While it runs, open http://localhost:8042 to watch the conversation live
(see SPEC.md). The dashboard is a side effect: it never delays, alters or
breaks forwarding, and any dashboard error is only logged.

    python3 proxy.py --port 9000 --ui-port 9001
    python3 proxy.py --provider claude     # then ./claude-proxy
    python3 proxy.py --provider codex      # then ./codex-proxy
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import http.client
import json
import queue
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# Headers that describe one hop of the connection, not the message. They are
# never forwarded. `accept-encoding` is dropped too, so the API answers in plain
# text and what we print is readable.
SKIP = {
    "host", "connection", "keep-alive", "transfer-encoding", "te", "trailers",
    "upgrade", "proxy-authorization", "proxy-authenticate", "accept-encoding",
    "content-length",
}

PROVIDER_UPSTREAMS = {
    "codex": "https://chatgpt.com/backend-api/codex",
    "claude": "https://api.anthropic.com",
}
UPSTREAM = PROVIDER_UPSTREAMS["codex"]
DEFAULT_PROVIDER = "codex"
CUSTOM_UPSTREAM = False
PROXY_PORT = 8041
UI_PORT = 8042


def request_provider(path: str) -> str:
    bare = urlsplit(path).path.rstrip("/")
    if bare.startswith("/v1/messages") or bare == "/api/hello":
        return "claude"
    if bare.endswith("/responses") or "/responses/" in bare or bare.endswith("/models"):
        return "codex"
    return DEFAULT_PROVIDER


def request_upstream(path: str) -> str:
    # Explicit overrides also support local fixture servers and organization gateways.
    if CUSTOM_UPSTREAM or UPSTREAM not in PROVIDER_UPSTREAMS.values():
        return UPSTREAM
    return PROVIDER_UPSTREAMS[request_provider(path)]


def stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


# =============================================================================
# Dashboard: event bus, per-call SSE tap, session threading, UI server
# =============================================================================

MAX_EVENTS = 2000          # ring buffer of dashboard events
RAW_CALLS = 50             # raw request/response bodies kept for the last N calls
CLIP = 40_000              # longest string sent in a live event (full text stays in /raw)
RAW_CAP = 20 * 1024 * 1024  # never keep more than this much of one response body
SIDE_SYSTEM_MAX = 3000     # "short" system prompt for side-call classification
REDACT = ("authorization", "x-api-key")


def dash_log(msg: str) -> None:
    try:
        print(f"[{stamp()}] [dashboard] {msg}", file=sys.stderr, flush=True)
    except Exception:
        pass


def redact_headers(items) -> list:
    out = []
    for k, v in items:
        if k.lower() in REDACT:
            v = "[REDACTED]"
        out.append([k, v])
    return out


def shrink(x):
    """Copy of x with huge strings clipped and base64 payloads replaced."""
    if isinstance(x, str):
        if len(x) <= CLIP:
            return x
        return x[:CLIP] + f"\n[… {len(x) - CLIP:,} more characters, see raw]"
    if isinstance(x, list):
        return [shrink(v) for v in x]
    if isinstance(x, dict):
        if x.get("type") == "base64" and isinstance(x.get("data"), str):
            d = {k: v for k, v in x.items() if k != "data"}
            d["data"] = f"[{len(x['data']):,} bytes of base64]"
            return d
        return {k: shrink(v) for k, v in x.items()}
    return x


def system_text(system) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(b.get("text", "") for b in system if isinstance(b, dict))
    return ""


def system_summary(text: str) -> dict:
    first = ""
    for line in text.splitlines():
        s = line.strip()
        if s and not s.lower().startswith("x-anthropic-billing-header"):
            first = s
            break
    return {"length": len(text), "first_line": first[:200]}


def fingerprint(msg) -> str:
    """Stable identity of a message across calls.

    Ignores what the agent changes from call to call without changing meaning:
    cache_control markers, thinking blocks, string-vs-list content, whitespace.
    """
    if not isinstance(msg, dict):
        return hashlib.sha1(repr(msg).encode("utf-8", "replace")).hexdigest()
    content = msg.get("content")
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    parts = []
    for b in content or []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            s = (b.get("text") or "").strip()
            if s:
                parts.append(["text", s])
        elif t in ("thinking", "redacted_thinking"):
            continue
        elif "id" in b:
            parts.append([t, b["id"]])
        elif "tool_use_id" in b:
            parts.append([t, b["tool_use_id"]])
        else:
            parts.append([t])
    raw = json.dumps([msg.get("role"), parts], ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()


def common_prefix(a: list, b: list) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def history_context(message) -> bool:
    if not isinstance(message, dict):
        return False
    if message.get("role") in ("system", "developer"):
        return True
    content = message.get("content")
    if isinstance(content, list):
        content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    text = content.strip() if isinstance(content, str) else ""
    # A mixed context/user message is not discardable context: its ordinary
    # text may be a new user instruction even if later history overlaps.
    return bool(re.fullmatch(
        r"(?:<(environment_context|system-reminder|context_summary|conversation_summary)>"
        r"[\s\S]*?</\1>\s*)+", text,
    ))


def history_anchor(message) -> bool:
    if not isinstance(message, dict):
        return False
    if message.get("role") == "assistant":
        return True
    content = message.get("content")
    return isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


class Track:
    """One conversation inside a session (the main agent, or a subagent)."""

    def __init__(self, index: int, sys_hash: str):
        self.index = index
        self.label = "main" if index == 1 else f"thread {index}"
        self.sys_hash = sys_hash
        self.hashes: list = []
        self.reply_pos = None  # start of the output appended from a stream
        self.generation = 0
        self.response_ids: set = set()


class Sub:
    def __init__(self):
        self.q: queue.Queue = queue.Queue(maxsize=20000)
        self.dead = False


class Dashboard:
    def __init__(self):
        self.lock = threading.Lock()
        self.boot = uuid.uuid4().hex[:12]
        self.events: deque = deque(maxlen=MAX_EVENTS)  # (id, call_id, type, json)
        self.next_id = 1
        self.subs: set = set()
        self.raw: OrderedDict = OrderedDict()
        self.sessions: dict = {}
        self.last_session = None
        self.call_seq = 0

    # -- bus ------------------------------------------------------------------
    def publish(self, ev: dict) -> None:
        data = json.dumps(ev, ensure_ascii=False)
        with self.lock:
            eid = self.next_id
            self.next_id += 1
            self.events.append((eid, ev.get("call_id"), ev["type"], data))
            for sub in list(self.subs):
                try:
                    sub.q.put_nowait((eid, data))
                except queue.Full:  # a stalled browser: drop it, it will reconnect
                    sub.dead = True
                    self.subs.discard(sub)

    def compact(self, call_id: str) -> None:
        """Once a call has ended, drop its deltas from the replay buffer.

        block_stop events carry the final content, so a reload still shows
        everything while the buffer holds many more calls.
        """
        with self.lock:
            kept = [e for e in self.events if not (e[1] == call_id and e[2] == "block_delta")]
            self.events = deque(kept, maxlen=MAX_EVENTS)

    def subscribe(self, last_id: int):
        sub = Sub()
        with self.lock:
            if last_id >= self.next_id:  # id from a previous run of the proxy
                last_id = 0
            replay = [(e[0], e[3]) for e in self.events if e[0] > last_id]
            self.subs.add(sub)
        return replay, sub

    def unsubscribe(self, sub: Sub) -> None:
        with self.lock:
            self.subs.discard(sub)

    # -- calls, raw store ------------------------------------------------------
    def new_call_id(self) -> str:
        with self.lock:
            self.call_seq += 1
            return f"c{self.call_seq}"

    def store_raw(self, call_id: str, record: dict) -> None:
        with self.lock:
            self.raw[call_id] = record
            while len(self.raw) > RAW_CALLS:
                self.raw.popitem(last=False)

    def get_raw(self, call_id: str):
        with self.lock:
            return self.raw.get(call_id)

    # -- sessions ---------------------------------------------------------------
    def resolve_session(self, header_sid, req) -> str:
        sid = header_sid
        if not sid and isinstance(req, dict):
            uid = str((req.get("metadata") or {}).get("user_id") or "")
            m = re.search(r"session_?([0-9a-fA-F-]{36})", uid)
            if m:
                sid = m.group(1)
        with self.lock:
            if sid:
                self.last_session = sid
                return sid
            return self.last_session or "(no session)"

    def assign(self, sid: str, messages: list, sys_hash: str, *, responses=False,
               previous_response_id=None):
        """Match ordered history, never a global set of already-seen text.

        Responses output items are stored separately, matching the separate input
        items Codex echoes on its next request. A compaction may retain only a
        suffix; recognize it only with an assistant/tool anchor, so a repeated
        user prompt alone always remains a new prompt after a rewritten history.
        """
        fps = [fingerprint(m) for m in messages]
        with self.lock:
            tracks = self.sessions.setdefault(sid, [])
            info = {"new": False, "rewritten": 0}
            parent = next((t for t in tracks if previous_response_id in t.response_ids), None) if previous_response_id else None
            if parent is not None:
                # The Responses API can send just the new input when referring
                # to a prior response. It is an append, even if its text repeats.
                parent.hashes.extend(fps)
                parent.reply_pos = None
                parent.generation += 1
                info.update(index=parent.index, label=parent.label, total=len(tracks))
                return parent, 0, info

            best, best_k = None, 0
            for t in tracks:
                k = common_prefix(t.hashes, fps)
                if k > best_k or (k == best_k and k > 0 and t.sys_hash == sys_hash):
                    best, best_k = t, k

            # Find a suffix retained across compaction or a resumed short history.
            # Only context may precede the retained suffix: do not discard a new
            # user prompt just because later content resembles old history.
            overlap_track, overlap_start, overlap_size = None, 0, 0
            if responses:
                for t in tracks:
                    for incoming_start in range(len(fps)):
                        if incoming_start and not history_context(messages[incoming_start - 1]):
                            break
                        max_size = min(len(t.hashes), len(fps) - incoming_start)
                        for size in range(max_size, overlap_size, -1):
                            window = messages[incoming_start:incoming_start + size]
                            anchored = any(history_anchor(m) for m in window)
                            if anchored and t.hashes[-size:] == fps[incoming_start:incoming_start + size]:
                                overlap_track, overlap_start, overlap_size = t, incoming_start, size
                                break
                if overlap_track is not None and overlap_start + overlap_size > best_k:
                    best = overlap_track
                    start = overlap_start + overlap_size
                    info["rewritten"] = len(best.hashes) - overlap_size
                else:
                    start = best_k
            else:
                start = best_k

            if best is None:
                # A changed Codex context still belongs to its existing thread
                # when that thread is unambiguous; report the rewrite explicitly.
                compatible = [t for t in tracks if t.sys_hash == sys_hash]
                if responses and not compatible and len(tracks) == 1:
                    compatible = tracks
                if responses and len(compatible) == 1:
                    best = compatible[0]
                    info["rewritten"] = len(best.hashes)
                    start = 0
                else:
                    best = Track(len(tracks) + 1, sys_hash)
                    tracks.append(best)
                    start = 0
                    info["new"] = True
            elif overlap_track is not best or start == best_k:
                if best_k < len(best.hashes):
                    reply_mismatch = (
                        not responses
                        and best.reply_pos == best_k
                        and best_k == len(best.hashes) - 1
                        and best_k < len(messages)
                        and isinstance(messages[best_k], dict)
                        and messages[best_k].get("role") == "assistant"
                    )
                    if reply_mismatch:  # Anthropic may regroup its assistant blocks
                        start = best_k + 1
                    else:
                        info["rewritten"] = len(best.hashes) - best_k
            best.hashes = fps
            best.reply_pos = None
            best.generation += 1
            info.update(index=best.index, label=best.label, total=len(tracks))
            return best, start, info

    def add_replies(self, track: Track, reply_fps: list, response_id=None, generation=None) -> None:
        with self.lock:
            # A slower concurrent response must not append to a newer request's
            # history snapshot. Its output remains available in its own raw call.
            if generation is not None and generation != track.generation:
                return
            if response_id:
                track.response_ids.add(response_id)
            if reply_fps:
                track.reply_pos = len(track.hashes)
                track.hashes.extend(reply_fps)

    def add_reply(self, track: Track, reply_fp: str) -> None:
        self.add_replies(track, [reply_fp])


DASH = Dashboard()


def guarded(fn):
    """Dashboard work must never break forwarding: catch, log, go quiet."""

    def wrapper(self, *a, **kw):
        if self.broken:
            return None
        try:
            return fn(self, *a, **kw)
        except Exception as e:  # noqa: BLE001
            self.broken = True
            dash_log(f"error in {fn.__name__} for {getattr(self, 'call_id', '?')}: {e!r}")
            return None

    return wrapper


def responses_messages(items):
    """Normalize Responses input for the existing readable thread renderer."""
    if isinstance(items, str):
        return [{"role": "user", "content": items}]
    messages = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        typ = item.get("type", "message")
        if typ == "message":
            content = item.get("content", [])
            if isinstance(content, list):
                content = [{"type": "text", "text": part.get("text", "")} if part.get("type") in ("input_text", "output_text") else part for part in content if isinstance(part, dict)]
            messages.append({"role": item.get("role", "user"), "content": content})
        elif typ in ("function_call", "custom_tool_call"):
            raw = item.get("arguments", item.get("input", ""))
            try:
                args = json.loads(raw)
            except (ValueError, TypeError):
                args = raw
            messages.append({"role": "assistant", "content": [{"type": "tool_use", "id": item.get("call_id"), "name": item.get("name"), "input": args}]})
        elif typ in ("function_call_output", "custom_tool_call_output"):
            output = item.get("output", "")
            messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": item.get("call_id"), "content": output if isinstance(output, str) else json.dumps(output)}]})
    return messages


class CallTap:
    """Watches one proxied call and turns it into dashboard events."""

    def __init__(self, method: str, path: str, headers, body: bytes):
        self.broken = False
        self.started = False
        self.finished = False
        self.call_id = "?"
        try:
            self.call_id = DASH.new_call_id()
            self.method, self.path, self.body = method, path, body
            self.req_headers = redact_headers(headers.items())
            self.header_sid = headers.get("session_id") or headers.get("x-codex-session-id") or headers.get("X-Claude-Code-Session-Id")
            self.responses_api = False
            self.t0 = time.time()
            self.kind = "side"
            self.track = None
            self.track_generation = None
            self.response_id = None
            self.status = None
            self.reason = ""
            self.resp_headers: list = []
            self.resp_body = bytearray()
            self.is_sse = False
            self.dec = codecs.getincrementaldecoder("utf-8")("replace")
            self.buf = ""
            self.blocks: dict = {}
            self.usage: dict = {}
            self.stop_reason = None
            self.error = None
            self.model = None
            self.record = None
        except Exception as e:  # noqa: BLE001
            self.broken = True
            dash_log(f"cannot tap call: {e!r}")

    # -- request -------------------------------------------------------------
    @guarded
    def start(self) -> None:
        if self.started:
            return
        self.started = True
        req = None
        if self.body:
            try:
                req = json.loads(self.body)
            except ValueError:
                req = None
        if not isinstance(req, dict):
            req = None
        bare = self.path.split("?", 1)[0]
        self.responses_api = bare.rstrip("/").endswith("/responses")
        if self.responses_api and req:
            req = dict(req)
            req["messages"] = responses_messages(req.get("input", []))
            req["system"] = req.get("instructions", "")
        sys_txt = system_text(req.get("system")) if req else ""
        tools = req.get("tools") if req and isinstance(req.get("tools"), list) else []

        if self.responses_api and self.method == "POST" and req:
            kind, why = "main", "Codex Responses API"
        elif self.method != "POST" or bare != "/v1/messages":
            kind, why = "side", f"{self.method} {bare}"
        elif req is None:
            kind, why = "side", "unparsed body"
        elif (req.get("max_tokens") or 0) <= 1:
            kind, why = "side", "quota probe (max_tokens ≤ 1)"
        elif not tools and len(sys_txt) < SIDE_SYSTEM_MAX:
            kind, why = "side", "no tools, short system (title / utility)"
        else:
            kind, why = "main", f"{len(tools)} tools"
        self.kind = kind
        self.model = req.get("model") if req else None
        sid = DASH.resolve_session(self.header_sid, req)

        self.record = {
            "call_id": self.call_id,
            "request": {
                "method": self.method, "path": self.path, "headers": self.req_headers,
                "body": self.body.decode("utf-8", "replace"),
            },
            "response": {"status": None, "reason": "", "headers": [], "body": ""},
        }
        DASH.store_raw(self.call_id, self.record)

        new_messages, track_info = [], None
        if kind == "main":
            messages = req.get("messages") if isinstance(req.get("messages"), list) else []
            sys_hash = hashlib.sha1(sys_txt.encode("utf-8", "replace")).hexdigest()
            self.track, start, track_info = DASH.assign(
                f"{request_provider(self.path)}:{sid}", messages, sys_hash, responses=self.responses_api,
                previous_response_id=req.get("previous_response_id") if self.responses_api else None,
            )
            self.track_generation = self.track.generation
            new_messages = shrink(messages[start:])

        DASH.publish({
            "type": "call_start",
            "call_id": self.call_id,
            "session_id": sid,
            "provider": request_provider(self.path),
            "time": int(self.t0 * 1000),
            "method": self.method,
            "path": self.path,
            "model": self.model,
            "kind": kind,
            "side_reason": why,
            "max_tokens": req.get("max_tokens") if req else None,
            "new_messages": new_messages,
            "track": track_info,
            "system_summary": system_summary(sys_txt),
            "tools": [t.get("name") for t in tools if isinstance(t, dict)],
            "request_raw_ref": f"/raw/{self.call_id}",
        })

    # -- response --------------------------------------------------------------
    @guarded
    def response(self, status: int, reason: str, headers) -> None:
        self.status, self.reason = status, reason
        self.resp_headers = [[k, v] for k, v in headers]
        ctype = next((v for k, v in headers if k.lower() == "content-type"), "")
        self.is_sse = "text/event-stream" in ctype.lower()
        if self.record:
            self.record["response"].update(status=status, reason=reason, headers=self.resp_headers)

    @guarded
    def feed(self, chunk: bytes) -> None:
        if len(self.resp_body) < RAW_CAP:
            self.resp_body += chunk[: RAW_CAP - len(self.resp_body)]
        if not self.is_sse:
            return
        self.buf += self.dec.decode(chunk).replace("\r", "")
        while "\n\n" in self.buf:
            block, self.buf = self.buf.split("\n\n", 1)
            self.sse_event(block)

    def sse_event(self, block: str) -> None:
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if not data:
            return
        try:
            obj = json.loads("\n".join(data))
        except ValueError:
            return
        if not isinstance(obj, dict):
            return
        typ = obj.get("type") or name
        if isinstance(typ, str) and typ.startswith("response."):
            self.responses_event(typ, obj)
        elif typ == "message_start":
            msg = obj.get("message") or {}
            self.model = msg.get("model") or self.model
            self.usage.update(msg.get("usage") or {})
        elif typ == "content_block_start":
            self.block_start(obj.get("index", 0), obj.get("content_block") or {})
        elif typ == "content_block_delta":
            self.block_delta(obj.get("index", 0), obj.get("delta") or {})
        elif typ == "content_block_stop":
            self.block_stop(obj.get("index", 0))
        elif typ == "message_delta":
            d = obj.get("delta") or {}
            if d.get("stop_reason"):
                self.stop_reason = d["stop_reason"]
            for k, v in (obj.get("usage") or {}).items():
                if v is not None:
                    self.usage[k] = v
        elif typ == "error":
            err = obj.get("error") or {}
            self.error = f"{err.get('type', 'error')}: {err.get('message', '')}".strip()

    def responses_event(self, typ: str, obj: dict) -> None:
        response = obj.get("response") or {}
        if response.get("id"):
            self.response_id = response["id"]
        # Give every output item/content part a stable dashboard block index.
        index = obj.get("output_index", 0) * 10000 + obj.get("content_index", obj.get("summary_index", 0))
        if typ == "response.output_item.added":
            item = obj.get("item") or {}
            if item.get("type") in ("function_call", "custom_tool_call"):
                self.block_start(index, {"type": "tool_use", "name": item.get("name"), "id": item.get("call_id")})
        elif typ in ("response.output_text.delta", "response.reasoning_summary_text.delta"):
            thinking = "reasoning" in typ
            if index not in self.blocks:
                self.block_start(index, {"type": "thinking" if thinking else "text"})
            self.block_delta(index, {"type": "thinking_delta" if thinking else "text_delta",
                                     "thinking" if thinking else "text": obj.get("delta", "")})
        elif typ == "response.function_call_arguments.delta":
            self.block_delta(index, {"type": "input_json_delta", "partial_json": obj.get("delta", "")})
        elif typ == "response.custom_tool_call_input.delta":
            self.block_delta(index, {"type": "input_json_delta", "partial_json": obj.get("delta", "")})
        elif typ == "response.output_item.done":
            item = obj.get("item") or {}
            if item.get("type") in ("function_call", "custom_tool_call"):
                if index not in self.blocks:
                    self.block_start(index, {"type": "tool_use", "name": item.get("name"), "id": item.get("call_id")})
                if not self.blocks[index]["json"]:
                    self.block_delta(index, {"type": "input_json_delta", "partial_json": item.get("arguments", item.get("input", ""))})
            elif item.get("type") in ("message", "reasoning"):
                thinking = item["type"] == "reasoning"
                for part_index, part in enumerate(item.get("summary" if thinking else "content") or []):
                    key = index + part_index
                    if part.get("type") not in ("output_text", "summary_text") or key in self.blocks:
                        continue
                    self.block_start(key, {"type": "thinking" if thinking else "text"})
                    self.block_delta(key, {"type": "thinking_delta" if thinking else "text_delta",
                                           "thinking" if thinking else "text": part.get("text", "")})
            for key in sorted(self.blocks):
                if key // 10000 == index // 10000:
                    self.block_stop(key)
        elif typ in ("response.completed", "response.failed", "response.incomplete"):
            response = obj.get("response") or {}
            self.model = response.get("model") or self.model
            self.stop_reason = response.get("status")
            usage = response.get("usage") or {}
            self.usage.update(usage)
            self.usage["cache_read_input_tokens"] = (usage.get("input_tokens_details") or {}).get("cached_tokens")
            if response.get("error"):
                self.error = json.dumps(response["error"])

    def block_start(self, index: int, cb: dict) -> None:
        t = cb.get("type", "text")
        b = {"type": t, "text": [], "json": [], "name": cb.get("name"), "id": cb.get("id"), "extra": None}
        self.blocks[index] = b
        if t not in ("text", "tool_use", "thinking", "server_tool_use", "redacted_thinking"):
            b["extra"] = shrink(cb)  # e.g. web_search_tool_result: arrives whole
        DASH.publish({
            "type": "block_start", "call_id": self.call_id, "index": index, "block_type": t,
            "tool_name": cb.get("name"), "tool_use_id": cb.get("id"),
        })
        if t == "text" and cb.get("text"):
            self.block_delta(index, {"type": "text_delta", "text": cb["text"]})
        if t == "thinking" and cb.get("thinking"):
            self.block_delta(index, {"type": "thinking_delta", "thinking": cb["thinking"]})

    def block_delta(self, index: int, d: dict) -> None:
        b = self.blocks.get(index)
        if b is None:
            return
        dt = d.get("type")
        ev = {"type": "block_delta", "call_id": self.call_id, "index": index}
        if dt == "text_delta":
            b["text"].append(d.get("text", ""))
            ev["text"] = d.get("text", "")
        elif dt == "thinking_delta":
            b["text"].append(d.get("thinking", ""))
            ev["text"] = d.get("thinking", "")
        elif dt == "input_json_delta":
            b["json"].append(d.get("partial_json", ""))
            ev["partial_json"] = d.get("partial_json", "")
        else:
            return  # signature_delta and friends: nothing to show
        DASH.publish(ev)

    def block_stop(self, index: int) -> None:
        b = self.blocks.get(index)
        if b is None:
            return
        ev = {"type": "block_stop", "call_id": self.call_id, "index": index}
        text = "".join(b["text"])
        if b["type"] in ("tool_use", "server_tool_use"):
            raw = "".join(b["json"])
            try:
                ev["input"] = shrink(json.loads(raw)) if raw.strip() else {}
            except ValueError:
                ev["input"] = shrink(raw)
        elif b["type"] in ("thinking", "redacted_thinking"):
            ev["text"] = shrink(text)
            ev["redacted"] = not text.strip()
        elif b["extra"] is not None:
            ev["extra"] = b["extra"]
        else:
            ev["text"] = shrink(text)
        DASH.publish(ev)

    def reply_fingerprint(self) -> str | None:
        content = []
        for i in sorted(self.blocks):
            b = self.blocks[i]
            if b["type"] == "text":
                content.append({"type": "text", "text": "".join(b["text"])})
            elif b.get("id"):
                content.append({"type": b["type"], "id": b["id"]})
        if not content:
            return None
        return fingerprint({"role": "assistant", "content": content})

    def reply_fingerprints(self) -> list:
        if not self.responses_api:
            fp = self.reply_fingerprint()
            return [fp] if fp else []
        items = {}
        for index in sorted(self.blocks):
            block = self.blocks[index]
            group = items.setdefault(index // 10000, [])
            if block["type"] == "text":
                group.append({"type": "text", "text": "".join(block["text"])})
            elif block["type"] in ("tool_use", "server_tool_use") and block.get("id"):
                group.append({"type": "tool_use", "id": block["id"]})
        return [fingerprint({"role": "assistant", "content": content})
                for content in items.values() if content]

    # -- end ---------------------------------------------------------------------
    @guarded
    def finish(self, error: str | None = None) -> None:
        if self.finished:
            return
        if not self.started:
            self.start()
        self.finished = True
        if self.is_sse and self.buf.strip():
            self.sse_event(self.buf)
            self.buf = ""
        body_text = bytes(self.resp_body).decode("utf-8", "replace")
        if self.record:
            self.record["response"]["body"] = body_text
        if not self.is_sse and body_text.strip():
            self.plain_body(body_text)
        if error and not self.error:
            self.error = error
        if self.status and self.status >= 400 and not self.error:
            self.error = f"HTTP {self.status} {self.reason}"
        if self.kind == "main" and self.track and self.status == 200 and not self.error:
            DASH.add_replies(self.track, self.reply_fingerprints(),
                             response_id=self.response_id, generation=self.track_generation)
        u = self.usage
        DASH.publish({
            "type": "call_end",
            "call_id": self.call_id,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "model": self.model,
            "usage": {
                "input": u.get("input_tokens"),
                "output": u.get("output_tokens"),
                "cache_read": u.get("cache_read_input_tokens"),
                "cache_write": u.get("cache_creation_input_tokens"),
            },
            "duration_ms": int((time.time() - self.t0) * 1000),
            "error": self.error,
        })
        DASH.compact(self.call_id)

    def plain_body(self, text: str) -> None:
        try:
            obj = json.loads(text)
        except ValueError:
            if self.status and self.status >= 400:
                self.error = text.strip()[:500]
            return
        if not isinstance(obj, dict):
            return
        if obj.get("type") == "error" or obj.get("error"):
            err = obj.get("error")
            if isinstance(err, dict):
                self.error = f"{err.get('type', 'error')}: {err.get('message', '')}"
            else:
                self.error = str(err)[:500]
        elif obj.get("object") == "response" or (self.responses_api and "output" in obj):
            for i, item in enumerate(obj.get("output") or []):
                self.responses_event("response.output_item.done", {"output_index": i, "item": item})
            self.responses_event("response.completed", {"response": obj})
        elif obj.get("type") == "message":  # non-streaming reply
            self.model = obj.get("model") or self.model
            self.stop_reason = obj.get("stop_reason")
            self.usage.update(obj.get("usage") or {})
            for i, cb in enumerate(obj.get("content") or []):
                t = cb.get("type")
                start = {k: v for k, v in cb.items() if k not in ("text", "thinking", "input")}
                self.block_start(i, start)
                if t == "text":
                    self.block_delta(i, {"type": "text_delta", "text": cb.get("text", "")})
                elif t == "thinking":
                    self.block_delta(i, {"type": "thinking_delta", "thinking": cb.get("thinking", "")})
                elif t in ("tool_use", "server_tool_use"):
                    self.block_delta(i, {"type": "input_json_delta", "partial_json": json.dumps(cb.get("input", {}))})
                self.block_stop(i)


class UI(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass

    def send_bytes(self, code: int, ctype: str, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        try:
            parts = urlsplit(self.path)
            if parts.path in ("/", "/index.html"):
                self.send_bytes(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
            elif parts.path == "/health":
                health = {"status": "ok", "provider": DEFAULT_PROVIDER,
                          "proxy_port": PROXY_PORT, "ui_port": UI_PORT,
                          "upstream": UPSTREAM, "auto_route": not CUSTOM_UPSTREAM}
                self.send_bytes(200, "application/json", json.dumps(health).encode())
            elif parts.path == "/events":
                self.events(parts.query)
            elif parts.path.startswith("/raw/"):
                rec = DASH.get_raw(parts.path[5:])
                if rec is None:
                    self.send_bytes(404, "application/json", b'{"error":"not kept"}')
                else:
                    with DASH.lock:
                        data = json.dumps(rec, ensure_ascii=False)
                    self.send_bytes(200, "application/json; charset=utf-8", data.encode("utf-8"))
            else:
                self.send_bytes(404, "text/plain", b"not found")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001
            dash_log(f"UI error on {self.path}: {e!r}")

    def events(self, query: str) -> None:
        last = self.headers.get("Last-Event-ID") or (parse_qs(query).get("last") or ["0"])[0]
        try:
            last_id = int(last)
        except ValueError:
            last_id = 0
        replay, sub = DASH.subscribe(last_id)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            hello = json.dumps({"type": "hello", "boot": DASH.boot, "replay": len(replay)})
            out = [f"retry: 2000\ndata: {hello}\n\n"]
            out += [f"id: {eid}\ndata: {data}\n\n" for eid, data in replay]
            out.append('data: {"type":"replay_done"}\n\n')
            self.wfile.write("".join(out).encode("utf-8"))
            self.wfile.flush()
            while not sub.dead:
                try:
                    first = sub.q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                batch = [first]
                while len(batch) < 500:
                    try:
                        batch.append(sub.q.get_nowait())
                    except queue.Empty:
                        break
                self.wfile.write("".join(f"id: {e}\ndata: {d}\n\n" for e, d in batch).encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            DASH.unsubscribe(sub)


# =============================================================================
# The proxy
# =============================================================================


class Tap(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # we print our own lines

    def do_POST(self) -> None:
        self.relay()

    def do_GET(self) -> None:
        self.relay()

    def do_HEAD(self) -> None:
        self.relay()

    def relay(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""

        # 1. Print what the agent sent.
        print(f"\n{'=' * 78}\n[{stamp()}] >>> {self.command} {self.path}", flush=True)
        for name, value in self.headers.items():
            if name.lower() in ("authorization", "x-api-key"):
                value = "[REDACTED]"
            print(f"    {name}: {value}")
        if body:
            try:
                print(json.dumps(json.loads(body), indent=2, ensure_ascii=False))
            except ValueError:
                print(body.decode("utf-8", "replace"))

        tap = CallTap(self.command, self.path, self.headers, body)  # dashboard side effect

        # 2. Forward it, headers intact.
        selected_upstream = request_upstream(self.path)
        target = urlsplit(selected_upstream)
        conn_cls = http.client.HTTPSConnection if target.scheme == "https" else http.client.HTTPConnection
        # Connect with a short timeout (a lost SYN must not stall the agent for
        # minutes); the long timeout is for reading a slow stream.
        upstream = conn_cls(target.netloc, timeout=10)
        try:
            upstream.connect()
        except OSError as e:
            print(f"\n[{stamp()}] !!! cannot reach {selected_upstream}: {e}", flush=True)
            tap.finish(error=f"proxy: cannot reach {selected_upstream}: {e}")
            self.send_error(502, f"proxy: cannot reach {selected_upstream}: {e}")
            self.close_connection = True
            return
        response_started = False
        try:
            upstream.sock.settimeout(600)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in SKIP}
            upstream.request(self.command, target.path.rstrip("/") + self.path, body=body or None, headers=headers)
            tap.start()  # parse + publish while the upstream thinks
            reply = upstream.getresponse()

            # 3. Stream the reply back as it arrives, printing it on the way.
            print(f"\n[{stamp()}] <<< {reply.status} {reply.reason}", flush=True)
            tap.response(reply.status, reply.reason, reply.getheaders())
            response_started = True
            self.send_response(reply.status, reply.reason)
            for name, value in reply.getheaders():
                if name.lower() not in SKIP and name.lower() != "content-encoding":
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command == "HEAD":
                self.close_connection = True
                tap.finish()
                return
            while chunk := reply.read1(4096):  # read1: pass on each chunk as it lands, don't wait for 4096 bytes
                self.wfile.write(chunk)
                self.wfile.flush()
                tap.feed(chunk)
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()
            self.close_connection = True
            print(f"\n[{stamp()}] --- done", flush=True)
            tap.finish()
        except Exception as e:
            tap.finish(error=f"proxy: cannot reach upstream: {type(e).__name__}: {e}")
            if not response_started:
                self.send_error(502, "Cannot reach upstream; check your connection and upstream URL")
            self.close_connection = True
        finally:
            upstream.close()


def serve_ui(port: int) -> None:
    try:
        ui = ThreadingHTTPServer(("127.0.0.1", port), UI)
        ui.daemon_threads = True
    except OSError as e:
        dash_log(f"cannot start dashboard on port {port}: {e} (proxy keeps running)")
        return
    t = threading.Thread(target=ui.serve_forever, name="dashboard", daemon=True)
    t.start()
    print(f"dashboard on       http://localhost:{port}")


def main() -> None:
    global UPSTREAM, DEFAULT_PROVIDER, CUSTOM_UPSTREAM, PROXY_PORT, UI_PORT
    parser = argparse.ArgumentParser(description="Print and forward an agent's API traffic.")
    parser.add_argument("--port", type=int, default=8041)
    parser.add_argument("--ui-port", type=int, default=8042, help="dashboard port (default 8042)")
    parser.add_argument("--provider", choices=tuple(PROVIDER_UPSTREAMS), default="codex",
                        help="upstream provider (default: codex)")
    parser.add_argument("--upstream", help="override the selected provider upstream URL")
    args = parser.parse_args()
    DEFAULT_PROVIDER = args.provider
    CUSTOM_UPSTREAM = args.upstream is not None
    PROXY_PORT, UI_PORT = args.port, args.ui_port
    UPSTREAM = (args.upstream or PROVIDER_UPSTREAMS[args.provider]).rstrip("/")
    target = urlsplit(UPSTREAM)
    if target.scheme not in ("http", "https") or not target.netloc or target.query or target.fragment:
        parser.error("--upstream must be an HTTP(S) base URL without a query or fragment")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Tap)
    server.daemon_threads = True
    print(f"proxy listening on http://localhost:{args.port}  ->  {UPSTREAM}")
    serve_ui(args.ui_port)
    print(f"run {args.provider} with:  ./{args.provider}-proxy --proxy-port {args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


# =============================================================================
# The page (one self-contained file: inline CSS and JS, no CDN)
# =============================================================================

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent traffic</title>
<style>
:root{
  --bg:#f5f6f8;--panel:#ffffff;--fg:#1c2026;--dim:#69707c;--faint:#9aa1ac;--line:#e1e4e9;
  --you:#1f5fd6;--you-bg:#e7efff;--you-line:#c3d5fb;
  --model:#08795a;--model-line:#5cc4a1;
  --tool:#a3530a;--tool-bg:#fff6ea;--tool-line:#f0c58f;
  --res:#0b6b82;--res-bg:#f0f7f9;--res-line:#b5dbe5;
  --err:#c62828;--err-bg:#fff0f0;
  --think:#6d3fd1;--inj:#6b7280;--code-bg:#eef0f3;--hl:#fff1a8;
}
@media (prefers-color-scheme: dark){:root{
  --bg:#0f1114;--panel:#171a20;--fg:#e4e7eb;--dim:#9aa1ac;--faint:#6b7280;--line:#2a2f38;
  --you:#86aefc;--you-bg:#1a2742;--you-line:#2c4370;
  --model:#4fd1a5;--model-line:#2a7d63;
  --tool:#f2a93b;--tool-bg:#261d10;--tool-line:#6b4a19;
  --res:#5cc8e6;--res-bg:#10202a;--res-line:#1f4555;
  --err:#ff7b7b;--err-bg:#361616;
  --think:#b79cff;--inj:#9aa1ac;--code-bg:#0b0d10;--hl:#5a4b00;
}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;display:flex;flex-direction:column}
code,pre,.mono{font-family:ui-monospace,"Cascadia Code",Consolas,Menlo,monospace;font-size:12.5px}
button{font:inherit;color:inherit}
svg.i{width:14px;height:14px;flex:none;fill:none;stroke:currentColor;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}

header{background:var(--panel);border-bottom:1px solid var(--line);padding:8px 16px;display:flex;flex-wrap:wrap;gap:8px 16px;align-items:center}
header h1{font-size:15px;margin:0;font-weight:650;white-space:nowrap}
#status{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--dim);white-space:nowrap}
#status .dot{width:9px;height:9px;border-radius:50%;background:var(--faint)}
#status.live .dot{background:#22a06b;box-shadow:0 0 0 3px rgba(34,160,107,.18)}
#status.down .dot{background:#d97706;animation:pulse 1s infinite}
select,input[type=search]{font:inherit;font-size:13px;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:4px 8px}
#session{max-width:340px}
#search{width:180px}
.filters{display:flex;gap:12px;font-size:13px;color:var(--dim)}
.filters label{display:flex;gap:4px;align-items:center;cursor:pointer;white-space:nowrap}
#totals{font-size:12px;color:var(--dim);margin-left:auto;white-space:nowrap}
#totals b{color:var(--fg);font-weight:600}
#matches{font-size:12px;color:var(--dim)}

main{flex:1;overflow-y:auto;position:relative}
#thread{max-width:1000px;margin:0 auto;padding:12px 16px 80px}
.empty{color:var(--dim);text-align:center;margin-top:18vh;line-height:1.8}
.empty code{background:var(--code-bg);padding:2px 6px;border-radius:4px}

/* setup panel */
details.setup{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:6px 12px;margin:4px 0 14px;font-size:13px;color:var(--dim)}
details.setup summary{cursor:pointer}
details.setup .tools{margin-top:6px;display:flex;flex-wrap:wrap;gap:4px}
details.setup .tools span{background:var(--code-bg);border-radius:4px;padding:1px 6px;font-size:12px}

/* entries */
.entry{margin:12px 0;display:flex;flex-direction:column;align-items:flex-start}
.lab{font-size:11.5px;font-weight:650;letter-spacing:.04em;text-transform:uppercase;display:flex;align-items:center;gap:5px;margin:0 2px 3px}
.lab .sub{text-transform:none;letter-spacing:0;font-weight:500;color:var(--dim)}
.body{max-width:100%;overflow-wrap:anywhere}

.k-user{align-items:flex-end}
.k-user .lab{color:var(--you)}
.k-user .body{background:var(--you-bg);border:1px solid var(--you-line);border-radius:14px 14px 4px 14px;padding:8px 12px;max-width:78%;white-space:pre-wrap}

.k-model .lab{color:var(--model)}
.k-model .body{border-left:3px solid var(--model-line);padding:0 0 0 12px;max-width:94%}
.md p{margin:.25em 0}.md ul,.md ol{margin:.3em 0;padding-left:1.5em}.md li{margin:.1em 0}
.md h3,.md h4,.md h5,.md h6{margin:.6em 0 .2em;font-size:14.5px}
.md code{background:var(--code-bg);padding:1px 4px;border-radius:4px}
.md pre{background:var(--code-bg);padding:8px 10px;border-radius:6px;overflow-x:auto;margin:.4em 0}
.md pre code{background:none;padding:0}
.md .gap{height:.35em}
.cursor{display:inline-block;width:7px;height:1.05em;background:var(--model);vertical-align:text-bottom;margin-left:2px;animation:blink 1s steps(2) infinite}
@keyframes blink{50%{opacity:0}}
@keyframes pulse{50%{opacity:.35}}

.k-tool{width:94%}
.k-tool .lab{color:var(--tool)}
.card{width:100%;background:var(--tool-bg);border:1px solid var(--tool-line);border-left:4px solid var(--tool);border-radius:6px;overflow:hidden}
.tool-head{display:flex;gap:10px;align-items:baseline;padding:6px 10px}
.tool-head b{font-size:13.5px;white-space:nowrap}
.tool-head .arg{color:var(--fg);opacity:.85;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1}
.tool-head .tg{font-size:11px;color:var(--dim);white-space:nowrap}
details.inp{padding:0 10px 6px;font-size:12px;color:var(--dim)}
details.inp summary{cursor:pointer;width:max-content}
details.inp pre{color:var(--fg);background:var(--panel);border:1px solid var(--line);border-radius:4px;padding:6px 8px;margin:4px 0 0;max-height:360px;overflow:auto;white-space:pre-wrap}

.result{border-top:1px solid var(--res-line);background:var(--res-bg);padding:6px 10px 8px;border-left:0}
.result .lab{color:var(--res)}
.result pre,.k-result pre{margin:0;white-space:pre-wrap;overflow-wrap:anywhere;max-height:none}
.result.err{background:var(--err-bg);border-top:2px solid var(--err);box-shadow:inset 3px 0 0 var(--err)}
.result.err .lab{color:var(--err)}
.result.pending{color:var(--dim);font-size:12px;font-style:italic}
.clamped{max-height:21.5em;overflow:hidden;-webkit-mask-image:linear-gradient(#000 70%,transparent);mask-image:linear-gradient(#000 70%,transparent)}
.more{background:none;border:0;color:var(--you);cursor:pointer;padding:2px 0;font-size:12px}
.k-result{width:94%}
.k-result .result{border:1px solid var(--res-line);border-radius:6px}

.k-think{width:94%;color:var(--think)}
.k-think details,.k-inj details{font-size:12.5px;opacity:.85}
.k-think summary,.k-inj summary{cursor:pointer;display:flex;gap:6px;align-items:center;list-style:none}
.k-think summary::-webkit-details-marker,.k-inj summary::-webkit-details-marker{display:none}
.k-think summary::before,.k-inj summary::before{content:"▸";font-size:10px}
.k-think details[open] summary::before,.k-inj details[open] summary::before{content:"▾"}
.k-think pre,.k-inj pre{white-space:pre-wrap;color:var(--dim);border-left:2px solid currentColor;padding:2px 0 2px 10px;margin:4px 0 0 4px;max-height:400px;overflow:auto;font-family:inherit;font-size:12.5px}
.k-inj{color:var(--inj);align-items:flex-end;width:100%}
.k-inj details{max-width:78%}
.k-inj summary{justify-content:flex-end}
.k-inj .peek{max-width:420px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--faint)}

.k-error .lab{color:var(--err)}
.k-error .body{background:var(--err-bg);border:1px solid var(--err);border-radius:6px;padding:6px 10px;white-space:pre-wrap}

.divider{display:flex;align-items:center;gap:10px;color:var(--dim);font-size:12px;margin:18px 0}
.divider::before,.divider::after{content:"";flex:1;border-top:1px dashed var(--line)}

/* call footer */
.footer{display:flex;flex-wrap:wrap;gap:4px 12px;font-size:11.5px;color:var(--faint);border-top:1px solid var(--line);padding:3px 2px;margin:6px 0 16px}
.footer .stream{color:var(--model);animation:pulse 1.2s infinite}
.footer .bad{color:var(--err)}
.footer .tag{background:var(--code-bg);border-radius:4px;padding:0 5px;color:var(--dim)}
.linkbtn{background:none;border:0;padding:0;color:var(--you);cursor:pointer;font-size:inherit}
.raw{width:100%;background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin:-10px 0 16px;font-size:12px}
.raw h4{margin:6px 0 4px;font-size:12px}
.raw pre{background:var(--code-bg);border-radius:4px;padding:6px 8px;max-height:420px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;margin:0 0 6px}

/* side calls */
.siderow{display:flex;gap:10px;align-items:center;font-size:12px;color:var(--dim);padding:3px 8px;margin:4px 0;border:1px dashed var(--line);border-radius:6px;cursor:pointer;white-space:nowrap;overflow:hidden}
.siderow .k{font-weight:650;color:var(--faint);text-transform:uppercase;font-size:10.5px;letter-spacing:.05em}
.siderow .x{overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1}
body.hide-side .call.side{display:none}
body.hide-thinking .k-think{display:none}
#connection-detail{padding:10px 20px;background:var(--you-bg);border-bottom:1px solid var(--line)}
#connection-detail.error{background:var(--err-bg);color:var(--err)}
#launch-help{padding:6px 20px;color:var(--dim);font-size:12px}
body.hide-injected .k-inj,body.hide-injected .setup{display:none}
.miss{display:none!important}
mark{background:var(--hl);color:inherit}

#newbtn{position:fixed;right:24px;bottom:22px;background:var(--you);color:#fff;border:0;border-radius:18px;padding:7px 14px;font-weight:600;box-shadow:0 2px 10px rgba(0,0,0,.25);cursor:pointer}
@media (prefers-color-scheme: dark){#newbtn{color:#0f1114}}
@media (max-width:900px){#totals{margin-left:0}.k-user .body,.k-inj details{max-width:90%}}
</style>
</head>
<body class="hide-side hide-injected">
<header>
  <h1>Agent traffic</h1>
  <div id="status" class="down"><span class="dot"></span><span id="statustext">connecting…</span></div>
  <select id="provider" aria-label="Provider"><option value="all">All providers</option><option value="codex">Codex</option><option value="claude">Claude Code</option></select>
  <select id="session" title="Session"><option value="auto">Latest session (auto)</option></select>
  <div class="filters">
    <label><input type="checkbox" id="f-side"> side calls</label>
    <label><input type="checkbox" id="f-think" checked> thinking</label>
    <label><input type="checkbox" id="f-inj"> show context</label>
  </div>
  <input type="search" id="search" placeholder="Search thread…">
  <span id="matches"></span>
  <span id="totals"></span>
</header>
<div id="connection-detail" role="status" hidden></div>
<div id="launch-help">Start an agent: <code id="launch-command">./codex-proxy</code></div>
<main id="scroller">
  <div id="thread">
    <div class="empty" id="empty">Waiting for traffic…<br>Run the agent with <code>./codex-proxy</code></div>
  </div>
</main>
<button id="newbtn" hidden></button>
<script>
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const thread = $('#thread'), scroller = $('#scroller');

const ICON = {
  user: '<circle cx="8" cy="5" r="3"/><path d="M2.5 15c0-3.2 2.5-5 5.5-5s5.5 1.8 5.5 5"/>',
  model: '<path d="M8 1.5l1.6 4.9 4.9 1.6-4.9 1.6L8 14.5l-1.6-4.9L1.5 8l4.9-1.6z"/>',
  tool: '<path d="M10.5 1.8a3.6 3.6 0 0 0-3.9 4.9L1.8 11.5a1.4 1.4 0 0 0 2 2l4.8-4.8a3.6 3.6 0 0 0 4.9-3.9l-2.1 2.1-2-.3-.3-2z"/>',
  result: '<path d="M3 2.5v6h10M10 5.5l3 3-3 3"/>',
  think: '<path d="M4 12.5a3 3 0 0 1-.4-6A4.3 4.3 0 0 1 12 5.6a3.4 3.4 0 0 1 .4 6.9z"/><circle cx="3" cy="15" r=".8"/>',
  inj: '<circle cx="8" cy="8" r="6.5"/><path d="M8 7.2v4.3M8 4.6v.2"/>',
  err: '<path d="M8 1.8L15 14H1z"/><path d="M8 6.5v3.5M8 12v.2"/>',
};
const icon = k => `<svg class="i" viewBox="0 0 16 16">${ICON[k]}</svg>`;

function h(tag, cls, html) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
}
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const num = n => (n == null ? '–' : Number(n).toLocaleString());
const kfmt = n => n >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : n >= 1e4 ? Math.round(n / 1e3) + 'k' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'k' : String(n);
const secs = ms => ms == null ? '' : ms < 1000 ? ms + ' ms' : (ms / 1000).toFixed(1) + ' s';
const clock = t => new Date(t).toLocaleTimeString([], {hour: '2-digit', minute: '2-digit', second: '2-digit'});

/* BEGIN readable context helpers */
/* Readable-view filtering only: raw requests and forwarded bytes stay untouched. */
const AGENT_CONTEXT_TAGS = new Set([
  'environment_context', 'environment-context', 'system-reminder', 'system_reminder',
  'conversation_context', 'conversation-context', 'context', 'injected_context',
  'developer', 'developer_instructions', 'developer-instructions',
  'system', 'system_instructions', 'system-instructions', 'instructions',
  'permissions', 'permissions_instructions', 'permissions-instructions',
  'collaboration_mode', 'collaboration-mode', 'turn_aborted',
  'user-prompt-submit-hook', 'local-command-caveat',
]);
function isAgentContextTag(name) {
  name = name.toLowerCase();
  return AGENT_CONTEXT_TAGS.has(name) || /^ide_[a-z_]+$/.test(name) || /^local-command-[a-z-]+$/.test(name);
}

function splitAgentContext(value) {
  const source = String(value == null ? '' : value);
  const visible = [], contexts = [], stack = [];
  let i = 0, code = null, root = null;
  const append = text => { if (root) root.text += text; else visible.push(text); };
  const finish = () => {
    if (root) contexts.push({tag: root.tag, text: root.text.trim()});
    root = null;
    stack.length = 0;
  };
  while (i < source.length) {
    // Preserve code examples verbatim, including literal protocol-looking tags.
    const tick = source.slice(i).match(/^(`+|~{3,})/);
    if (tick && (tick[1][0] === '`' || /(^|\n) {0,3}$/.test(source.slice(0, i)))) {
      const mark = tick[1];
      if (!code) code = mark;
      else if (code[0] === mark[0] && (code.length < 3 ? code.length === mark.length : mark.length >= code.length)) code = null;
      append(mark);
      i += mark.length;
      continue;
    }
    if (!code && source[i] === '<') {
      const tag = source.slice(i).match(/^<\s*(\/?)\s*([a-z][a-z0-9_-]*)(?:\s+[^<>]*?)?\s*(\/?)>/i);
      if (tag && isAgentContextTag(tag[2])) {
        const name = tag[2].toLowerCase();
        if (!tag[1]) {
          if (!root) root = {tag: name, text: ''};
          if (!tag[3]) stack.push(name);
          else if (!stack.length) finish();
        } else {
          const at = stack.lastIndexOf(name);
          if (at >= 0) {
            stack.length = at;
            if (!stack.length) finish();
          }
          // A trailing known closing delimiter is protocol noise, not user text.
        }
        i += tag[0].length;
        continue;
      }
      // A stream may currently end halfway through a known opening delimiter.
      // Withhold that suffix until a subsequent repaint completes the block.
      const partial = source.slice(i).match(/^<\s*\/?\s*([a-z][a-z0-9_-]*)$/i);
      if (partial && partial[1].length >= 3 && [...AGENT_CONTEXT_TAGS].some(name => name.startsWith(partial[1].toLowerCase()))) {
        if (!root) root = {tag: partial[1].toLowerCase(), text: ''};
        i = source.length;
        break;
      }
      // Also handle an opening tag whose attributes have not finished streaming.
      const unfinished = source.slice(i).match(/^<\s*([a-z][a-z0-9_-]*)\s+[^<>]*$/i);
      if (unfinished && isAgentContextTag(unfinished[1])) {
        if (!root) root = {tag: unfinished[1].toLowerCase(), text: ''};
        i = source.length;
        break;
      }
    }
    append(source[i++]);
  }
  // Unclosed context blocks remain hidden; their contents may still be streaming.
  finish();
  return {text: visible.join('').trim(), contexts};
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {splitAgentContext, isAgentContextTag};
}

/* Readable message wrappers only; preserve code samples and arbitrary HTML/XML. */
function stripAgentTags(value) {
  const wrappers = new Set(['user_request', 'user_message', 'user_prompt', 'assistant_response', 'final_answer']);
  const source = String(value == null ? '' : value), out = [];
  let i = 0, code = null;
  while (i < source.length) {
    const run = source.slice(i).match(/^(`+|~{3,})/);
    if (run && (run[1][0] === '`' || /(^|\n) {0,3}$/.test(source.slice(0, i)))) {
      const mark = run[1];
      if (!code) code = mark;
      else if (code[0] === mark[0] && (code.length < 3 ? code.length === mark.length : mark.length >= code.length)) code = null;
      out.push(mark);
      i += mark.length;
      continue;
    }
    if (!code && source[i] === '<') {
      const tag = source.slice(i).match(/^<\s*\/?\s*([a-z_][a-z0-9_-]*)(?:\s+[^<>]*?)?\s*\/?>/i);
      if (tag && wrappers.has(tag[1].toLowerCase())) {
        i += tag[0].length;
        continue;
      }
      // Withhold a recognized delimiter suffix while a streaming repaint awaits
      // the rest. A short '<u' remains literal to avoid hiding normal prose.
      const partial = source.slice(i).match(/^<\s*\/?\s*([a-z_][a-z0-9_-]*)$/i);
      if (partial && partial[1].length >= 3 && [...wrappers].some(name => name.startsWith(partial[1].toLowerCase()))) break;
      const attributes = source.slice(i).match(/^<\s*\/?\s*([a-z_][a-z0-9_-]*)\s+[^<>]*$/i);
      if (attributes && wrappers.has(attributes[1].toLowerCase())) break;
    }
    out.push(source[i++]);
  }
  return out.join('');
}
if (typeof module !== 'undefined' && module.exports) module.exports = {stripAgentTags};

/* END readable context helpers */

/* ---------- tiny markdown: fences, inline code, lists, headings, bold/italic ---------- */
function inline(s) {
  return esc(s).split(/(`[^`]+`)/).map(p =>
    p.length > 1 && p[0] === '`' && p[p.length - 1] === '`'
      ? `<code>${p.slice(1, -1)}</code>`
      : p.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
         .replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,:;!?]|$)/g, '$1<i>$2</i>')
         .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
  ).join('');
}
function md(src) {
  const lines = src.split('\n'), out = [];
  let list = null, i = 0;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if (/^\s*```/.test(line)) {
      close();
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      close();
      const buf = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) buf.push(lines[i++]);
      out.push(`<pre>${esc(buf.join('\n'))}</pre>`);
      continue;
    }
    if ((m = line.match(/^(#{1,6})\s+(.*)/))) { close(); const n = Math.min(m[1].length + 2, 6); out.push(`<h${n}>${inline(m[2])}</h${n}>`); }
    else if ((m = line.match(/^\s*[-*+]\s+(.*)/))) { if (list !== 'ul') { close(); out.push('<ul>'); list = 'ul'; } out.push(`<li>${inline(m[1])}</li>`); }
    else if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) { if (list !== 'ol') { close(); out.push('<ol>'); list = 'ol'; } out.push(`<li>${inline(m[1])}</li>`); }
    else if (!line.trim()) { close(); out.push('<div class="gap"></div>'); }
    else { close(); out.push(`<p>${inline(line)}</p>`); }
    i++;
  }
  close();
  return out.join('');
}

/* ---------- state ---------- */
const S = {
  sessions: new Map(), calls: new Map(),
  selected: 'auto', auto: null, shown: null, provider: 'all', proxyPort: 8041, connected: false,
  stick: true, newCount: 0, boot: null, replaying: false, q: '',
};

function session(id, t, provider) {
  let s = S.sessions.get(id);
  if (!s) {
    const root = h('div', 'session');
    root.hidden = true;
    const setup = h('details', 'setup');
    setup.hidden = true;
    root.appendChild(setup);
    thread.appendChild(root);
    s = {id, provider, root, setup, tools: new Map(), first: t, last: t, lastMain: 0, prompt: '',
         totals: {in: 0, out: 0, cr: 0, cw: 0, calls: 0}, tracks: 0};
    S.sessions.set(id, s);
    refreshPicker();
  }
  s.last = Math.max(s.last, t || 0);
  return s;
}
const acceptsProvider = s => s && (S.provider === 'all' || s.provider === S.provider);
const visible = () => S.selected === 'auto' ? S.auto : S.sessions.get(S.selected);
function selectProvider() {
  S.provider = $('#provider').value;
  S.selected = 'auto';
  $('#session').value = 'auto';
  if (S.shown) S.shown.root.hidden = true;
  S.shown = null;
  S.auto = [...S.sessions.values()].filter(acceptsProvider).sort((a, b) => b.last - a.last)[0] || null;
  if (S.auto) show(S.auto);
  $('#empty').hidden = !!S.auto;
  if (!S.auto) $('#empty').textContent = 'Waiting for ' + (S.provider === 'all' ? 'agent' : S.provider) + ' traffic…';
  refreshPicker(); renderTotals(); updateLaunchHelp();
}
function updateLaunchHelp() {
  const suffix = S.proxyPort === 8041 ? '' : ' --proxy-port ' + S.proxyPort;
  $('#launch-command').textContent = S.provider === 'all'
    ? './codex-proxy' + suffix + ' or ./claude-proxy' + suffix
    : './' + S.provider + '-proxy' + suffix;
}
$('#provider').addEventListener('change', selectProvider);

function show(s) {
  if (!acceptsProvider(s) || S.shown === s) return;
  if (S.shown) S.shown.root.hidden = true;
  s.root.hidden = false;
  S.shown = s;
  $('#empty').hidden = true;
  applySearch();
  renderTotals();
  S.stick = true; S.newCount = 0; updateNewBtn();
  scrollBottom();
}

let pickerTimer = 0;
function refreshPicker() {
  if (pickerTimer) return;
  pickerTimer = setTimeout(() => {
    pickerTimer = 0;
    const sel = $('#session'), keep = sel.value;
    const list = [...S.sessions.values()].filter(acceptsProvider).sort((a, b) => b.last - a.last);
    sel.innerHTML = '<option value="auto">Latest session (auto)</option>' + list.map(s =>
      `<option value="${esc(s.id)}">${clock(s.first)} · ${esc(s.id.slice(0, 8))}${s.prompt ? ' · ' + esc(s.prompt.slice(0, 48)) : ''}</option>`).join('');
    sel.value = [...sel.options].some(o => o.value === keep) ? keep : 'auto';
  }, 150);
}

function renderTotals() {
  const s = visible();
  if (!s) { $('#totals').textContent = ''; return; }
  const t = s.totals;
  $('#totals').innerHTML = `${t.calls} calls · in <b>${kfmt(t.in)}</b> · out <b>${kfmt(t.out)}</b> · cache read <b>${kfmt(t.cr)}</b> · write <b>${kfmt(t.cw)}</b>`;
}

/* ---------- following the conversation ---------- */
let scrollRaf = 0;
function scrollBottom() {
  if (scrollRaf) return;
  scrollRaf = requestAnimationFrame(() => { scrollRaf = 0; scroller.scrollTop = scroller.scrollHeight; });
}
const nearBottom = () => scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 60;
scroller.addEventListener('scroll', () => {
  if (scrollRaf) return;
  S.stick = nearBottom();
  if (S.stick && S.newCount) { S.newCount = 0; updateNewBtn(); }
});
function updateNewBtn() {
  const b = $('#newbtn');
  b.hidden = !S.newCount;
  b.textContent = `${S.newCount} new ↓`;
}
$('#newbtn').onclick = () => { S.stick = true; S.newCount = 0; updateNewBtn(); scrollBottom(); };

function grew(s, isNew) {          // something was added to session s
  if (s !== S.shown) return;
  if (S.stick || S.replaying) scrollBottom();
  else if (isNew) { S.newCount++; updateNewBtn(); }
}
function add(parent, node, s) {
  parent.appendChild(node);
  if (S.q) searchNode(node);
  grew(s, true);
  return node;
}

/* ---------- building entries ---------- */
function entry(kind, label, ic, sub) {
  const e = h('div', 'entry ' + kind);
  e.appendChild(h('div', 'lab', `${icon(ic)}<span>${esc(label)}</span>${sub ? `<span class="sub">${esc(sub)}</span>` : ''}`));
  return e;
}
function clampIfLong(el, text, lines = 30, chars = 3500) {
  const n = text.split('\n').length;
  if (el.dataset.clamp || (n <= lines && text.length <= chars)) return;
  el.dataset.clamp = '1';
  el.classList.add('clamped');
  const b = h('button', 'more', `show more (${n.toLocaleString()} lines)`);
  b.onclick = () => {
    const c = el.classList.toggle('clamped');
    b.textContent = c ? `show more (${n.toLocaleString()} lines)` : 'show less';
  };
  el.after(b);
}

function userText(call, text, track) {
  const filtered = splitAgentContext(text);
  const parts = filtered.contexts.map(part => [part.tag, part.text]);
  let rest = stripAgentTags(filtered.text).trim();
  if (/^Caveat: The messages below were generated/.test(rest)) { parts.push(['caveat', rest]); rest = ''; }
  const s = call.s;
  if (rest) {
    const label = track && track.index > 1 ? 'Prompt' : 'You';
    const e = entry('k-user', label, 'user', track && track.index > 1 ? track.label : '');
    const b = h('div', 'body');
    b.textContent = rest;
    e.appendChild(b);
    add(call.body, e, s);
    clampIfLong(b, rest);
    if (!s.prompt && (!track || track.index === 1)) { s.prompt = rest.split('\n')[0]; refreshPicker(); }
  }
  for (const [tag, inner] of parts) injected(call, tag, inner);
}
function injected(call, tag, inner) {
  const e = h('div', 'entry k-inj');
  const d = h('details');
  d.innerHTML = `<summary>${icon('inj')}<span>injected context</span></summary>`;
  const pre = h('pre');
  pre.textContent = inner;
  d.appendChild(pre);
  e.appendChild(d);
  add(call.body, e, call.s);
}

function keyArg(name, input) {
  if (!input || typeof input !== 'object') return typeof input === 'string' ? input.slice(0, 200) : '';
  const str = k => typeof input[k] === 'string' ? input[k] : '';
  const by = {Bash: 'command', PowerShell: 'command', Read: 'file_path', Write: 'file_path', Edit: 'file_path',
    MultiEdit: 'file_path', NotebookEdit: 'notebook_path', Glob: 'pattern', Grep: 'pattern', WebFetch: 'url',
    WebSearch: 'query', Task: 'description', Agent: 'description', Skill: 'skill'};
  let v = by[name] ? str(by[name]) : '';
  if (!v && Array.isArray(input.todos)) v = input.todos.length + ' todos';
  if (!v) for (const k of ['command', 'file_path', 'path', 'pattern', 'url', 'query', 'description', 'prompt', 'name'])
    if (str(k)) { v = str(k); break; }
  if (!v) v = Object.values(input).find(x => typeof x === 'string') || '';
  if (name === 'Grep' && str('path')) v += '   in ' + input.path;
  const first = v.split('\n')[0];
  return first.length > 220 ? first.slice(0, 220) + ' …' : first + (v.includes('\n') ? ' …' : '');
}

function toolCard(call, name, id, input, streaming) {
  const s = call.s;
  const e = entry('k-tool', 'Tool call', 'tool');
  const card = h('div', 'card');
  const head = h('div', 'tool-head');
  head.innerHTML = `<b>${esc(name || '?')}</b><span class="arg mono"></span><span class="tg"></span>`;
  const det = h('details', 'inp');
  det.innerHTML = '<summary>input</summary>';
  const pre = h('pre');
  det.appendChild(pre);
  const results = h('div', 'results');
  card.append(head, det, results);
  e.appendChild(card);
  const t = {e, head, pre, results, name, json: '', id};
  t.set = inp => {
    head.querySelector('.arg').textContent = keyArg(name, inp);
    head.querySelector('.tg').textContent = '';
    pre.textContent = typeof inp === 'string' ? inp : JSON.stringify(inp, null, 2);
  };
  if (streaming) head.querySelector('.tg').textContent = 'streaming input…';
  else t.set(input);
  if (id) s.tools.set(id, t);
  add(call.body, e, s);
  return t;
}

function resultText(c) {
  if (typeof c === 'string') return c;
  if (Array.isArray(c)) return c.map(b => b && b.type === 'text' ? b.text : `[${b && b.type}]`).join('\n');
  return c == null ? '' : JSON.stringify(c, null, 2);
}
function toolResult(call, block) {
  const s = call.s;
  const text = resultText(block.content);
  const r = h('div', 'result' + (block.is_error ? ' err' : ''));
  r.innerHTML = `<div class="lab">${icon(block.is_error ? 'err' : 'result')}<span>${block.is_error ? 'Result · error' : 'Result'}</span></div>`;
  const pre = h('pre');
  pre.textContent = text || '(empty)';
  r.appendChild(pre);
  const t = s.tools.get(block.tool_use_id);
  if (t) {
    t.results.appendChild(r);
    if (S.q) searchNode(t.e);
    grew(s, true);
  } else {
    const e = entry('k-result', 'Tool result', 'result', 'call not in view');
    e.appendChild(r);
    add(call.body, e, s);
  }
  clampIfLong(pre, text, 15, 2500);
}

function thinking(call, text, redacted, streaming) {
  const e = h('div', 'entry k-think');
  const d = h('details');
  const sum = h('summary');
  const pre = h('pre');
  d.append(sum, pre);
  e.appendChild(d);
  const t = {e, sum, pre, text: text || ''};
  t.render = (done) => {
    const tok = Math.round(t.text.length / 4);
    sum.innerHTML = `${icon('think')}<span>${!t.text && done ? 'thinking (redacted, content omitted)' : `thinking (~${tok.toLocaleString()} tokens)${done ? '' : ' …'}`}</span>`;
    pre.textContent = t.text;
    pre.hidden = !t.text;
  };
  t.render(!streaming);
  add(call.body, e, call.s);
  return t;
}

function modelText(call, text, streaming) {
  const e = entry('k-model', 'Model', 'model');
  const b = h('div', 'body md');
  e.appendChild(b);
  const t = {e, b, text: text || ''};
  t.render = (done) => {
    const parsed = splitAgentContext(t.text);
    const readable = stripAgentTags(parsed.text);
    if (done && !t.contextRendered) {
      for (const context of parsed.contexts) injected(call, context.tag, context.text);
      t.contextRendered = true;
    }
    e.hidden = !readable.trim();
    b.innerHTML = md(readable) + (done ? '' : '<span class="cursor"></span>');
    if (done) clampIfLong(b, readable, 60, 6000);
  };
  t.render(!streaming);
  add(call.body, e, call.s);
  return t;
}

function errorEntry(call, msg) {
  const e = entry('k-error', 'Error', 'err');
  const b = h('div', 'body mono');
  b.textContent = msg;
  e.appendChild(b);
  add(call.body, e, call.s);
}

function divider(call, text) {
  add(call.body, h('div', 'divider', esc(text)), call.s);
}

function renderMessage(call, msg, track) {
  const blocks = typeof msg.content === 'string' ? [{type: 'text', text: msg.content}] : (msg.content || []);
  if (msg.role === 'system' || msg.role === 'developer') {
    const text = blocks.map(b => b && (b.text || b.thinking || '')).filter(Boolean).join('\n');
    if (text) injected(call, msg.role, text);
    return;
  }
  for (const b of blocks) {
    if (!b) continue;
    if (msg.role === 'user') {
      if (b.type === 'text') userText(call, b.text || '', track);
      else if (b.type === 'tool_result') toolResult(call, b);
      else userText(call, `[${b.type}]`, track);
    } else {
      if (b.type === 'text') { if ((b.text || '').trim()) modelText(call, b.text, false); }
      else if (b.type === 'tool_use' || b.type === 'server_tool_use') toolCard(call, b.name, b.id, b.input, false);
      else if (b.type === 'thinking') thinking(call, b.thinking || '', !b.thinking, false);
      else if (b.type === 'redacted_thinking') thinking(call, '', true, false);
      else if (b.tool_use_id) toolResult(call, {tool_use_id: b.tool_use_id, content: b.content, is_error: false});
      else modelText(call, `\`[${b.type}]\``, false);
    }
  }
}

/* ---------- raw view ---------- */
function pretty(body) {
  if (!body) return '(empty)';
  try { return JSON.stringify(JSON.parse(body), null, 2); } catch (e) { return body; }
}
function rawSection(title, line, headers, body) {
  const wrap = h('div');
  wrap.appendChild(h('h4', '', esc(title) + ' · ' + esc(line)));
  const hp = h('pre');
  hp.textContent = (headers || []).map(([k, v]) => `${k}: ${v}`).join('\n') || '(no headers)';
  const bp = h('pre');
  bp.textContent = pretty(body);
  wrap.append(hp, bp);
  return wrap;
}
async function toggleRaw(call) {
  if (!call.raw) {
    call.raw = h('div', 'raw');
    call.raw.hidden = true;
    (call.side ? call.root : call.foot).after(call.raw);
  }
  const r = call.raw;
  if (!r.hidden) { r.hidden = true; return; }
  r.hidden = false;
  if (r.dataset.loaded) return;
  r.textContent = 'loading…';
  try {
    const res = await fetch('/raw/' + encodeURIComponent(call.id));
    if (!res.ok) throw new Error(res.status === 404 ? 'no longer kept (raw bodies are kept for the last 50 calls)' : 'HTTP ' + res.status);
    const j = await res.json();
    r.textContent = '';
    r.append(rawSection('Request', `${j.request.method} ${j.request.path}`, j.request.headers, j.request.body),
             rawSection('Response', `${j.response.status ?? '…'} ${j.response.reason || ''}`, j.response.headers, j.response.body));
    if (call.ended) r.dataset.loaded = '1';
  } catch (e) {
    r.textContent = 'could not load raw: ' + e.message;
  }
}

/* ---------- event handlers ---------- */
function onCallStart(ev) {
  const provider = ev.provider || (ev.path.includes('responses') ? 'codex' : 'claude');
  const s = session(provider + ':' + ev.session_id, ev.time, provider);
  const side = ev.kind !== 'main';
  const root = h('div', 'call ' + (side ? 'side' : 'main'));
  s.root.appendChild(root);
  const call = {id: ev.call_id, ev, s, root, body: root, side, blocks: new Map(), ended: false, sideText: ''};
  S.calls.set(ev.call_id, call);

  if (side) {
    const row = h('div', 'siderow', `<span class="k">side</span><span class="x"></span><span class="st">…</span>`);
    row.title = 'click for raw request/response';
    row.onclick = () => toggleRaw(call);
    root.appendChild(row);
    call.row = row;
    updateSideRow(call);
    if (S.q) searchNode(row);
    grew(s, false);
  } else {
    s.lastMain = ev.time;
    const tr = ev.track || {index: 1, label: 'main'};
    s.tracks = Math.max(s.tracks, tr.total || 1);
    const sysLine = `system prompt ${num(ev.system_summary.length)} chars · ${ev.tools.length} tools`;
    if (s.setup.hidden) {
      s.setup.hidden = false;
      s.setup.innerHTML = `<summary>Setup · ${esc(ev.model || '')} · ${esc(sysLine)}</summary>
        <div>System prompt starts with: <i>${esc(ev.system_summary.first_line)}</i></div>
        <div class="tools">${ev.tools.map(t => `<span>${esc(t)}</span>`).join('')}</div>`;
    }
    if (tr.new && tr.index > 1)
      divider(call, `new conversation thread "${tr.label}" (subagent or replaced context)`);
    if (tr.rewritten)
      divider(call, `history rewritten by the agent: ${tr.rewritten} message${tr.rewritten === 1 ? '' : 's'} replaced`);
    for (const m of ev.new_messages || []) renderMessage(call, m, tr);
    call.track = tr;
    const foot = h('div', 'footer');
    foot.innerHTML = `<span>${esc(ev.model || '')}</span><span class="stream">streaming…</span>`;
    root.appendChild(foot);
    call.foot = foot;
    grew(s, false);
  }
  if (acceptsProvider(s) && (!side || !S.auto)) {
    if (S.auto !== s) {
      S.auto = s;
      if (S.selected === 'auto') show(s);
      $('#empty').hidden = true;
    }
  }
  if (s === S.shown) renderTotals();
}

function updateSideRow(call) {
  const ev = call.ev, e = call.end;
  const bits = [ev.side_reason];
  if (ev.model) bits.push(ev.model);
  const preview = stripAgentTags(splitAgentContext(call.sideText).text).trim();
  if (preview) bits.push('“' + preview.replace(/\s+/g, ' ').slice(0, 120) + '”');
  call.row.querySelector('.x').textContent = bits.join(' · ');
  call.row.querySelector('.st').textContent = e
    ? `${e.status ?? 'ERR'} · ${secs(e.duration_ms)}${e.error ? ' · ' + e.error.slice(0, 80) : ''}`
    : '…';
  call.row.style.color = e && e.error ? 'var(--err)' : '';
}

function onBlockStart(ev) {
  const call = S.calls.get(ev.call_id);
  if (!call) return;
  const t = ev.block_type;
  if (call.side) { call.blocks.set(ev.index, {type: t, text: ''}); return; }
  let b;
  if (t === 'text') b = modelText(call, '', true);
  else if (t === 'tool_use' || t === 'server_tool_use') b = toolCard(call, ev.tool_name, ev.tool_use_id, null, true);
  else if (t === 'thinking' || t === 'redacted_thinking') b = thinking(call, '', t === 'redacted_thinking', true);
  else b = {type: t, placeholder: true};
  b.type = t;
  b.text = b.text || '';
  b.json = '';
  call.blocks.set(ev.index, b);
}

const dirty = new Set();
let dirtyRaf = 0;
function markDirty(b, s) {
  dirty.add(b);
  if (!dirtyRaf) dirtyRaf = requestAnimationFrame(() => {
    dirtyRaf = 0;
    for (const x of dirty) if (x.render) x.render(false);
    dirty.clear();
    if (S.shown && S.stick) scrollBottom();
  });
}

function onBlockDelta(ev) {
  const call = S.calls.get(ev.call_id);
  const b = call && call.blocks.get(ev.index);
  if (!b) return;
  if (ev.text != null) b.text += ev.text;
  if (ev.partial_json != null) b.json += ev.partial_json;
  if (call.side) { if (ev.text) { call.sideText += ev.text; updateSideRow(call); } return; }
  if (b.pre && b.set && ev.partial_json != null) {   // tool input streaming
    b.pre.textContent = b.json;
    b.head.querySelector('.tg').textContent = `streaming input… ${b.json.length.toLocaleString()} chars`;
    const m = b.json.match(/^\s*\{\s*"[^"]+"\s*:\s*"((?:[^"\\]|\\.){1,200})/);
    if (m) b.head.querySelector('.arg').textContent = m[1].replace(/\\n/g, ' ').replace(/\\"/g, '"');
    return;
  }
  markDirty(b, call.s);
}

function onBlockStop(ev) {
  const call = S.calls.get(ev.call_id);
  const b = call && call.blocks.get(ev.index);
  if (!b) return;
  if (ev.text != null) b.text = ev.text;
  if (call.side) { call.sideText = b.text || call.sideText; updateSideRow(call); return; }
  dirty.delete(b);
  b.done = true;
  if (b.set) { b.set(ev.input !== undefined ? ev.input : b.json); }
  else if (b.render) {
    if (b.type === 'text' && !b.text.trim()) { b.e.remove(); return; }
    b.render(true);
  } else if (ev.extra) {
    if (ev.extra.tool_use_id) toolResult(call, {tool_use_id: ev.extra.tool_use_id, content: ev.extra.content, is_error: false});
  }
  if (S.q && b.e) searchNode(b.e);
  grew(call.s, false);
}

function onCallEnd(ev) {
  const call = S.calls.get(ev.call_id);
  if (!call) return;
  call.ended = true;
  call.end = ev;
  const s = call.s, u = ev.usage || {};
  s.totals.calls++;
  s.totals.in += u.input || 0; s.totals.out += u.output || 0;
  s.totals.cr += u.cache_read || 0; s.totals.cw += u.cache_write || 0;
  if (s === S.shown) renderTotals();
  for (const b of call.blocks.values()) {   // a stream cut off mid-block: drop the cursor
    if (!b.done && b.render && b.e && b.e.isConnected) { b.done = true; dirty.delete(b); b.render(true); }
  }
  if (ev.error || (ev.status && ev.status >= 400)) {
    connectionDetail(recoveryMessage(ev), 'upstream');
  } else if (S.connected && $('#connection-detail').dataset.kind === 'upstream') {
    connectionDetail('');
  }
  if (call.side) { updateSideRow(call); return; }
  if (ev.error) errorEntry(call, recoveryMessage(ev) + " " + ev.error);
  const f = call.foot;
  const bits = [
    `<span>${esc(ev.model || call.ev.model || '')}</span>`,
    `<span>${secs(ev.duration_ms)}</span>`,
    `<span>in ${num(u.input)} · out ${num(u.output)}</span>`,
    `<span>cache read ${num(u.cache_read)} · write ${num(u.cache_write)}</span>`,
    `<span class="${ev.error || (ev.status && ev.status >= 400) ? 'bad' : ''}">${esc(ev.stop_reason || (ev.status ? 'HTTP ' + ev.status : 'no reply'))}</span>`,
  ];
  if (s.tracks > 1 && call.track) bits.push(`<span class="tag">${esc(call.track.label)}</span>`);
  f.innerHTML = bits.join('');
  const raw = h('button', 'linkbtn', 'raw');
  raw.onclick = () => toggleRaw(call);
  f.appendChild(raw);
  grew(s, false);
}

/* ---------- search ---------- */
function searchNode(n) {
  if (!n.classList) return;
  const hiddenContext = document.body.classList.contains('hide-injected') && n.classList.contains('k-inj');
  const hit = !hiddenContext && (!S.q || n.textContent.toLowerCase().includes(S.q));
  if (n.classList.contains('entry') || n.classList.contains('siderow')) n.classList.toggle('miss', !hit);
}
function applySearch() {
  const s = S.shown;
  if (!s) return;
  let hits = 0;
  s.root.querySelectorAll('.entry, .siderow').forEach(n => {
    searchNode(n);
    if (S.q && !n.classList.contains('miss')) hits++;
  });
  s.root.querySelectorAll('.footer, .divider').forEach(n => n.classList.toggle('miss', !!S.q));
  $('#matches').textContent = S.q ? `${hits} match${hits === 1 ? '' : 'es'}` : '';
}
let searchTimer = 0;
$('#search').addEventListener('input', e => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { S.q = e.target.value.trim().toLowerCase(); applySearch(); }, 150);
});

/* ---------- header controls ---------- */
$('#session').addEventListener('change', e => {
  S.selected = e.target.value;
  const s = visible();
  if (s) { S.shown && (S.shown.root.hidden = true); S.shown = null; show(s); }
});
const bindFilter = (id, cls) => {
  const c = $(id);
  const apply = () => document.body.classList.toggle(cls, !c.checked);
  c.addEventListener('change', () => { apply(); applySearch(); if (S.stick) scrollBottom(); });
  apply();
};
bindFilter('#f-side', 'hide-side');
bindFilter('#f-think', 'hide-thinking');
bindFilter('#f-inj', 'hide-injected');

/* ---------- connection ---------- */
function connectionDetail(message, kind = '') {
  const detail = $('#connection-detail');
  detail.textContent = message;
  detail.hidden = !message;
  detail.dataset.kind = kind;
  detail.classList.toggle('error', !!message);
}
function recoveryMessage(ev) {
  const provider = S.calls.get(ev.call_id)?.s.provider || 'agent';
  if (ev.status === 401 || ev.status === 403)
    return 'Authentication failed (' + ev.status + '). Sign in again with ' + provider + ' login, then restart the agent.';
  if (ev.status === 429)
    return 'Rate limit reached (429). Wait before retrying; check your provider usage limits.';
  if (!ev.status || ev.status === 502 || ev.status === 504)
    return 'Cannot reach upstream. Check your internet connection and upstream URL, then retry.';
  return 'Upstream returned HTTP ' + ev.status + '. Open raw details and retry when the provider recovers.';
}
async function checkHealth(disconnected = false) {
  try {
    const response = await fetch('/health', {signal: AbortSignal.timeout(3000)});
    if (!response.ok) throw new Error('health HTTP ' + response.status);
    const health = await response.json();
    S.proxyPort = health.proxy_port || 8041;
    updateLaunchHelp();
    if (disconnected && !S.connected)
      connectionDetail('Dashboard stream disconnected. The proxy is running; reconnecting automatically…', 'connection');
  } catch (error) {
    if (!S.connected) connectionDetail('Proxy unavailable. Start it with ./start, then reload this page.', 'connection');
  }
}
function setStatus(live, text) {
  S.connected = live;
  $('#status').className = live ? 'live' : 'down';
  $('#statustext').textContent = text;
  if (live && $('#connection-detail').dataset.kind === 'connection') connectionDetail('');
}
function connect() {
  const es = new EventSource('/events');
  es.onopen = () => setStatus(true, 'live');
  es.onerror = () => { setStatus(false, 'reconnecting…'); checkHealth(true); };
  es.onmessage = m => {
    let ev;
    try { ev = JSON.parse(m.data); } catch (e) { return; }
    try {
      switch (ev.type) {
        case 'hello':
          if (S.boot && S.boot !== ev.boot) { location.reload(); return; }   // proxy restarted
          S.boot = ev.boot;
          if (ev.replay) S.replaying = true;
          setStatus(true, 'live');
          break;
        case 'replay_done': S.replaying = false; scrollBottom(); break;
        case 'call_start': onCallStart(ev); break;
        case 'block_start': onBlockStart(ev); break;
        case 'block_delta': onBlockDelta(ev); break;
        case 'block_stop': onBlockStop(ev); break;
        case 'call_end': onCallEnd(ev); break;
      }
    } catch (e) { console.error('dashboard event failed', ev, e); }
  };
}
checkHealth();
connect();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
