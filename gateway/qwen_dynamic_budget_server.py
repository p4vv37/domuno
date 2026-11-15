#!/usr/bin/env python3
"""Offline CPU-only prompt counter; no inference backend or network model loading."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import sys
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MAX_BODY_BYTES = 2 * 1024 * 1024


def read_gguf_metadata(path):
    """Read the metadata prefix only; never seek into or load tensor data."""
    with open(path, "rb") as stream:
        def exact(n):
            data = stream.read(n)
            if len(data) != n:
                raise ValueError("Truncated GGUF metadata")
            return data

        def unpack(fmt):
            return struct.unpack("<" + fmt, exact(struct.calcsize(fmt)))[0]

        def string():
            size = unpack("Q")
            if size > 64 * 1024 * 1024:
                raise ValueError("Oversized GGUF metadata string")
            return exact(size).decode("utf-8")

        def value(kind):
            if kind == 8:
                return string()
            if kind == 9:
                subtype, size = unpack("I"), unpack("Q")
                if size > 2_000_000 or subtype == 9:
                    raise ValueError("Unsupported GGUF metadata array")
                return [value(subtype) for _ in range(size)]
            formats = {0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i",
                       6: "f", 7: "?", 10: "Q", 11: "q", 12: "d"}
            if kind not in formats:
                raise ValueError("Unknown GGUF metadata type")
            return unpack(formats[kind])

        if exact(4) != b"GGUF" or unpack("I") != 3:
            raise ValueError("Expected GGUF v3")
        unpack("Q")  # tensor count; no tensor descriptor/data reading
        count = unpack("Q")
        if count > 100_000:
            raise ValueError("Oversized GGUF metadata table")
        result = {}
        for _ in range(count):
            key = string()
            if key in result:
                raise ValueError("Duplicate GGUF metadata key")
            result[key] = value(unpack("I"))
        return result


def validate_tokenizer(metadata, tokenizer_json):
    """Check all official vocabulary IDs, added tokens, and BPE merges.

    GGUF may contain extra padded vocabulary entries. Validation is necessary,
    not a proof of native pretokenizer or template-renderer equivalence.
    """
    data = json.loads(Path(tokenizer_json).read_text(encoding="utf-8"))
    model = data["model"]
    if (model["type"] != "BPE" or metadata.get("tokenizer.ggml.model") != "gpt2"
            or metadata.get("tokenizer.ggml.pre") != "qwen2"
            or metadata.get("tokenizer.ggml.add_bos_token") is not False):
        raise ValueError("Unexpected tokenizer family or BOS policy")
    tokens = metadata["tokenizer.ggml.tokens"]
    for token, index in list(model["vocab"].items()) + [
            (item["content"], item["id"]) for item in data.get("added_tokens", [])]:
        if not 0 <= index < len(tokens) or tokens[index] != token:
            raise ValueError("Tokenizer vocabulary ID mismatch")
    merges = [" ".join(x) if isinstance(x, list) else x for x in model["merges"]]
    if merges != metadata["tokenizer.ggml.merges"]:
        raise ValueError("Tokenizer merge order mismatch")
    if metadata.get("tokenizer.ggml.eos_token_id") != 151645:
        raise ValueError("Unexpected EOS ID")
    return {"vocabulary_and_merges_verified": True,
            "tokenizer_sha256": hashlib.sha256(Path(tokenizer_json).read_bytes()).hexdigest()}


def normalize_go_http_messages(messages):
    """Project text-only HTTP messages to the frozen Go SDK message shape.

    Source: GenieX 7b234f6c2f75c64b64bcbfd24eb47e6dec4028dd,
    cli/server/handler/chat_messages.go (buildLLMMessages/contentText).
    Ordinary arrays expand; tool branches concatenate. Nil is accepted only
    in tool branches, as in contentText. Reject unsupported/nontext inputs
    conservatively, including parts contentText might silently discard.
    This is NOT llama e467e5b capability normalization: argument strings stay
    strings, without JSON parsing or C-pointer defaults. No parity certification.
    """
    if not isinstance(messages, list):
        raise ValueError("messages must be a list")
    result = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            raise ValueError("message and role must be an object and string")
        calls = message.get("tool_calls", [])
        if calls is None:
            calls = []
        if not isinstance(calls, list):
            raise ValueError("tool_calls must be a list")
        call_id = message.get("tool_call_id")
        if call_id is not None and not isinstance(call_id, str):
            raise ValueError("tool_call_id must be a string")
        content = message.get("content")
        tool_branch = bool(calls) or call_id is not None
        if content is None and tool_branch:
            parts = [""]
        elif isinstance(content, str):
            parts = [content]
        elif isinstance(content, list):
            parts = []
            for part in content:
                if (not isinstance(part, dict) or part.get("type") != "text"
                        or not isinstance(part.get("text"), str)):
                    raise ValueError("only string text parts are supported")
                parts.append(part["text"])
        else:
            raise ValueError("unsupported message content")
        normalized_calls = []
        for call in calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ValueError("only function tool calls are supported")
            function = call.get("function")
            if (not isinstance(function, dict)
                    or not isinstance(function.get("name"), str)
                    or not isinstance(function.get("arguments"), str)
                    or not isinstance(call.get("id", ""), str)):
                raise ValueError("function name, arguments and call ID must be strings")
            normalized_calls.append({"type": "function", "id": call.get("id", ""),
                                     "function": {"name": function["name"],
                                                  "arguments": function["arguments"]}})
        if tool_branch:
            item = {"role": message["role"], "content": "".join(parts)}
            if calls:  # Tool calls precede and suppress the tool-result branch.
                item["tool_calls"] = normalized_calls
            else:
                item["tool_call_id"] = call_id
            result.append(item)
        else:
            result.extend({"role": message["role"], "content": part} for part in parts)
    return result



def estimate(body, metadata, tokenizer, normalize):
    """Estimate a text prompt with freely changeable function tool schemas."""
    if not isinstance(body, dict):
        raise ValueError("invalid body")
    if body.get('functions'):
        raise ValueError('legacy functions unsupported by this text budget profile')
    tools = body.get("tools", [])
    if tools is None:
        raise ValueError("invalid tools")
    if not isinstance(tools, list):
        raise ValueError("invalid tools")
    for tool in tools:
        if (not isinstance(tool, dict)
                or not isinstance(tool.get("function"), dict)
                or not isinstance(tool["function"].get("name"), str)):
            raise ValueError("invalid tool")
    messages = normalize(body.get("messages"))
    rendered = tokenizer.apply_chat_template(
        messages, tools=body.get("tools"),
        chat_template=metadata["tokenizer.chat_template"],
        tokenize=False, add_generation_prompt=True)
    tokens = len(tokenizer.encode(rendered, add_special_tokens=False))
    # Conservative experimental bound for the frozen Qwen2.5 text profile,
    # not a mathematical or universally certified bound. Prior CPU/native
    # comparisons differed by +1/+4 tokens; keep an explicit 1024-token reserve
    # plus per-message/tool reserves rather than claiming native parity.
    return {"prompt_upper_bound": tokens + 1024 + 16 * len(messages) + 16 * len(tools),
            "estimated_tokens": tokens, "native_parity_certified": False}


def make_handler(metadata, tokenizer, normalize):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log request content or exception details.

        def reply(self, status, body):
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/healthz":
                self.reply(200, {"ready": True, "counter_only": True, "npu": False})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/count":
                self.reply(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= MAX_BODY_BYTES or self.headers.get("Transfer-Encoding"):
                    raise ValueError("invalid length")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError("incomplete body")
                result = estimate(json.loads(raw), metadata, tokenizer, normalize)
            except Exception:
                self.close_connection = True
                self.reply(503, {"error": "counter unavailable"})
                return
            self.reply(200, result)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18193)
    parser.add_argument("--gguf", required=True)
    parser.add_argument("--tokenizer", required=True)
    args = parser.parse_args()
    try:
        if not ipaddress.ip_address(args.host).is_loopback:
            raise ValueError("loopback required")
        metadata = read_gguf_metadata(args.gguf)
        validate_tokenizer(metadata, Path(args.tokenizer) / "tokenizer.json")
        if not isinstance(metadata["tokenizer.chat_template"], str):
            raise ValueError("missing template")
        from transformers import AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(
            args.tokenizer, local_files_only=True, trust_remote_code=False)
        server = ThreadingHTTPServer(
            (args.host, args.port),
            make_handler(metadata, tokenizer, normalize_go_http_messages))
    except Exception:
        print("counter startup failed", file=sys.stderr)
        return 1
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
