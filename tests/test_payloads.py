"""Tests for the store-payload archive (library/payloads.py, 0.20.0)."""

import json

import pytest

from athenaeum.library.payloads import PAYLOAD_DIR, PayloadStore


def make_payload(
    request_id: str,
    *,
    outcome: str = "ok",
    received_at: str = "2026-08-01T00:00:00+00:00",
    content: str = "c",
) -> dict:
    return {
        "request_id": request_id,
        "tool": "store_knowledge",
        "user_id": "user-1",
        "agent_label": "agent-a",
        "trace_id": "20260801T000000Z-deadbeef",
        "received_at": received_at,
        "outcome": outcome,
        "error": None,
        "params": {
            "content": content,
            "kind_hint": None,
            "relates_to": None,
            "topic_hint": None,
            "images": [],
        },
        "stored": [],
    }


def test_create_and_read_roundtrip(tmp_path):
    store = PayloadStore(tmp_path)
    payload = make_payload("20260801T000000Z-aaaa1111")
    assert store.create(payload) == "20260801T000000Z-aaaa1111"
    path = tmp_path / PAYLOAD_DIR / "20260801T000000Z-aaaa1111.json"
    assert json.loads(path.read_text(encoding="utf-8")) == payload
    assert store.read("20260801T000000Z-aaaa1111") == payload


def test_create_overwrites_by_id(tmp_path):
    """Two-phase archiving: the exit rewrite replaces the received record."""
    store = PayloadStore(tmp_path)
    store.create(make_payload("20260801T000000Z-aaaa1111", outcome="received"))
    store.create(make_payload("20260801T000000Z-aaaa1111", outcome="ok"))
    assert store.read("20260801T000000Z-aaaa1111")["outcome"] == "ok"
    assert len(list((tmp_path / PAYLOAD_DIR).glob("*.json"))) == 1


def test_create_rejects_bad_ids(tmp_path):
    store = PayloadStore(tmp_path)
    with pytest.raises(ValueError):
        store.create(make_payload("../escape"))
    with pytest.raises(ValueError):
        store.create(make_payload(""))
    with pytest.raises(ValueError):
        store.create(make_payload("with/slash"))


def test_read_guards_bad_ids_and_missing(tmp_path):
    store = PayloadStore(tmp_path)
    with pytest.raises(ValueError):
        store.read("../escape")
    with pytest.raises(FileNotFoundError):
        store.read("20260801T000000Z-aaaa1111")


def test_list_summaries_newest_first_without_params(tmp_path):
    store = PayloadStore(tmp_path)
    store.create(make_payload("20260801T000000Z-aaaa1111"))
    store.create(make_payload("20260802T000000Z-bbbb2222", outcome="error"))
    summaries = store.list()
    assert [s["request_id"] for s in summaries] == [
        "20260802T000000Z-bbbb2222",
        "20260801T000000Z-aaaa1111",
    ]
    assert summaries[0]["outcome"] == "error"
    assert "params" not in summaries[0]  # summaries carry no payload body
    assert "stored" not in summaries[0]


def test_list_skips_unparseable_files(tmp_path):
    store = PayloadStore(tmp_path)
    store.create(make_payload("20260801T000000Z-aaaa1111"))
    (tmp_path / PAYLOAD_DIR / "broken.json").write_text("{not json", encoding="utf-8")
    assert [s["request_id"] for s in store.list()] == ["20260801T000000Z-aaaa1111"]


def test_prune_keeps_newest_on_create(tmp_path):
    store = PayloadStore(tmp_path, keep=2)
    for day in range(1, 5):
        store.create(make_payload(f"2026080{day}T000000Z-aaaa111{day}"))
    remaining = sorted(p.stem for p in (tmp_path / PAYLOAD_DIR).glob("*.json"))
    assert remaining == ["20260803T000000Z-aaaa1113", "20260804T000000Z-aaaa1114"]
    assert store.prune(1) == 1
    assert [p.stem for p in (tmp_path / PAYLOAD_DIR).glob("*.json")] == [
        "20260804T000000Z-aaaa1114"
    ]


def test_keep_zero_never_prunes(tmp_path):
    store = PayloadStore(tmp_path, keep=0)
    for day in range(1, 4):
        store.create(make_payload(f"2026080{day}T000000Z-aaaa111{day}"))
    assert len(list((tmp_path / PAYLOAD_DIR).glob("*.json"))) == 3


def test_since_filters_by_received_at_inclusive(tmp_path):
    store = PayloadStore(tmp_path)
    store.create(make_payload("20260801T000000Z-aaaa1111", received_at="2026-08-01T00:00:00+00:00"))
    store.create(make_payload("20260802T000000Z-bbbb2222", received_at="2026-08-02T00:00:00+00:00"))
    # None baseline: all retained payloads, newest first
    assert [r["request_id"] for r in store.since(None)] == [
        "20260802T000000Z-bbbb2222",
        "20260801T000000Z-aaaa1111",
    ]
    assert [r["request_id"] for r in store.since("2026-08-02T00:00:00+00:00")] == [
        "20260802T000000Z-bbbb2222"
    ]
    # the boundary is inclusive (received_at >= ts)
    assert [r["request_id"] for r in store.since("2026-08-01T00:00:00+00:00")] == [
        "20260802T000000Z-bbbb2222",
        "20260801T000000Z-aaaa1111",
    ]


def test_missing_store_dir_reads_empty(tmp_path):
    store = PayloadStore(tmp_path)
    assert store.list() == []
    assert store.since(None) == []
    assert store.prune(5) == 0
