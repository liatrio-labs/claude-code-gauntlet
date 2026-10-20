"""Response usage receipts for the pinned scoring model, in USD per million tokens."""

import json
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from typing import TypedDict

from bench.runner.epochs import read_json

# Dated model pricing keeps rescoring independent of a moving model alias.
PRICES = {"claude-opus-4-5-20251101": (5.0, 25.0, 0.5, 6.25)}
COUNTS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class Usage(TypedDict):
    model: str | None
    usage: dict[str, int] | None


def usage_record(reply, pin) -> Usage:
    model, usage = reply.get("model", pin), reply.get("usage")
    counts = None
    if isinstance(usage, dict):
        if "prompt_tokens" in usage:
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            counts = [
                usage["prompt_tokens"] - cached,
                usage.get("completion_tokens"),
                cached,
                0,
            ]
        elif "input_tokens" in usage:
            counts = [
                usage.get(k, 0) if k.startswith("cache_") else usage.get(k)
                for k in COUNTS
            ]
        if counts is not None and any(type(n) is not int or n < 0 for n in counts):
            counts = None
    return Usage(
        model=model,
        usage=dict(zip(COUNTS, counts, strict=True)) if counts is not None else None,
    )


def total_cost(records):
    total = 0.0
    for record in records:
        prices, usage = PRICES.get(record["model"]), record["usage"]
        if prices is None or usage is None:
            return None
        charge = sum(usage[k] * p for k, p in zip(COUNTS, prices, strict=True))
        total += charge / 1_000_000
    return total


@contextmanager
def recording_proxy(base_url, pin, receipt):
    """Forward only chat completions; persist model and token counts, never prompts or keys."""
    records, lock = [], Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if self.path != "/chat/completions":
                self.send_error(404)
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in ("host", "connection", "content-length")
            }
            request = urllib.request.Request(
                base_url.rstrip("/") + self.path, data=body, headers=headers
            )
            try:
                try:
                    response = urllib.request.urlopen(request, timeout=180)
                except urllib.error.HTTPError as exc:
                    response = exc
                with response:
                    data, status = response.read(), response.code
                try:
                    record = usage_record(json.loads(data), pin)
                except (ValueError, TypeError, AttributeError):
                    record = usage_record({}, pin)
            except OSError:
                data, status, record = (
                    b'{"error":"scoring transport failed"}',
                    502,
                    usage_record({}, pin),
                )
            with lock:
                records.append(record)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        from bench.runner.observations import exclusive_json

        exclusive_json(receipt, records)


def read_costs(model_dir):
    return {
        stage: total_cost(read_json(path))
        if (path := model_dir / (stage + "-usage.json")).is_file()
        else None
        for stage in ("dedup", "judge")
    }
