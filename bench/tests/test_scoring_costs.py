import gzip
import json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from bench.runner.scoring_costs import recording_proxy, total_cost

REPLY = {
    "model": "claude-opus-4-5-20251101",
    "choices": [{"message": {"content": "{}"}}],
    "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
}


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
    text = receipt.read_text()
    assert "SECRET-KEY" not in text and "PROMPT-TEXT" not in text
    assert total_cost(json.loads(text)) == (1000 * 5.0 + 100 * 25.0) / 1_000_000
