"""Relay overloads (2026-10-10): the first research call got 502 server_is_overloaded six
times in ~1 minute and the whole article failed with 0 calls made. The client now waits
out an overload and can switch to a configured fallback model."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from hermes_adapter.llm import ChatClient, LLMError
from tests.test_hermes_adapter import _cfg

OVERLOADED = json.dumps({"error": {"type": "service_unavailable_error", "code": "server_is_overloaded",
                                   "message": "Our servers are currently overloaded."}}).encode()


def _server(ok_after: int, ok_models: set[str] | None = None):
    calls: list[str] = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            calls.append(body["model"])
            good = (ok_models is not None and body["model"] in ok_models) or \
                   (ok_models is None and len(calls) > ok_after)
            out = json.dumps({"choices": [{"message": {"role": "assistant", "content": "pong"},
                                           "finish_reason": "stop"}], "usage": {}}).encode() if good else OVERLOADED
            self.send_response(200 if good else 502)
            self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, calls


def test_long_overload_is_waited_out(monkeypatch):
    monkeypatch.setattr("hermes_adapter.llm.time.sleep", lambda s: None)
    srv, calls = _server(ok_after=9)
    try:
        cfg = _cfg(base_url=f"http://127.0.0.1:{srv.server_port}/v1", overload_wait_minutes=20)
        res = ChatClient(cfg).chat(model="m", messages=[], tools=None, stage="research")
        assert res.message["content"] == "pong" and len(calls) == 10   # beyond the old 6-attempt cap
    finally:
        srv.shutdown()


def test_overload_switches_to_fallback_model(monkeypatch):
    monkeypatch.setattr("hermes_adapter.llm.time.sleep", lambda s: None)
    srv, calls = _server(ok_after=0, ok_models={"gpt-5.6-sol"})
    try:
        cfg = _cfg(base_url=f"http://127.0.0.1:{srv.server_port}/v1",
                   fallback_models={"gpt-6.1-sol": "gpt-5.6-sol"})
        res = ChatClient(cfg).chat(model="gpt-6.1-sol", messages=[], tools=None, stage="research")
        assert res.message["content"] == "pong"
        assert calls[:3] == ["gpt-6.1-sol"] * 3 and calls[-1] == "gpt-5.6-sol"
    finally:
        srv.shutdown()


def test_overload_still_gives_up_eventually(monkeypatch):
    monkeypatch.setattr("hermes_adapter.llm.time.sleep", lambda s: None)
    srv, calls = _server(ok_after=10_000)
    try:
        cfg = _cfg(base_url=f"http://127.0.0.1:{srv.server_port}/v1", overload_wait_minutes=0)
        try:
            ChatClient(cfg).chat(model="m", messages=[], tools=None, stage="research")
            raise AssertionError("expected LLMError")
        except LLMError as e:
            assert "server_is_overloaded" in str(e)
        assert len(calls) == 6
    finally:
        srv.shutdown()
