"""B 轻量路线（同卡并发）回归测试：

场景：两机离线各改同一张卡再合流。
- 真冲突（同行改动）：rebase 不得整个 abort，远端版进工作树、本地版进隔离区；
- 静默 auto-merge（不同部位改动）：合流后检测必须能判定"两边都改了同一张卡"；
- 写提交必须附带 base content_revision 元数据；
- 极端双崩溃：同一张卡两条 pending intent 时，恢复不得凭修订号猜测归属。
"""
import json
import subprocess
from pathlib import Path

import pytest

from knowbase import commit_adapter, config, gitops, index, quarantine, store
from knowbase.__main__ import cmd_init
from knowbase.mutations import _compute_revision


# ---------- 通用 git 助手 ----------

def git(repo, *args, check=True):
    r = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=kb-test",
         "-c", "user.email=kb-test@example.local", *args],
        capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {r.stderr}")
    return r


def card_bytes(mid, title, body, scope="test"):
    meta = store.new_meta("pitfall", title, scope, [], "agent:pytest:adhoc")
    meta["id"] = mid
    return store.render(meta, body)


def card_path(repo, mid):
    return Path(repo) / "pitfalls" / f"{mid}.md"


@pytest.fixture()
def write_repo(tmp_path, monkeypatch):
    """带 git 提交的记忆库（writer 路径用）。"""
    repo = tmp_path / "memory"
    cfg_path = tmp_path / "knowbase.json"
    cfg_path.write_text(json.dumps(config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo), "knowledge_path": "",
        "git": {"auto_commit": True, "auto_push": False, "auto_pull": False},
    })))
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_CONFIG", str(cfg_path))
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-broute")
    assert cmd_init() == 0
    git(repo, "config", "user.name", "kb-test")
    git(repo, "config", "user.email", "kb-test@example.local")
    return repo


@pytest.fixture()
def two_clones(tmp_path):
    """base 远端 + A/B 两个克隆，均含同一张卡；返回 (remote, a, b, mid)。"""
    mid = "P-abcdef123456"
    base = tmp_path / "base"
    base.mkdir()
    git(base, "init", "-q", "-b", "main")
    (base / "pitfalls").mkdir()
    (base / "pitfalls" / f"{mid}.md").write_text(
        card_bytes(mid, "双机同卡", "## 结论\n原始版本。\n\n## 解决的问题\n占位。\n\n"
                      "## 适用条件\n通用。\n\n## 不适用条件\n无。\n\n"
                      "## 可执行动作\n无。\n\n## 关键证据\n无。\n\n"
                      "## 验证情况\n无。\n\n## 未知与待确认\n无。\n"),
        encoding="utf-8")
    (base / "other.txt").write_text("other base\n")
    git(base, "add", "-A")
    git(base, "commit", "-qm", "init")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(base), str(remote)],
                   check=True, capture_output=True, text=True)
    a, b = tmp_path / "a", tmp_path / "b"
    subprocess.run(["git", "clone", "-q", str(remote), str(a)],
                   check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", "-q", str(remote), str(b)],
                   check=True, capture_output=True, text=True)
    return remote, a, b, mid


def edit_card(repo, mid, old, new):
    p = card_path(repo, mid)
    text = p.read_text(encoding="utf-8")
    assert old in text
    p.write_text(text.replace(old, new), encoding="utf-8")


# ---------- B-(1) 写提交附带 base content_revision ----------

CARD_BODY = ("## 结论\n第一版。\n\n## 解决的问题\n占位。\n\n## 适用条件\n通用。\n\n"
             "## 不适用条件\n无。\n\n## 可执行动作\n无。\n\n## 关键证据\n无。\n\n"
             "## 验证情况\n无。\n\n## 未知与待确认\n无。\n")


def test_write_commits_carry_base_revision_trailer(write_repo):
    from knowbase import writer
    result = writer.create(write_repo, "pitfall", "基准卡", CARD_BODY,
                           scope="test", push=False)
    assert not result.get("error"), result
    mid = result["entity_id"]
    msg = git(write_repo, "log", "-1", "--format=%B").stdout
    assert f"Knowbase-Base: {mid}=none" in msg
    meta, body, _ = store.load(write_repo, mid)
    prior = _compute_revision(meta, body)
    result = writer.update(write_repo, mid, body=CARD_BODY.replace("第一版", "第二版"),
                           push=False)
    assert not result.get("error"), result
    msg = git(write_repo, "log", "-1", "--format=%B").stdout
    assert f"Knowbase-Base: {mid}={prior}" in msg


# ---------- B-(2)/(3) 真冲突：按文件隔离，不整个 abort ----------

def test_same_card_conflict_isolated_not_aborted(two_clones):
    _remote, a, b, mid = two_clones
    # B 先改同一行并推送；A 离线改同一行。
    edit_card(b, mid, "原始版本", "B 机版本")
    git(b, "add", "-A")
    git(b, "commit", "-qm", "b edits card")
    (b / "other.txt").write_text("other remote\n")
    git(b, "add", "-A")
    git(b, "commit", "-qm", "b edits other")
    git(b, "push", "-q", "origin", "main")
    edit_card(a, mid, "原始版本", "A 机版本")
    git(a, "add", "-A")
    git(a, "commit", "-qm", "a edits card")
    git(a, "fetch", "-q", "origin")

    changed, push_needed, conflicts = gitops.integrate_with_conflicts(a, "origin", "main")
    assert changed
    # 未留下半截 rebase
    assert not (a / ".git" / "rebase-merge").exists()
    assert not (a / ".git" / "rebase-apply").exists()
    # 远端版进工作树
    assert "B 机版本" in card_path(a, mid).read_text(encoding="utf-8")
    # 其余卡/文件继续同步
    assert (a / "other.txt").read_text() == "other remote\n"
    # 本地版进隔离区：DB 记录 + 隔离文件
    assert len(conflicts) == 1
    rec = conflicts[0]
    assert rec["card_id"] == mid
    conn = index.connect(a)
    row = conn.execute(
        "SELECT status, local_file, remote_file FROM card_conflicts WHERE conflict_id=?",
        (rec["conflict_id"],)).fetchone()
    conn.close()
    assert row is not None
    assert row[1] and Path(row[1]).read_text(encoding="utf-8").find("A 机版本") >= 0
    assert row[2] and Path(row[2]).read_text(encoding="utf-8").find("B 机版本") >= 0
    # 无证据打平：保持 open，不自动选边
    assert row[0] == "open"
    # 合流后 A 可继续推送（不再分叉）
    git(a, "push", "-q", "origin", "main")


def test_silent_auto_merge_is_detected_and_quarantined(two_clones):
    _remote, a, b, mid = two_clones
    # 两边改不同部位：git 能静默合并，但仍是同卡并发写。
    edit_card(b, mid, "原始版本", "B 机版本")
    git(b, "add", "-A")
    git(b, "commit", "-qm", "b edits conclusion")
    git(b, "push", "-q", "origin", "main")
    edit_card(a, mid, "占位", "A 机占位")
    git(a, "add", "-A")
    git(a, "commit", "-qm", "a edits problem section")
    git(a, "fetch", "-q", "origin")

    _changed, _push_needed, conflicts = gitops.integrate_with_conflicts(a, "origin", "main")
    assert len(conflicts) == 1
    rec = conflicts[0]
    assert rec["card_id"] == mid
    conn = index.connect(a)
    row = conn.execute(
        "SELECT status, local_file, remote_file, merged_file FROM card_conflicts "
        "WHERE conflict_id=?", (rec["conflict_id"],)).fetchone()
    conn.close()
    assert row is not None and row[0] == "open"
    assert "A 机占位" in Path(row[1]).read_text(encoding="utf-8")
    assert "B 机版本" in Path(row[2]).read_text(encoding="utf-8")
    # 合并产物本身也被保留（不静默丢版本）
    assert row[3] and "A 机占位" in Path(row[3]).read_text(encoding="utf-8")
    assert "B 机版本" in Path(row[3]).read_text(encoding="utf-8")
    # 无证据打平：工作树保持合并结果，不自动改写成某一方
    tree = card_path(a, mid).read_text(encoding="utf-8")
    assert "A 机占位" in tree and "B 机版本" in tree


def test_detect_divergent_cards_unit(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "pitfalls").mkdir()
    (repo / "pitfalls" / "P-abcdef123456.md").write_text("base\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-qb", "local")
    (repo / "pitfalls" / "P-abcdef123456.md").write_text("local\n")
    git(repo, "commit", "-qam", "local")
    local = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "main")
    git(repo, "checkout", "-qb", "remote")
    (repo / "pitfalls" / "P-abcdef123456.md").write_text("remote\n")
    git(repo, "commit", "-qam", "remote")
    remote = git(repo, "rev-parse", "HEAD").stdout.strip()
    found = gitops.detect_divergent_cards(repo, base, local, remote)
    assert [f["card_id"] for f in found] == ["P-abcdef123456"]
    # 两边改成相同内容不算冲突
    git(repo, "checkout", "-q", "local")
    (repo / "pitfalls" / "P-abcdef123456.md").write_text("same\n")
    git(repo, "commit", "-qam", "local same")
    local2 = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "remote")
    (repo / "pitfalls" / "P-abcdef123456.md").write_text("same\n")
    git(repo, "commit", "-qam", "remote same")
    remote2 = git(repo, "rev-parse", "HEAD").stdout.strip()
    assert gitops.detect_divergent_cards(repo, base, local2, remote2) == []


# ---------- doctor 可见 + 双崩溃遗留边界 ----------

def test_doctor_reports_open_conflicts(write_repo):
    conn = index.connect(write_repo)
    rec = quarantine.record_conflict(
        write_repo, conn, "P-abcdef123456", base_rev="",
        local_text="local version", remote_text="remote version")
    conn.close()
    from knowbase import doctor
    checks = {c["name"]: c for c in doctor.inspect(write_repo)}
    assert "card_conflicts" in checks
    assert checks["card_conflicts"]["level"] == "warn"
    assert "P-abcdef123456" in checks["card_conflicts"]["message"]
    assert rec["conflict_id"] in checks["card_conflicts"]["message"]


def test_double_crash_ambiguous_intents_not_falsely_recovered(write_repo):
    from knowbase import writer
    result = writer.create(write_repo, "pitfall", "双崩溃卡", CARD_BODY,
                           scope="test", push=False)
    assert not result.get("error"), result
    mid = result["entity_id"]
    conn = index.connect(write_repo)
    # 模拟崩溃：文件已写新版，entity_heads 未入账；两条 pending intent 同卡。
    meta, body, path = store.load(write_repo, mid)
    path.write_text(store.render(meta, body.replace("第一版", "崩溃版")),
                    encoding="utf-8")
    for rid in ("R-amb-1", "R-amb-2"):
        conn.execute(
            "INSERT INTO write_intents(intent_id,request_id,entity_id,scope,kind,"
            "payload_hash,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (f"INT-{rid}", rid, mid, "", "update", f"ph-{rid}", "pending",
             "2026-10-24T00:00:00"))
    conn.commit()
    out = commit_adapter.recover_intents(write_repo, conn)
    rows = conn.execute(
        "SELECT status FROM write_intents WHERE request_id IN ('R-amb-1','R-amb-2')"
    ).fetchall()
    conn.close()
    # 不得凭修订号差异猜测归属：两条都保持 pending
    assert [r[0] for r in rows] == ["pending", "pending"]
    assert any("ambiguous" in str(d) for d in out["details"])
    assert out["recovered"] == 0


def test_single_pending_intent_still_recovers(write_repo):
    """单条 pending 时现有恢复语义不变（回归保护）。"""
    from knowbase import writer
    result = writer.create(write_repo, "pitfall", "单崩溃卡", CARD_BODY,
                           scope="test", push=False)
    assert not result.get("error"), result
    mid = result["entity_id"]
    conn = index.connect(write_repo)
    meta, body, path = store.load(write_repo, mid)
    path.write_text(store.render(meta, body.replace("第一版", "崩溃版")),
                    encoding="utf-8")
    conn.execute(
        "INSERT INTO write_intents(intent_id,request_id,entity_id,scope,kind,"
        "payload_hash,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
        ("INT-single", "R-single", mid, "", "update", "ph-single", "pending",
         "2026-10-24T00:00:00"))
    conn.commit()
    out = commit_adapter.recover_intents(write_repo, conn)
    row = conn.execute(
        "SELECT status FROM write_intents WHERE request_id='R-single'").fetchone()
    conn.close()
    assert out["recovered"] == 1
    assert row[0] == "completed"


# ---------- 自动裁决：非打平胜者写回、后续证据打破打平 ----------

def _fabricate_helpful_evidence(conn, mid, revision, task_keys):
    """往库里直写版本绑定 helpful 证据（测试替身，绕开完整反馈链路）。"""
    for i, key in enumerate(task_keys):
        use_id, outcome_id = f"U-{key}", f"TO-{key}"
        conn.execute(
            "INSERT INTO knowledge_uses(use_id,host_profile,session_id,event_id,"
            "payload_hash,context_event_id,task_id,task_revision,scope,memory_id,"
            "content_revision,state,current_version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (use_id, "pytest", f"s{i}", f"e{i}", "ph", "", f"t{i}", "", "test",
             mid, revision, "done", 1, "2026-10-24T00:00:00"))
        conn.execute(
            "INSERT INTO task_outcomes(task_outcome_id,task_id,task_run_id,result,"
            "verification_source,host_profile,session_id,use_id,memory_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (outcome_id, f"t{i}", "", "completed", "agent_report", "pytest",
             f"s{i}", use_id, mid, "2026-10-24T00:00:00", "2026-10-24T00:00:00"))
        conn.execute(
            "INSERT INTO feedback_events(receipt_id,request_id,payload_hash,memory_id,"
            "content_revision,use_id,task_key,task_outcome_id,agent,outcome,binding,"
            "state,before_hash,after_hash,after_meta_json,result_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"FB-{key}", "", "ph", mid, revision, use_id, key, outcome_id,
             "pytest", "helpful", "version_task", "applied", "", "", "{}", "{}",
             "2026-10-24T00:00:00"))
    conn.commit()


def test_adjudication_restores_stronger_local_version(write_repo):
    from knowbase import writer
    result = writer.create(write_repo, "pitfall", "裁决卡", CARD_BODY,
                           scope="test", push=False)
    assert not result.get("error"), result
    mid = result["entity_id"]
    conn = index.connect(write_repo)
    meta, body, path = store.load(write_repo, mid)
    local_text = store.render(meta, body.replace("第一版", "本地强证据版"))
    local_rev = _compute_revision(*store.parse_text(local_text))
    remote_text = path.read_text(encoding="utf-8")
    rec = quarantine.record_conflict(write_repo, conn, mid, base_rev="",
                                     local_text=local_text, remote_text=remote_text,
                                     source="rebase")
    _fabricate_helpful_evidence(conn, mid, local_rev, ["k1", "k2"])
    status, _note, restored = quarantine.adjudicate(write_repo, conn, rec)
    row = conn.execute(
        "SELECT status FROM card_conflicts WHERE conflict_id=?",
        (rec["conflict_id"],)).fetchone()
    head = conn.execute(
        "SELECT content_revision FROM entity_heads WHERE entity_id=?", (mid,)).fetchone()
    conn.close()
    assert status == "local_wins" and restored
    assert row[0] == "local_wins"
    assert path.read_text(encoding="utf-8") == local_text
    assert head[0] == local_rev
    msg = git(write_repo, "log", "-1", "--format=%B").stdout
    assert "conflict-resolve" in msg and "Knowbase-Base:" in msg


def test_readjudication_after_new_evidence_breaks_tie(write_repo):
    from knowbase import writer
    result = writer.create(write_repo, "pitfall", "重裁决卡", CARD_BODY,
                           scope="test", push=False)
    assert not result.get("error"), result
    mid = result["entity_id"]
    conn = index.connect(write_repo)
    meta, body, path = store.load(write_repo, mid)
    local_text = store.render(meta, body.replace("第一版", "后来居上版"))
    local_rev = _compute_revision(*store.parse_text(local_text))
    rec = quarantine.record_conflict(write_repo, conn, mid, base_rev="",
                                     local_text=local_text,
                                     remote_text=path.read_text(encoding="utf-8"),
                                     source="auto_merge")
    # 无证据时打平保持 open
    status, _, _ = quarantine.adjudicate(write_repo, conn, rec)
    assert status == "open"
    # 后续反馈给本地版证据，再裁决即打破打平
    _fabricate_helpful_evidence(conn, mid, local_rev, ["k9"])
    outcomes = quarantine.readjudicate_card(write_repo, conn, mid)
    row = conn.execute(
        "SELECT status FROM card_conflicts WHERE conflict_id=?",
        (rec["conflict_id"],)).fetchone()
    conn.close()
    assert outcomes and outcomes[0][1] == "local_wins"
    assert row[0] == "local_wins"
    assert path.read_text(encoding="utf-8") == local_text
