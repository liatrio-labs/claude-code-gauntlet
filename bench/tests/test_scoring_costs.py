import gzip
import json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from bench.adjudicator.adjudicate import adjudicate
from bench.runner.scoring_costs import recording_proxy, total_cost

REPLY = {
    "model": "claude-opus-4-5-20251101",
    "choices": [{"message": {"content": "{}"}}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
}


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"prompt_tokens": None, "completion_tokens": 1},
        {"prompt_tokens": "100", "completion_tokens": 1},
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": -1, "completion_tokens": 1},
        {"prompt_tokens": 10, "completion_tokens": None},
        {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": None},
        {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": []},
        {"prompt_tokens": 10, "completion_tokens": 1, "prompt_tokens_details": "bad"},
        {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": None},
        },
        {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": "2"},
        },
        {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": -1},
        },
        {
            "prompt_tokens": 10,
            "completion_tokens": 1,
            "prompt_tokens_details": {"cached_tokens": 11},
        },
        {"input_tokens": None, "output_tokens": 1},
        {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": "2"},
    ],
)
def test_adjudication_keeps_verdict_with_unknown_cost_for_malformed_usage(usage):
    reply = {
        **REPLY,
        "usage": usage,
        "choices": [{"message": {"content": '{"bucket":"noise","reason":"checked"}'}}],
    }
    result = adjudicate(
        "claim",
        "hunk",
        "context",
        REPLY["model"],
        "synthetic",
        transport=lambda *args: reply,
    )
    assert result["bucket"] == "noise" and result["reason"] == "checked"
    assert result["cost_usd"] is None
    assert result["usage"] == [{"model": REPLY["model"], "usage": None}]


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        body = json.dumps(REPLY).encode()
        gz = "gzip" in self.headers.get("Accept-Encoding", "")
        self.send_response(200)
        if gz:
            body = gzip.compress(body)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_proxy_relays_plain_json_and_records_only_usage(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    Thread(target=server.serve_forever, daemon=True).start()
    receipt = tmp_path / "judge-usage.json"
    try:
        with recording_proxy(
            f"http://127.0.0.1:{server.server_port}/", REPLY["model"], receipt
        ) as base:
            request = urllib.request.Request(
                base + "chat/completions",
                data=json.dumps({"messages": [{"content": "PROMPT-TEXT"}]}).encode(),
                headers={
                    "Authorization": "Bearer SECRET-KEY",
                    "Accept-Encoding": "gzip",
                },
            )
            with urllib.request.urlopen(request) as response:
                assert json.loads(response.read()) == REPLY
    finally:
        server.shutdown()
    text = receipt.read_text(encoding="utf-8")
    assert "SECRET-KEY" not in text and "PROMPT-TEXT" not in text
    assert total_cost(json.loads(text)) == (1000 * 5.0 + 100 * 25.0) / 1_000_000
