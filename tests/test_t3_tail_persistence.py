"""T3 有界尾持久化：宿主删除临时 transcript 后，后台采集仍可完成。

覆盖：
- 入队时捕获尾部 → 原件被删 → run_due 仍产出采集任务（不再 MATERIAL_PARSE_FAILED）；
- 无原件且无尾部 → 诚实失败（保持 MATERIAL_PARSE_FAILED）；
- 超大 transcript 尾部对齐完整行（不产破损 JSONL）；
- 入队同步读尾有界（8MB 会话毫秒级入队）。
"""

import io
import json
import time

from knowbase import config, hooks, index, materials
from knowbase.adapters import checkpoint_queue


def _stop_enqueue(monkeypatch, session_id, transcript_path):
    monkeypatch.setattr(hooks.sys, "stdin", io.StringIO(json.dumps({
        "session_id": session_id, "transcript_path": str(transcript_path)})))
    hooks.cmd_stop("zcode")


def _content_lines(text: str) -> str:
    return (json.dumps({"type": "tool_use", "name": "Bash", "input": {"command": "test"}}) + "\n"
            + json.dumps({"role": "user", "text": text}, ensure_ascii=False) + "\n")


def test_tail_survives_host_deleting_transcript(memory_repo, tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "gone.jsonl"
    transcript.write_text(_content_lines("业务规则：已结算订单禁止撤回，出处 PRD-T3 §1。"),
                          encoding="utf-8")
    _stop_enqueue(monkeypatch, "tail-s1", transcript)
    assert capsys.readouterr().out == ""
    transcript.unlink()  # 宿主在 Stop 返回后删除临时文件
    conn = index.connect(memory_repo)
    try:
        result = checkpoint_queue.run_due(memory_repo, conn, config.load_config(), lambda: True)
        assert result.get("processed") == 1, result
        checkpoint = result["results"][0]
        assert not checkpoint.get("error"), checkpoint
        assert checkpoint.get("job_ids"), checkpoint
        payload = materials.payload_for_job(conn, checkpoint["job_ids"][0])
        assert "已结算订单禁止撤回" in json.dumps(payload, ensure_ascii=False)
    finally:
        conn.close()


def test_missing_file_without_tail_fails_honestly(memory_repo, tmp_path, monkeypatch):
    """无尾持久化的旧格式任务：原件缺失时保持诚实失败，不伪造采集。"""
    from knowbase import jobs
    from datetime import datetime, timezone
    conn = index.connect(memory_repo)
    try:
        jobs.enqueue(conn, jobs.KIND_CHECKPOINT, "orders", {
            "session_id": "old-style", "transcript_path": str(tmp_path / "missing.jsonl"),
            "host_profile": "zcode", "project": "/tmp/orders", "legacy": True,
            "mid_session": False,
            "requested_at": datetime.now(timezone.utc).isoformat()})
        conn.commit()
        result = checkpoint_queue.run_due(memory_repo, conn, config.load_config(), lambda: True)
        assert result.get("processed") == 1
        assert result["results"][0].get("error") == "MATERIAL_PARSE_FAILED"
    finally:
        conn.close()


def test_zcode_stop_queues_each_distinct_final_reply(memory_repo, tmp_path, monkeypatch, capsys):
    replies = ("布尔开关不能直接用 bool 转换字符串。", "代理的 TLS 终止位置决定证书部署点。")
    conn = index.connect(memory_repo)
    try:
        conn.execute("INSERT INTO context_sessions(host_profile,session_id,scope,updated_at) "
                     "VALUES('zcode','zcode-two-turns','','2026-11-11')")
        conn.commit()
        payloads = []
        for i, text in enumerate((*replies, replies[0])):
            transcript = tmp_path / f"turn-{i}.jsonl"
            transcript.write_text(json.dumps({"message": {"role": "assistant", "content": [
                {"type": "text", "text": text}]}}, ensure_ascii=False) + "\n", encoding="utf-8")
            _stop_enqueue(monkeypatch, "zcode-two-turns", transcript)
            transcript.unlink()
            processed = checkpoint_queue.run_due(memory_repo, conn, config.load_config(), lambda: True)
            payloads.extend(materials.payload_for_job(conn, job_id)
                            for job_id in processed["results"][0]["job_ids"])
        assert capsys.readouterr() == ("", "")
        assert [p["segments"][0]["text"] for p in payloads] == list(replies)
        assert [(p["capture_range"]["start"], p["capture_range"]["end"]) for p in payloads] == [(0, 1), (1, 2)]
    finally:
        conn.close()


def test_oversized_tail_aligned_to_complete_lines(memory_repo, tmp_path, monkeypatch, capsys):
    transcript = tmp_path / "big.jsonl"
    with transcript.open("w", encoding="utf-8") as out:
        out.write('{"type":"tool_use","name":"Bash","input":{"command":"warm"}}\n')
        out.write(json.dumps({"role": "assistant", "text": "x" * 400_000}) + "\n")
        # 尾部窗口（最后 256KB）内须仍有 tool_use 与结论行：
        out.write('{"type":"tool_use","name":"Bash","input":{"command":"late"}}\n')
        out.write(json.dumps({"role": "user", "text": "业务规则：T3 尾部对齐样本，出处 PRD-T3 §2。"},
                             ensure_ascii=False) + "\n")
    assert transcript.stat().st_size > checkpoint_queue.TAIL_BYTES
    _stop_enqueue(monkeypatch, "tail-big", transcript)
    assert capsys.readouterr().out == ""
    transcript.unlink()
    conn = index.connect(memory_repo)
    try:
        spool = conn.execute(
            "SELECT s.input_json FROM job_spool s JOIN jobs j ON j.job_id=s.job_id "
            "WHERE j.kind='capture_checkpoint'").fetchone()[0]
        tail = json.loads(spool)["transcript_tail"]
        assert len(tail.encode("utf-8")) <= checkpoint_queue.TAIL_BYTES
        assert tail.startswith("{"), "尾部必须对齐到完整行首"
        result = checkpoint_queue.run_due(memory_repo, conn, config.load_config(), lambda: True)
        checkpoint = result["results"][0]
        assert not checkpoint.get("error"), checkpoint
        assert checkpoint.get("job_ids"), checkpoint
    finally:
        conn.close()


def test_enqueue_tail_read_is_bounded(memory_repo, tmp_path, monkeypatch):
    transcript = tmp_path / "huge.jsonl"
    with transcript.open("w", encoding="utf-8") as out:
        out.write('{"type":"tool_use","name":"Bash","input":{"command":"test"}}\n')
        for i in range(11):
            out.write(json.dumps({"role": "assistant", "text": chr(65 + i) * 900_000}) + "\n")
    assert transcript.stat().st_size > 8 * 1024 * 1024
    start = time.monotonic()
    _stop_enqueue(monkeypatch, "tail-perf", transcript)
    elapsed = time.monotonic() - start
    assert elapsed < 2.0, f"Stop 入队耗时 {elapsed:.2f}s，超出有界预算"
    conn = index.connect(memory_repo)
    try:
        spool = conn.execute(
            "SELECT s.input_json FROM job_spool s JOIN jobs j ON j.job_id=s.job_id "
            "WHERE j.kind='capture_checkpoint'").fetchone()[0]
        tail = json.loads(spool)["transcript_tail"]
        assert len(tail.encode("utf-8")) <= checkpoint_queue.TAIL_BYTES
    finally:
        conn.close()
