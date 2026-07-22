"""`querygate-audit` CLI: verify a ledger and extract a receipt (item 91)."""

from __future__ import annotations

import json

import pytest

from querygate.audit_cli import _cmd_receipt, _cmd_verify, main
from querygate.audit.ledger import GENESIS_PREV_HASH, make_record


def _event(event_id: str) -> dict:
    return {
        "event_id": event_id,
        "event_type": "query.execution",
        "connection_id": "c1",
        "policy_decision": "allowed",
        "outcome": "success",
        "query_shape": {"from": "orders"},
        "duration_ms": 1,
    }


def _write_chain(path, events, *, key=None):
    prev = GENESIS_PREV_HASH
    lines = []
    for i, ev in enumerate(events):
        rec = make_record(i, prev, ev, key=key)
        lines.append(rec.model_dump_json())
        prev = rec.hash
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_verify_ok_exit_zero(tmp_path, capsys):
    ledger = tmp_path / "l.jsonl"
    _write_chain(ledger, [_event("a"), _event("b")])
    rc = _cmd_verify(_Args(ledger=str(ledger), hmac_key_env=None, expected_head=None))
    assert rc == 0
    assert "Ledger OK: 2 record(s)" in capsys.readouterr().out


def test_verify_detects_tamper_exit_one(tmp_path, capsys):
    ledger = tmp_path / "l.jsonl"
    lines = _write_chain(ledger, [_event("a"), _event("b")])
    d = json.loads(lines[1])
    d["event"]["connection_id"] = "evil"
    ledger.write_text(lines[0] + "\n" + json.dumps(d) + "\n", encoding="utf-8")
    rc = _cmd_verify(_Args(ledger=str(ledger), hmac_key_env=None, expected_head=None))
    assert rc == 1
    assert "Ledger BROKEN" in capsys.readouterr().err


def test_verify_missing_file_exit_two(tmp_path):
    rc = _cmd_verify(
        _Args(ledger=str(tmp_path / "nope.jsonl"), hmac_key_env=None, expected_head=None)
    )
    assert rc == 2


def test_verify_keyed_reads_key_from_env(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "l.jsonl"
    _write_chain(ledger, [_event("a")], key=b"topsecret")
    monkeypatch.setenv("QG_LEDGER_KEY", "topsecret")
    rc = _cmd_verify(_Args(ledger=str(ledger), hmac_key_env="QG_LEDGER_KEY", expected_head=None))
    assert rc == 0
    # Wrong key => broken.
    monkeypatch.setenv("QG_LEDGER_KEY", "wrong")
    rc = _cmd_verify(_Args(ledger=str(ledger), hmac_key_env="QG_LEDGER_KEY", expected_head=None))
    assert rc == 1


def test_verify_empty_key_env_errors(tmp_path, monkeypatch):
    ledger = tmp_path / "l.jsonl"
    _write_chain(ledger, [_event("a")])
    monkeypatch.delenv("QG_LEDGER_KEY", raising=False)
    with pytest.raises(SystemExit):
        _cmd_verify(_Args(ledger=str(ledger), hmac_key_env="QG_LEDGER_KEY", expected_head=None))


def test_receipt_prints_verifiable_json(tmp_path, capsys):
    ledger = tmp_path / "l.jsonl"
    _write_chain(ledger, [_event("a"), _event("b")])
    rc = _cmd_receipt(_Args(ledger=str(ledger), event_id="b", hmac_key_env=None))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "querygate.audit.receipt"
    assert payload["event"]["event_id"] == "b"


def test_receipt_unknown_event_exit_one(tmp_path):
    ledger = tmp_path / "l.jsonl"
    _write_chain(ledger, [_event("a")])
    rc = _cmd_receipt(_Args(ledger=str(ledger), event_id="zzz", hmac_key_env=None))
    assert rc == 1


def test_main_dispatches_verify(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "l.jsonl"
    _write_chain(ledger, [_event("a")])
    monkeypatch.setattr("sys.argv", ["querygate-audit", "verify", str(ledger)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 0
