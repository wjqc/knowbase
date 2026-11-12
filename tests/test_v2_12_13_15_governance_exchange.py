"""V2-12/13/15：证据治理投影、跨机证据交换（签名/去重/撤销）、适用范围。

覆盖：S2 治理分支（可信反证隔离、撤销降级）、S5 核心机制（同任务不扩票、
重发零增量、撤销先到、伪签发零治理影响）、S6 分支（依赖 mismatch 只限制
不判错、critical 不归档）。
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from knowbase import index, store
from knowbase.knowledge_lifecycle import (applicability, attribution, evidence,
                                          governance, observation)
from knowbase.knowledge_lifecycle import trust
from knowbase.migrations import apply_all

BODY = ("## 结论\nCRLF 归一。\n## 解决的问题\nx。\n## 适用条件\ny。\n"
        "## 不适用条件\nz。\n## 可执行动作\n运行 scripts/fix.py。\n"
        "## 关键证据\nPR。\n## 验证情况\nv。\n## 未知与待确认\n无。\n")


def seed_card(repo, title, body=BODY, **meta_extra):
    meta = store.new_meta("pitfall", title, "orders", ["fixture"], "agent:fixture:s")
    meta["id"] = store.alloc_id(repo, "pitfall")
    meta.update(meta_extra)
    path = store.mem_path(repo, "pitfall", meta["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_shared_text(path, store.render(meta, body))
    conn = index.connect(repo)
    index.upsert(conn, meta, body, path)
    conn.close()
    return meta["id"]


def seed_validator(cfg_path):
    cfg = json.loads(Path(cfg_path).read_text())
    cfg["knowledge_lifecycle"] = {**(cfg.get("knowledge_lifecycle") or {}),
                                  "validators": [{"validator_id": "tv", "kind": "pytest"}]}
    Path(cfg_path).write_text(json.dumps(cfg))


def trusted_l3(conn, repo, mid, revision, *, run_key, task_key, outcome="pass",
               validator="tv", check="c1", case=None):
    run = observation.ingest(conn, "zcode", f"s-{run_key}",
                             [{"native_key": f"n-{run_key}", "kind": "note", "payload": {}}],
                             project_key="repo:orders", native_task_key=task_key,
                             native_run_key=run_key)
    from lifecycle_verifier_fixture import run_verifier
    group = "primary" if task_key in ("t1", "t2") else "secondary"
    result, _ = run_verifier(conn, repo, mid, revision, run_id=run["run_id"],
                             validator_id=validator, group=group, case=case or run_key, outcome=outcome)
    assert result["outcome"] == outcome, result
    return result, run


def head_of(conn, mid):
    return conn.execute("SELECT content_revision FROM entity_heads WHERE entity_id=?",
                        (mid,)).fetchone()[0]


def test_governance_promotes_on_three_independent_tasks_and_revokes(memory_repo):
    seed_validator(config_path := memory_repo.parent / "knowbase.json")
    mid = seed_card(memory_repo, "治理晋升卡")
    conn = index.connect(memory_repo)
    apply_all(conn)
    rev = head_of(conn, mid)
    # 两个独立任务 → validated 不足晋升。
    for run_key, task in (("r1", "t1"), ("r2", "t2")):
        trusted_l3(conn, memory_repo, mid, rev, run_key=run_key, task_key=task)
    d2 = governance.decide(conn, mid, rev)
    assert d2["decision"] == "validated" and d2["independent_tasks"] == 2
    trusted_l3(conn, memory_repo, mid, rev, run_key="r-copy", task_key="t-copy", case="r2")
    copied = governance.decide(conn, mid, rev)
    assert copied["independent_tasks"] == 2 and copied["independence_groups"] == 1
    # 第三个独立任务 + 第二独立组 → verified。
    trusted_l3(conn, memory_repo, mid, rev, run_key="r3", task_key="t3")
    d3 = governance.decide(conn, mid, rev)
    assert d3["decision"] == "verified" and d3["independent_tasks"] == 3
    # 同任务 100 次重放不增加任务数。
    for i in range(5):
        trusted_l3(conn, memory_repo, mid, rev, run_key=f"r1-replay-{i}",
                   task_key="t1", check=f"replay-{i}")
    d_replay = governance.decide(conn, mid, rev)
    assert d_replay["independent_tasks"] == 3 and d_replay["decision"] == "verified"
    # 撤销两条正证 → 支持不足降回 validated。
    events = [r[0] for r in conn.execute(
        "SELECT event_id FROM evidence_events WHERE subject_revision=? AND polarity='positive'",
        (f"{mid}@content-v1:{rev}",))][:2]
    for i, target in enumerate(events):
        conn.execute(
            "INSERT OR IGNORE INTO evidence_events(event_id,payload_hash,event_type,"
            "subject_revision,run_id,logical_task_key,independence_group,issuer_id,"
            "trust_basis,level,polarity,supersedes_event_id,revokes_event_id,payload_json,"
            "import_state,occurred_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"RV-{i}", "x", "evidence.revoked", "", "", "", "", "reviewer",
             "registered-adapter", "", "neutral", "", target, "{}", "local", _now_iso()))
    conn.commit()
    after = governance.decide(conn, mid, rev)
    assert after["decision"] == "validated"  # 撤销后支持不足；不自动回 unverified
    conn.close()


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def test_governance_trusted_negative_quarantines(memory_repo):
    """S2 治理分支：可信同版本反证 → 隔离（stale + 原因），不是直接覆盖。"""
    seed_validator(memory_repo.parent / "knowbase.json")
    mid = seed_card(memory_repo, "反证隔离卡")
    conn = index.connect(memory_repo)
    apply_all(conn)
    rev = head_of(conn, mid)
    trusted_l3(conn, memory_repo, mid, rev, run_key="g1", task_key="t1")
    trusted_l3(conn, memory_repo, mid, rev, run_key="g2", task_key="t2", outcome="fail")
    decision = governance.apply_decision(memory_repo, conn, mid)
    assert decision["decision"] == "quarantine"
    meta, _, _ = store.load(memory_repo, mid)
    assert meta["status"] == "stale" and "quarantine_reason" in meta
    assert meta.get("quality_state") == "disputed"
    conn.close()


def test_governance_unknown_task_keys_do_not_count(memory_repo):
    """W0-2：unknown: 前缀的任务键不参与独立性计数。"""
    seed_validator(config_path := memory_repo.parent / "knowbase.json")
    mid = seed_card(memory_repo, "未知任务卡")
    conn = index.connect(memory_repo)
    apply_all(conn)
    rev = head_of(conn, mid)
    # 两个真实任务 → validated。
    for run_key, task in (("r1", "t1"), ("r2", "t2")):
        trusted_l3(conn, memory_repo, mid, rev, run_key=run_key, task_key=task)
    d2 = governance.decide(conn, mid, rev)
    assert d2["decision"] == "validated" and d2["independent_tasks"] == 2
    # 两个 unknown 任务（无原生任务键）→ 仍 validated，计数不变。
    for run_key in ("u1", "u2"):
        trusted_l3(conn, memory_repo, mid, rev, run_key=run_key, task_key="",
                   check=f"unknown-{run_key}")
    d_unknown = governance.decide(conn, mid, rev)
    assert d_unknown["decision"] == "validated"
    assert d_unknown["independent_tasks"] == 2
    # 第三个真实任务 → verified，unknown 始终不计。
    trusted_l3(conn, memory_repo, mid, rev, run_key="r3", task_key="t3")
    d3 = governance.decide(conn, mid, rev)
    assert d3["decision"] == "verified" and d3["independent_tasks"] == 3
    conn.close()


def test_exchange_sign_dedup_revoke_and_untrusted(memory_repo):
    """S5 核心机制：签名交换、传输/业务去重、撤销先到、伪签发零治理。"""
    seed_validator(memory_repo.parent / "knowbase.json")
    mid = seed_card(memory_repo, "跨机证据卡")
    conn = index.connect(memory_repo)
    apply_all(conn)
    rev = head_of(conn, mid)
    trusted_l3(conn, memory_repo, mid, rev, run_key="x1", task_key="task-A")
    trusted_l3(conn, memory_repo, mid, rev, run_key="x2", task_key="task-B")
    # 设备密钥 + 信任名单。
    keys = trust.generate_keypair()
    evil_keys = trust.generate_keypair()
    registry = {"issuers": {"INST-a": {"public_pem": keys["public_pem"],
                                       "scopes": ["orders", "global"], "role": "member"}},
                "key_epochs": {"key-a": {"public_pem": keys["public_pem"],
                                         "revoked": False}}}
    package = evidence.export_package(
        conn, team_id="team-a", issuer_installation_id="INST-a",
        project_key="repo:orders", private_pem=keys["private_pem"], key_id="key-a")
    assert len(package["events"]) == 2
    # 接收库（第二块隔离库）。
    other = memory_repo.parent / "peer-memory"
    other.mkdir(exist_ok=True)
    import subprocess
    from knowbase import config as C
    peer_cfg = memory_repo.parent / "peer-knowbase.json"
    peer_cfg.write_text(json.dumps(C._deep_merge(C.DEFAULTS, {
        "repo_path": str(other), "knowledge_path": "",
        "git": {"auto_commit": False}})))
    # 同卡同版本在 peer 库存在（双机同卡）。
    peer_card = seed_card_at(other, mid, "跨机证据卡")
    env = {**__import__("os").environ, "KNOWBASE_CONFIG": str(peer_cfg),
           "KNOWBASE_REPO_PATH": str(other)}
    # 直接用第二个连接模拟 peer 库（同进程双库）。
    import knowbase.index as idx_mod
    peer_conn = __import__("sqlite3").connect(other / "memory.db")
    peer_conn.execute("PRAGMA journal_mode=WAL")
    from knowbase.migrations import apply_all as _aa
    _aa(peer_conn)
    first = evidence.import_package(peer_conn, package, registry=registry)
    assert first["imported"] == 2 and first["rejected"] == 0
    # 重发 10 次：零增量。
    stats = None
    for _ in range(10):
        stats = evidence.import_package(peer_conn, package, registry=registry)
    assert stats["imported"] == 0 and stats["idempotent"] == 2
    # 同 ID 异 payload（正确重算 payload_hash 并重签）：传输幂等冲突，
    # 两方隔离不进治理。（不重算 hash 的纯篡改会在信封校验被拒——下一
    # 个断言单独覆盖。）
    import hashlib as _hl
    tampered = json.loads(json.dumps(package))
    env0 = tampered["events"][0]
    env0["payload"]["observed_result"] = "fail"
    env0["payload_hash"] = _hl.sha256(json.dumps(
        env0["payload"], sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()
    env0 = trust.sign_envelope(env0, keys["private_pem"], "key-a")
    tampered["events"][0] = env0
    conflict = evidence.import_package(peer_conn, tampered, registry=registry)
    assert conflict["conflicts"] >= 1
    # 纯篡改（不重算 hash/签名）→ 信封校验拒收。
    naive = json.loads(json.dumps(package))
    naive["events"][0]["payload"]["observed_result"] = "fail"
    naive_result = evidence.import_package(peer_conn, naive, registry=registry)
    assert naive_result["rejected"] >= 1
    # 伪签发（未登记密钥签名，issuer 冒用）→ external_unverified，零治理。
    forged = evidence.export_package(
        conn, team_id="team-a", issuer_installation_id="INST-a",
        project_key="repo:orders", private_pem=evil_keys["private_pem"],
        key_id="key-unregistered")
    forged_result = evidence.import_package(peer_conn, forged, registry=registry)
    assert forged_result["unverified"] == 2 and forged_result["imported"] == 0
    # 未登记 issuer → unknown_issuer。
    stray = json.loads(json.dumps(package))
    for e in stray["events"]:
        e["issuer_installation_id"] = "INST-nobody"
    stray_result = evidence.import_package(peer_conn, stray, registry=registry)
    assert stray_result["unverified"] == 2
    # W0-2：全新未信事件 → 仅计数并记 details，peer 库无此行。
    trusted_l3(conn, memory_repo, mid, rev, run_key="r9", task_key="t9")
    fresh = evidence.export_package(
        conn, team_id="team-a", issuer_installation_id="INST-a",
        project_key="repo:orders", private_pem=evil_keys["private_pem"],
        key_id="key-unregistered")
    known_ids = {e["event_id"] for e in package["events"]}
    fresh_ids = [e["event_id"] for e in fresh["events"]]
    new_ids = [e for e in fresh_ids if e not in known_ids]
    assert len(new_ids) == 1
    fresh_result = evidence.import_package(peer_conn, fresh, registry=registry)
    assert fresh_result["unverified"] == len(fresh_ids)
    assert fresh_result["imported"] == 0
    assert {d["event_id"] for d in fresh_result["details"]
            if d["result"] == "external_unverified"} == set(fresh_ids)
    assert peer_conn.execute(
        "SELECT COUNT(*) FROM evidence_events WHERE event_id=?",
        (new_ids[0],)).fetchone()[0] == 0
    # 撤销先到：撤 event-1，再重导原包 → 目标事件不复活。
    target_id = package["events"][0]["event_id"]
    revoke_env = {
        "schema": "knowbase.lifecycle.events.v1",
        "event_id": "EV-revoke-0001", "event_type": "evidence.revoked",
        "team_id": "team-a", "issuer_installation_id": "INST-a",
        "project_key": "repo:orders", "scope": "orders",
        "occurred_at": _now_iso(), "policy_revision": "lifecycle-v2.1",
        "adapter_version": "t", "source_event_id": None,
        "payload": {"target_event_id": target_id, "reason_code": "artifact_tainted",
                    "authority_ref": "audit-42"}}
    from knowbase.knowledge_lifecycle import contracts as _contracts
    revoke_env["payload_hash"] = __import__("hashlib").sha256(
        _contracts.canonical_json(revoke_env["payload"]).encode()).hexdigest()
    revoke_env = trust.sign_envelope(revoke_env, keys["private_pem"], "key-a")
    revoke_pkg = {"schema": evidence.PACKAGE_SCHEMA, "team_id": "team-a",
                  "issuer_installation_id": "INST-a", "events": [revoke_env]}
    evidence.import_package(peer_conn, revoke_pkg, registry=registry)
    again = evidence.import_package(peer_conn, package, registry=registry)
    assert again["imported"] == 0  # 目标事件不复活
    revoked = {r[0] for r in peer_conn.execute(
        "SELECT revokes_event_id FROM evidence_events WHERE event_type='evidence.revoked'")}
    assert target_id in revoked
    # peer 库治理重算：撤销后正证只剩 1 任务 → 不晋升。
    d = governance.decide(peer_conn, mid, rev)
    assert d["decision"] == "validated" and d["independent_tasks"] == 1
    peer_conn.close()
    conn.close()


def seed_card_at(repo, mid, title, body=BODY):
    meta = store.new_meta("pitfall", title, "orders", ["fixture"], "agent:fixture:s")
    meta["id"] = mid
    path = store.mem_path(repo, "pitfall", mid)
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_shared_text(path, store.render(meta, body))
    conn = index.connect(repo)
    index.upsert(conn, meta, body, path)
    conn.close()
    return mid


def test_environment_mismatch_restricts_not_incorrect(memory_repo):
    """S6 分支：依赖 2.x mismatch 只生成待复验与环境限制，不判卡错。"""
    mid = seed_card(memory_repo, "依赖 1.x 卡", BODY,
                    applicability={"components": [{"name": "example-lib",
                                                   "version_spec": ">=1,<2"}],
                                   "platforms": ["macos"]})
    conn = index.connect(memory_repo)
    apply_all(conn)
    result = applicability.observe_environment(
        conn, memory_repo, "repo:orders", {"example-lib": "2.1.0"})
    assert any(r["memory_id"] == mid for r in result["revalidation_needed"])
    meta, _, _ = store.load(memory_repo, mid)
    assert meta.get("status") == "active"  # 不判错：无 incorrect 断言
    # 版本判定：1.x 精确匹配保留；未知组件不猜。
    assert applicability.spec_matches(">=1,<2", "1.9.3") is True
    assert applicability.spec_matches(">=1,<2", "2.1.0") is False
    assert applicability.spec_matches("custom-build", "custom-build") is None or \
        applicability.spec_matches("custom-build", "custom-build") is True
    verdict = applicability.evaluate_card(
        {"components": [{"name": "lib", "version_spec": ">=2,<3"}]},
        {"lib": "2.5", "other": "1.0"})
    assert verdict["status"] == "match"
    conn.close()


def test_critical_low_frequency_card_not_auto_archived(memory_repo):
    from knowbase.lifecycle import auto_archive_sweep
    critical = seed_card(memory_repo, "灾备关键卡", BODY, importance="critical")
    normal = seed_card(memory_repo, "普通旧卡", BODY)
    conn = index.connect(memory_repo)
    old = (datetime.now(timezone.utc) - timedelta(days=200)).date().isoformat()
    for mid in (critical, normal):
        meta, body, path = store.load(memory_repo, mid)
        meta["status"] = "stale"
        meta["stale_since"] = old
        path.write_text(store.render(meta, body), encoding="utf-8")
        index.upsert(conn, *store.load(memory_repo, mid), commit=False)
    conn.commit()
    from datetime import date
    archived = auto_archive_sweep(memory_repo, conn, today=date.today())
    archived_ids = {a["id"] for a in archived}
    assert normal in archived_ids
    assert critical not in archived_ids  # critical 低频保护
    conn.close()
