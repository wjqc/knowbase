import hashlib
import json
import sqlite3

from knowbase import index,metrics,telemetry
from knowbase.__main__ import main
from knowbase.server import metrics_impl


def test_metrics_is_read_only_and_does_not_export_raw_context_or_labels(memory_repo,capsys):
    secret="PRIVATE_QUERY_BODY_MEMBER_PROJECT_7C2A"
    conn=index.connect(memory_repo)
    conn.execute("INSERT INTO usage_log VALUES(?,?,?,?,?)",("2026-10-22T08:00:00",secret,"search",secret,secret))
    conn.execute("INSERT INTO usage_log VALUES(?,?,?,?,?)",("2026-10-22T00:30:00Z",secret,secret,secret,secret))
    conn.execute("INSERT INTO usage_log VALUES(?,?,?,?,?)",("2026-10-22T09:00:00",secret,"search",secret,secret))
    conn.execute("INSERT INTO usage_log VALUES(?,?,?,?,?)",("invalid-date",secret,"search",secret,secret))
    conn.execute("INSERT INTO command_calls VALUES(?,?,?,?,?,?)",("private-id","2026-10-22T00:30:00Z",secret,secret,secret,0))
    conn.commit();conn.close()
    before=hashlib.sha256(index.db_path(memory_repo).read_bytes()).hexdigest()
    result=metrics.export(memory_repo,since="2026-10-22",until="2026-10-22T01:00:00Z",
                           naive_timezone="Asia/Shanghai",source_label="device-a")
    assert result["schema"]=="knowbase.metrics.v1"
    assert result["counts"]["legacy_usage_records"]=={"search":1,"other":1}
    assert result["counts"]["command_calls"]==[{"transport":"other","command":"other","status":"other","count":1}]
    assert result["invalid_timestamp_records"]["usage_log"]==1
    assert secret not in json.dumps(result)
    assert str(memory_repo) not in json.dumps(result)
    assert main(["metrics","--source-label","device-a","--naive-timezone","Asia/Shanghai"])==0
    assert secret not in capsys.readouterr().out
    assert "error" not in json.loads(metrics_impl())
    assert hashlib.sha256(index.db_path(memory_repo).read_bytes()).hexdigest()==before
    conn=index.connect(memory_repo)
    assert conn.execute("SELECT COUNT(*) FROM command_calls").fetchone()[0]==1
    conn.close()


def test_old_database_missing_tables_are_unknown_and_never_migrated(tmp_path):
    conn=sqlite3.connect(tmp_path/"memory.db")
    conn.execute("CREATE TABLE usage_log(ts,agent,tool,memory_id,detail)")
    conn.execute("INSERT INTO usage_log VALUES('2026-10-21T12:00:00Z','private','read','private','private')")
    conn.commit();conn.close()
    result=metrics.export(tmp_path)
    assert result["database_schema"] is None
    assert result["counts"]["legacy_usage_records"]=={"read":1}
    assert result["counts"]["command_calls"] is None
    assert result["counts"]["use_events"] is None
    conn=sqlite3.connect(tmp_path/"memory.db")
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}=={"usage_log"}
    conn.close()


def test_observed_mcp_metadata_includes_retries_and_errors_without_arguments(memory_repo):
    def invoke(value):
        if value=="raised-private": raise RuntimeError("private exception")
        return value
    observed=telemetry.observe(invoke,"memory_context")
    assert observed("private query") == "private query"
    assert observed('{"error":"private failure"}')
    import pytest
    with pytest.raises(RuntimeError): observed("raised-private")
    result=metrics.export(memory_repo)
    rows=result["counts"]["command_calls"]
    assert {row["status"]:row["count"] for row in rows}=={"ok":1,"returned_error":1,"raised":1}
    assert "private" not in json.dumps(result)
    assert result["counts"]["legacy_usage_records"]=={}


def test_actual_cli_hook_entry_records_event_without_payload(memory_repo,monkeypatch):
    from knowbase import hooks
    monkeypatch.setattr(hooks,"cmd_session_start",lambda **kwargs:None)
    assert main(["hook","session-start"])==0
    result=metrics.export(memory_repo)
    assert result["counts"]["command_calls"]==[{"transport":"cli","command":"hook.session-start","status":"ok","count":1}]


def test_metrics_rejects_invalid_input_and_missing_database(memory_repo,tmp_path):
    assert metrics.export(memory_repo,since="invalid")["error"]=="INVALID_METRICS_WINDOW"
    assert metrics.export(memory_repo,since="2026-10-23",until="2026-10-22")["error"]=="INVALID_METRICS_WINDOW"
    assert metrics.export(memory_repo,source_label="/private/path")["error"]=="INVALID_SOURCE_LABEL"
    assert metrics.export(memory_repo,naive_timezone="invalid/zone")["error"]=="INVALID_NAIVE_TIMEZONE"
    assert metrics.export(tmp_path)["error"]=="MEMORY_DB_NOT_FOUND"
