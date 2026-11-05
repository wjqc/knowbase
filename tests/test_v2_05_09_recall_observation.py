"""V2-05 演进检索 + V2-09 宿主观测：域包含 staging/stale、快照冻结与漂移、
原生事件幂等/配对/gap、任务身份 unknown 显式。"""
import json

from knowbase import index, store, writer
from knowbase.adapters.normalizer import normalize_event, normalize_events
from knowbase.knowledge_lifecycle import observation, recall
from knowbase.migrations import apply_all

BODY = ("## 结论\nCRLF 读视图不一致会导致来源核验失败。\n## 解决的问题\n跨平台协作。\n"
        "## 适用条件\nWindows 编辑器保存。\n## 不适用条件\n纯 LF 仓库。\n"
        "## 可执行动作\n统一 read_text 归一。\n## 关键证据\nPR-7 复现。\n"
        "## 验证情况\nmacOS/Windows 双机验证。\n## 未知与待确认\n无。\n")
BODY2 = ("## 结论\nHTTP 502 需分层定位。\n## 解决的问题\n网关排障。\n"
         "## 适用条件\n数智平台 /dde/dataarts。\n## 不适用条件\n本地开发。\n"
         "## 可执行动作\n按 hop 二分。\n## 关键证据\n对照实验锁定 hop。\n"
         "## 验证情况\n生产复现。\n## 未知与待确认\nDNS 细节。\n")


def seed_repo(repo):
    active = writer.create(repo, "pitfall", "CRLF 行尾读视图不一致", BODY, scope="orders")
    other = writer.create(repo, "pitfall", "HTTP 502 分层定位", BODY2, scope="orders")
    # 邻近主题负例：不同 scope，不应被演进检索召回。
    writer.create(repo, "pitfall", "CRLF 行尾问题", BODY, scope="other-proj")
    # stale 卡必须参与演进检索：把 active 卡置 stale。
    conn = index.connect(repo)
    meta, body, path = store.load(repo, active["entity_id"])
    meta["status"] = "stale"
    path.write_text(store.render(meta, body), encoding="utf-8")
    index.upsert(conn, *store.load(repo, active["entity_id"]), commit=False)
    conn.commit()
    conn.close()
    return active["entity_id"], other["entity_id"]


def test_evolution_recall_includes_stale_and_excludes_foreign_scope(memory_repo):
    mid_stale, mid_502 = seed_repo(memory_repo)
    conn = index.connect(memory_repo)
    try:
        result = recall.evolution_recall(conn, "CRLF 行尾 读视图", "orders")
        assert not result.get("error")
        ids = {c["id"] for c in result["candidates"]}
        assert mid_stale in ids  # stale 参与演进检索（任务检索会过滤）
        foreign = [c for c in result["candidates"] if c["scope"] == "other-proj"]
        assert foreign == []  # 越scope 0
        snap = result["snapshot"]
        assert snap["purpose"] == "evolution"
        assert snap["fused_limit"] == 30 and snap["route_limit"] == 50
        assert "lexical_only" in result["degraded"]
        # 快照未漂移；语料变化后检出。
        assert recall.snapshot_changed(conn, snap) is False
        writer.create(memory_repo, "pitfall", "行尾混排源核验补遗", BODY + "新增。\n",
                      scope="orders")
        conn2 = index.connect(memory_repo)
        try:
            assert recall.snapshot_changed(conn2, snap) is True
        finally:
            conn2.close()
    finally:
        conn.close()


def test_evolution_recall_error_code_and_adjacency_routes(memory_repo):
    mid_stale, mid_502 = seed_repo(memory_repo)
    # 给 502 卡加 relates 指向 stale 卡，验证邻接路。
    conn = index.connect(memory_repo)
    meta, body, path = store.load(memory_repo, mid_502)
    meta["relations"] = [{"id": mid_stale, "type": "related"}]
    path.write_text(store.render(meta, body), encoding="utf-8")
    index.upsert(conn, *store.load(memory_repo, mid_502), commit=False)
    conn.commit()
    conn.close()
    conn = index.connect(memory_repo)
    try:
        result = recall.evolution_recall(conn, "网关 502 排查", "orders")
        ids = {c["id"] for c in result["candidates"]}
        assert mid_502 in ids
        # 邻接：通过 relations 把 stale 卡带进候选（即使词法未直接命中）。
        assert mid_stale in ids
        hit_502 = next(c for c in result["candidates"] if c["id"] == mid_502)
        assert "error_code_exact" in hit_502["channels"] or "fts" in hit_502["channels"]
        for c in result["candidates"]:
            assert c["content_revision"]  # 版本绑定
    finally:
        conn.close()


# ---------- V2-09 ----------

def test_native_ingest_idempotent_and_pairing(memory_repo):
    conn = index.connect(memory_repo)
    apply_all(conn)
    events = [
        {"native_key": "ne-1", "kind": "tool_call", "tool_call_id": "tc-1",
         "tool_name": "Bash", "payload": {"command_hash": "a"}},
        {"native_key": "ne-2", "kind": "tool_call", "tool_call_id": "tc-2",
         "tool_name": "Edit", "payload": {"file": "x.py"}, "artifact_hash": "h1"},
        {"native_key": "ne-3", "kind": "tool_result", "tool_call_id": "tc-1",
         "payload": {"exit_code": 0}},
        {"native_key": "ne-4", "kind": "task_end",
         "payload": {"completion": "completed"}},
    ]
    first = observation.ingest(conn, "zcode", "s-1", events, project_key="repo:knowbase",
                               native_task_key="task-42", native_run_key="run-42",
                               environment_fingerprint="env-1")
    assert first["inserted"] == 4 and first["idempotent"] == 0
    assert first["paired"] == 1
    assert first["gap_flags"] == ["unpaired_tool_calls"]  # tc-2 无 result
    assert first["identity_basis"] == "native"
    assert first["completion"] == "completed"
    assert first["artifact_set_hash"]
    # 同批重放：零增量、同 run。
    again = observation.ingest(conn, "zcode", "s-1", events, project_key="repo:knowbase",
                               native_task_key="task-42", native_run_key="run-42")
    assert again["inserted"] == 0 and again["idempotent"] == 4
    assert again["run_id"] == first["run_id"]
    assert conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0] == 1
    # 同 native_key 改 payload：冲突计数，不覆盖。
    tampered = [{**events[0], "payload": {"command_hash": "changed"}}]
    third = observation.ingest(conn, "zcode", "s-1", tampered, project_key="repo:knowbase")
    assert third["conflicts"] == 1
    stored = conn.execute("SELECT payload_hash FROM native_events WHERE native_key='ne-1'"
                          ).fetchone()[0]
    conn.close()
    # 读回。
    conn = index.connect(memory_repo)
    summary = observation.run_summary(conn, first["run_id"])
    conn.close()
    assert summary["identity_basis"] == "native"
    assert summary["logical_task_key"] == "task:repo:knowbase:task-42"
    assert "unpaired_tool_calls" in summary["gap_flags"]


def test_native_ingest_without_task_key_is_local_segment(memory_repo):
    conn = index.connect(memory_repo)
    apply_all(conn)
    result = observation.ingest(conn, "claude", "s-2",
                                [{"native_key": "n1", "kind": "note", "payload": {}}],
                                project_key="repo:x")
    assert result["identity_basis"] == "local_segment"
    assert result["logical_task_key"].startswith("unknown:")
    conn.close()
    conn = index.connect(memory_repo)
    summary = observation.run_summary(conn, result["run_id"])
    conn.close()
    assert summary["identity_basis"] == "local_segment"


def test_two_tasks_same_session_do_not_merge(memory_repo):
    conn = index.connect(memory_repo)
    apply_all(conn)
    a = observation.ingest(conn, "zcode", "s-3",
                           [{"native_key": "a-1", "kind": "note", "payload": {}}],
                           project_key="repo:kb", native_task_key="task-A",
                           native_run_key="run-A")
    b = observation.ingest(conn, "zcode", "s-3",
                           [{"native_key": "b-1", "kind": "note", "payload": {}}],
                           project_key="repo:kb", native_task_key="task-B",
                           native_run_key="run-B")
    assert a["run_id"] != b["run_id"]
    assert a["logical_task_key"] != b["logical_task_key"]
    conn.close()


def test_normalizer_preserves_each_tool_call_id():
    """复合 content 的多 tool_use 不再被压成一个（V2-09 配对保真）。"""
    raw = {"role": "assistant", "id": "msg-1", "content": [
        {"type": "text", "text": "running two tools"},
        {"type": "tool_use", "id": "call-A", "name": "Bash", "input": {"x": 1}},
        {"type": "tool_use", "id": "call-B", "name": "Read", "input": {"y": 2}},
    ]}
    events = normalize_events(raw)
    tool_events = [e for e in events if e.tool_call_id]
    assert {e.tool_call_id for e in tool_events} == {"call-A", "call-B"}
    assert {e.tool_name for e in tool_events} == {"Bash", "Read"}
    # 兼容包装：单事件路径仍可用（取 base）。
    single = normalize_event(raw)
    assert single.role == "assistant"
    # tool_result 复合块同样逐项保留。
    raw2 = {"role": "user", "id": "msg-2", "content": [
        {"type": "tool_result", "tool_use_id": "call-A", "content": "ok"},
        {"type": "tool_result", "tool_use_id": "call-B", "content": "fine"},
    ]}
    results = [e for e in normalize_events(raw2) if e.tool_call_id]
    assert {e.tool_call_id: e.tool_result for e in results} == {"call-A": "ok", "call-B": "fine"}
