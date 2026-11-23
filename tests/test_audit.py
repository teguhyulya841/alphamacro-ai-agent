import json

import pytest

from alphamacro.audit import GENESIS_HASH, AuditLog, main, sha256_text


def make_log(tmp_path, **kw):
    return AuditLog(tmp_path / "a" / "audit.jsonl", salt="salt-value", **kw)


def test_chain_links_and_verifies(tmp_path):
    log = make_log(tmp_path)
    first = log.record({"request_id": "r1", "status": "ok"})
    second = log.record({"request_id": "r2", "status": "blocked"})
    assert first["prev_hash"] == GENESIS_HASH
    assert second["prev_hash"] == first["hash"]
    result = AuditLog.verify(tmp_path / "a" / "audit.jsonl")
    assert result.ok and result.records == 2 and result.head_hash == second["hash"]


def test_modified_record_is_detected(tmp_path):
    log = make_log(tmp_path)
    for i in range(3):
        log.record({"request_id": f"r{i}", "status": "ok"})
    path = tmp_path / "a" / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    record = json.loads(lines[1])
    record["status"] = "tampered"
    lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = AuditLog.verify(path)
    assert not result.ok and "modified" in result.error


def test_deleted_middle_record_is_detected(tmp_path):
    log = make_log(tmp_path)
    for i in range(3):
        log.record({"request_id": f"r{i}"})
    path = tmp_path / "a" / "audit.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    assert not AuditLog.verify(path).ok


def test_resumes_chain_after_restart(tmp_path):
    make_log(tmp_path).record({"request_id": "r1"})
    reopened = make_log(tmp_path)
    reopened.record({"request_id": "r2"})
    result = AuditLog.verify(tmp_path / "a" / "audit.jsonl")
    assert result.ok and result.records == 2


def test_refuses_to_open_corrupted_log(tmp_path):
    log = make_log(tmp_path)
    log.record({"request_id": "r1"})
    path = tmp_path / "a" / "audit.jsonl"
    path.write_text(path.read_text(encoding="utf-8").replace("r1", "rX"), encoding="utf-8")
    with pytest.raises(ValueError):
        make_log(tmp_path)


def test_pseudonymization_is_stable_salted_and_not_plain(tmp_path):
    a = make_log(tmp_path)
    assert a.pseudonymize(42) == a.pseudonymize("42")
    assert a.pseudonymize(42) != a.pseudonymize(43)
    assert a.pseudonymize(42) != "42"
    other = AuditLog(tmp_path / "b.jsonl", salt="different")
    assert other.pseudonymize(42) != a.pseudonymize(42)


def test_cli_verify(tmp_path, capsys):
    log = make_log(tmp_path)
    log.record({"request_id": "r1", "query_sha256": sha256_text("x")})
    path = str(tmp_path / "a" / "audit.jsonl")
    assert main(["verify", path]) == 0
    assert "Head hash" in capsys.readouterr().out
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("garbage\n")
    assert main(["verify", path]) == 1
