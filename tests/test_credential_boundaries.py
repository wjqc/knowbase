"""Synthetic credentials only: argv, nested preview, write and delivery boundaries."""

import json

import pytest

from knowbase import context, index, materials, store
from knowbase.adapters import host_events
from knowbase.adapters.normalizer import normalize_event
from knowbase.extraction.sanitizer import has_credentials, sanitize, sanitize_value


@pytest.mark.parametrize("flag", ["--api-key", "--api_key", "--app-secret", "--token", "-s"])
@pytest.mark.parametrize("depth", range(5))
def test_argv_secret_at_every_json_depth(flag, depth):
    secret = "synthetic-opaque-value-192837465"
    value = {"args": ["-a", "application-id", flag, secret], "tail": "keep"}
    for _ in range(depth):
        value = json.dumps(value)
    clean = sanitize_value(value)
    assert secret not in str(clean)
    assert "application-id" in str(clean)
    assert "keep" in str(clean)
    assert store.find_secrets(value)
    assert store.find_secrets(clean) == []
    assert sanitize_value(clean) == clean


@pytest.mark.parametrize("key", ["FIGMA_API_KEY", "apiKey", "secret_token", "app_secret", "Authorization"])
@pytest.mark.parametrize("depth", range(5))
def test_same_secret_across_structured_and_preview_copies(key, depth):
    secret = "synthetic-gateway-value-192837465"
    response = {"env": {key: secret}, "valid_config": 900000}
    preview = response
    for _ in range(depth):
        preview = json.dumps(preview)
    value = {"toolResponse": response, "toolResultPreview": preview, "tool_response": response}
    clean = sanitize_value(value)
    assert secret not in json.dumps(clean)
    assert "toolResultPreview" in clean
    assert "valid_config" in json.dumps(clean)
    assert not has_credentials(json.dumps(clean))


@pytest.mark.parametrize("text", [
    "model_auto_compact_token_limit = 900000", "context_token_budget: 900000",
    "max_tokens = 900000", "token_count: 900000", "api_key_timeout = 900000",
    'model_auto_compact_token 上限 = 900000',
])
def test_token_configuration_is_not_a_credential(text):
    assert sanitize(text) == text
    assert not store.find_secrets(text)


@pytest.mark.parametrize("text", [
    'run --api-key "synthetic-key-with-quotes-123456" --port 8080',
    "run -s synthetic-short-option-123456 --port 8080",
    'config: ' + json.dumps(json.dumps({"API_KEY": "synthetic-escaped-value-123456"})),
    "token=123456789", "figd_SYNTHETIC12345678901234567890",
    "sk-trae-agent-SYNTHETIC12345678901234567890",
    'run --api-key "synthetic multi word secret" --port 8080',
    '[REDACTED:credential] api_key=synthetic-trailing-secret-123456789',
])
def test_unstructured_text_and_prefix_fallback(text):
    assert store.find_secrets(text)
    clean = sanitize(text)
    assert "123456" not in clean
    assert "multi word secret" not in clean
    assert not store.find_secrets(clean)


def test_write_boundary_for_direct_context_and_hook(memory_repo, tmp_path):
    from knowbase import config
    cfg = json.loads(config.CONFIG_PATH.read_text())
    cfg["inflight"] = {"enabled": True, "capture_enabled": False}
    cfg["scope_map"] = {str(tmp_path): "orders"}
    config.CONFIG_PATH.write_text(json.dumps(cfg))
    secret = "synthetic-persisted-secret-123456789"
    observation = {"payload": {"args": ["-s", secret], "toolResultPreview": json.dumps({"apiKey": secret})}}
    context.execute(memory_repo, session_id="direct", event_id="write", scope="orders", observation=observation)
    host_events.handle({"session_id": "hook", "cwd": str(tmp_path), **observation}, event="tool-after", style="zcode")
    conn = index.connect(memory_repo)
    rows = list(conn.execute("SELECT observation_json FROM context_events"))
    assert len(rows) == 2
    assert all(secret not in row[0] and "REDACTED:credential" in row[0] for row in rows)
    conn.close()


def test_capture_sanitizes_before_segmentation():
    secret = "synthetic-api-key-123456789"
    text = json.dumps({"args": ["--api-key", secret], "tail": "safe"})
    payload = materials.capture_payload([normalize_event({"role": "tool", "text": text})], scope="orders", session_id="s")
    assert secret not in json.dumps(payload)
    assert payload["segments"][0]["text_hash"] == materials.digest(payload["segments"][0]["text"])


def test_rejection_messages_do_not_echo_credentials():
    secret = "synthetic-validation-secret-123456789"
    assert store.find_secrets({"apiKey": secret}) == ["[REDACTED:credential]"]


def _legacy_dirty_job(memory_repo):
    """Inject old persisted bytes, including a secret split between two segments."""
    import hashlib
    from knowbase import jobs
    secret = "synthetic-historical-opaque-987654321"
    text = json.dumps({"args": ["-s", secret], "tail": "original-tail"})
    payload = materials.capture_payload([normalize_event({"role": "user", "text": "safe"})], scope="orders", session_id="s")
    conn = index.connect(memory_repo)
    queued = jobs.enqueue(conn, jobs.KIND_CAPTURE, "orders", payload)
    job_id = queued["job_id"]
    cut = text.index(secret) + 9
    payload["checkpoint"]["events"][0]["text"] = text
    template = payload["segments"][0]
    payload["segments"] = [{**template, "segment_id": f"seg-{i:04d}", "ordinal": i, "text": part,
                            "text_hash": materials.digest(part), "start_offset": start, "end_offset": start + len(part),
                            "original_length": len(text)} for i, (start, part) in enumerate([(0, text[:cut]), (cut, text[cut:])])]
    payload["total_segments"] = 2
    raw = json.dumps(payload, ensure_ascii=False)
    conn.execute("UPDATE job_spool SET input_json=?,checksum=? WHERE job_id=?",
                 (raw, hashlib.sha256(raw.encode()).hexdigest()[:16], job_id))
    materials.register_manifest(conn, job_id, payload)
    jobs.set_preparing(conn, job_id)
    jobs.set_awaiting(conn, job_id, 2)
    # An already delivered old batch must be revoked by the scrub.
    conn.execute("UPDATE jobs SET state='claimed',lease_owner='host:old-token',lease_until='2099-01-01T00:00:00+00:00',fencing_token=4 WHERE job_id=?", (job_id,))
    conn.execute("INSERT INTO job_batches(batch_id,job_id,input_version,manifest_hash,segment_ids_json,owner,lease_token,fencing_token,expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                 ("old-batch", job_id, payload["input_version"], materials.digest(payload["segments"]),
                  '["seg-0000","seg-0001"]', "host", "old-token", 4, "2099-01-01T00:00:00+00:00"))
    conn.commit()
    return conn, job_id, secret


def test_legacy_delivery_fails_closed_and_scrub_rebuilds_integrity(memory_repo, tmp_path):
    import sqlite3
    from knowbase import jobs
    from knowbase.privacy import scrub
    from knowbase.server import job_material_impl
    conn, job_id, secret = _legacy_dirty_job(memory_repo)
    with pytest.raises(ValueError, match="MATERIAL_REDACTION_REQUIRED"):
        materials.payload_for_job(conn, job_id)
    response = json.loads(job_material_impl(job_id, "old-batch", "old-token", fencing_token=4))
    assert response["error"] == "MATERIAL_REDACTION_REQUIRED"
    before = conn.execute("SELECT input_json FROM job_spool").fetchone()[0]
    assert scrub(memory_repo)["affected_jobs"] == 1
    assert conn.execute("SELECT input_json FROM job_spool").fetchone()[0] == before
    backup = tmp_path / "private-backup"
    result = scrub(memory_repo, apply=True, backup_dir=backup)
    assert result["remaining_changed_fields"] == {}
    assert result["integrity"] == "ok"
    assert backup.stat().st_mode & 0o777 == 0o700
    assert (backup / "memory.db").stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup / "memory.db") as saved:
        assert saved.execute("SELECT input_json FROM job_spool").fetchone()[0] == before
    clean = materials.payload_for_job(conn, job_id)
    assert secret not in json.dumps(clean)
    assert "original-tail" in json.dumps(clean)
    assert len(clean["segments"]) == 2
    for segment in clean["segments"]:
        assert segment["text_hash"] == materials.digest(segment["text"])
        assert conn.execute("SELECT text_hash FROM job_segments WHERE job_id=? AND segment_id=?",
                            (job_id, segment["segment_id"])).fetchone()[0] == segment["text_hash"]
    assert materials.validate_batch(conn, job_id, "old-batch", "old-token", 4)["error"] == "LEASE_EXPIRED"
    claim = jobs.claim(conn, job_id, "new-host")
    assert claim["fencing_token"] == 6
    assert secret not in json.dumps(claim)
    assert scrub(memory_repo)["total_changed_fields"] == 0
    conn.close()


def test_enqueue_never_persists_raw_credentials(memory_repo):
    from knowbase import jobs
    conn = index.connect(memory_repo)
    secret = "synthetic-enqueue-raw-secret-123456789"
    result = jobs.enqueue(conn, jobs.KIND_CAPTURE, "orders", {"text": json.dumps({"args": ["--api-key", secret]})})
    assert secret not in conn.execute("SELECT input_json FROM job_spool WHERE job_id=?", (result["job_id"],)).fetchone()[0]
    assert secret not in json.dumps(materials.payload_for_job(conn, result["job_id"]))
    conn.close()


def test_dry_run_cli_does_not_write_telemetry_or_database(memory_repo):
    import os
    import subprocess
    import sys
    conn = index.connect(memory_repo)
    before = list(conn.iterdump())
    result = subprocess.run([sys.executable, "-m", "knowbase", "privacy-scrub"],
                            env=os.environ.copy(), text=True, capture_output=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout)["mode"] == "dry_run"
    assert list(conn.iterdump()) == before
    conn.close()
