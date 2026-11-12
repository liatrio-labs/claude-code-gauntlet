"""Lossless Workflow measurement transport and its untrusted-input bounds."""

import base64
import json
import struct
import subprocess
from pathlib import Path

import pytest
from gauntlet import measurement

ROOT = Path(__file__).resolve().parents[1]


def packet_text(packet):
    # Literal LZW codes let malformed trees reach the decoder without a second codec.
    raw = json.dumps(packet, separators=(",", ":")).encode("ascii")
    return base64.b64encode(struct.pack(f">{len(raw)}H", *raw)).decode("ascii")


def decode_packet(packet):
    return measurement.measurement_text(packet_text(packet), measurement.ENCODING)


def test_workflow_compression_round_trips_json_without_losing_values():
    value = {
        "run_nonce": "transport",
        "versions": [{"description": "a" * 128, "evidence": 'café 🐍\n"quoted"'}] * 4,
        "snapshots": [],
        "complete": True,
        "cap_hit": False,
        "cap": None,
        "count": 12,
        "score": 0.75,
    }
    plain = json.dumps(value, ensure_ascii=False)
    encoded = subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            "import {compressMeasurement} from './workflows/src/measurementCompression.js';"
            "let text = ''; for await (const chunk of process.stdin) text += chunk;"
            "process.stdout.write(compressMeasurement(text));",
        ],
        input=plain,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=ROOT,
        check=True,
        timeout=30,
    ).stdout
    assert measurement.measurement_text(plain) == plain
    assert (
        json.loads(measurement.measurement_text(encoded, measurement.ENCODING)) == value
    )


def test_unknown_encoding_is_rejected_before_decoding():
    with pytest.raises(ValueError, match="unknown measurement encoding"):
        measurement.measurement_text("not base64", "future-version")


def test_corrupt_compressed_stream_is_rejected():
    with pytest.raises(ValueError):
        measurement.measurement_text("!", measurement.ENCODING)
    with pytest.raises(ValueError, match="truncated compressed measurement"):
        measurement.measurement_text("AA==", measurement.ENCODING)
    invalid_code = base64.b64encode(struct.pack(">H", 256)).decode("ascii")
    with pytest.raises(ValueError, match="invalid compressed measurement code"):
        measurement.measurement_text(invalid_code, measurement.ENCODING)


def test_compressed_tree_requires_the_versioned_packet_shape():
    with pytest.raises(ValueError, match="invalid compressed measurement tree"):
        decode_packet({"root": None})
    with pytest.raises(ValueError, match="invalid compressed measurement strings"):
        decode_packet({"strings": [17], "root": None})


def test_tree_nodes_cannot_smuggle_unencoded_objects():
    with pytest.raises(ValueError, match="invalid compressed measurement node"):
        decode_packet({"strings": [], "root": {"injected": "value"}})


def test_string_references_must_resolve_to_the_pool():
    with pytest.raises(ValueError, match="invalid compressed measurement reference"):
        decode_packet({"strings": ["value"], "root": [0, 1]})


def test_duplicate_properties_are_rejected_instead_of_overwriting_evidence():
    with pytest.raises(ValueError, match="invalid compressed measurement property"):
        decode_packet(
            {"strings": [], "root": [2, [["verified", True], ["verified", False]]]}
        )


def test_decompression_is_bounded_before_parsing_the_tree(monkeypatch):
    monkeypatch.setattr(measurement, "MAX_TRACE_CHARS", 32)
    with pytest.raises(ValueError, match="decoded measurement exceeds safety bound"):
        decode_packet({"strings": ["a" * 64], "root": [0, 0]})


def test_pooled_references_cannot_expand_past_the_output_bound(monkeypatch):
    packet = {"strings": ["a" * 100], "root": [1, [[0, 0]] * 20]}
    monkeypatch.setattr(measurement, "MAX_TRACE_CHARS", 1024)
    with pytest.raises(ValueError, match="expanded measurement exceeds safety bound"):
        decode_packet(packet)
