"""低危缺陷永久回归测试（代码深审低危 6 项）。

- 低危-1 sources.find_knowledge_cards_referencing 解包顺序写错
  （iter_all 产出 (meta, body, path)，旧代码按 (mid, meta, _) 解包），
  调用必崩 AttributeError；此前无调用方未被发现。
- 低危-2 CLI extract 导入截断正文（text[:100000]、segment[:4000]）却
  保留全文哈希，且手搓载荷与 MCP 路径分叉（无 protocol_version、
  无 job_segments manifest）。已改为与 MCP 共用 capture_payload 构造。
- 低危-3 staging 判断口径不一致：INDEX.md 与 CLI list 用
  `"staging" in str(path)` 子串匹配，仓库路径含 "staging" 时全部卡
  被误标 staging；已统一为 path.parent.name == "staging"。
- 低危-4 mutations 与 commit_adapter 语义漂移：模块 docstring 与实现
  不符（写顺序、加锁、职责），update 的 CAS docstring 两处都写错；
  staging 判定与 extra_meta None 处理先向生产实现对齐；其后已按决策
  删除 mutations.create_card / update_card，下述对齐用例迁移到
  生产实现 adapter_create（断言的行为属性不变）。
- 低危-5 semantic_index.build 对 job_spool 缺失无防护：
  fetchone()[0] 抛未捕获 TypeError 冒泡崩溃；已证实为真，现显性
  failed 并返回 job_spool_missing。
- 低危-6a jobs.renew 可无限续租过期租约；6b gitops.push 白名单为空
  时静默跳过校验放行推送；6c RepoLock 获取锁异常时句柄未关。
"""

import errno
import json
import os
import subprocess
import types
from pathlib import Path

import pytest

from knowbase import commit_adapter, config, gitops, index, jobs, locking, sources, store
from knowbase.__main__ import cmd_import, cmd_init, cmd_list
from knowbase.materials import digest


@pytest.fixture()
def isolated_repo(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    cfg_path = tmp_path / "config.json"
    cfg = config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo),
        "knowledge_path": "",
        "git": {"auto_commit": False, "auto_push": False, "auto_pull": False},
    })
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-agent")
    assert cmd_init() == 0
    return repo


def _write_card(repo: Path, mid: str, *, staging: bool = False, source_refs=None) -> Path:
    meta = store.new_meta("pitfall", "回归测试卡", "global", [], "pytest")
    meta["id"] = mid
    if source_refs is not None:
        meta["source_refs"] = source_refs
    path = store.mem_path(repo, "pitfall", mid, staging=staging)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.render(meta, "正文内容。"), encoding="utf-8")
    return path


# ---------- 低危-1：find_knowledge_cards_referencing 解包 ----------

def test_find_referencing_cards_returns_ids(isolated_repo):
    _write_card(isolated_repo, "P-2026-7001",
                source_refs=[{"source_id": "SRC-aaa"}, {"source_id": "SRC-bbb"}])
    _write_card(isolated_repo, "P-2026-7002", source_refs=[{"source_id": "SRC-bbb"}])
    _write_card(isolated_repo, "P-2026-7003")  # 无 source_refs
    assert sources.find_knowledge_cards_referencing(isolated_repo, "SRC-aaa") == ["P-2026-7001"]
    assert sorted(sources.find_knowledge_cards_referencing(isolated_repo, "SRC-bbb")) == [
        "P-2026-7001", "P-2026-7002"]


def test_find_referencing_cards_includes_staging_and_empty(isolated_repo):
    _write_card(isolated_repo, "P-2026-7004", staging=True,
                source_refs=[{"source_id": "SRC-ccc"}])
    assert sources.find_knowledge_cards_referencing(isolated_repo, "SRC-ccc") == ["P-2026-7004"]
    assert sources.find_knowledge_cards_referencing(isolated_repo, "SRC-不存在") == []


# ---------- 低危-2：CLI extract 与 MCP 路径一致 ----------

BIG_TEXT = "这是用于回归的填充段落，内容无敏感信息。\n\n" * 6000  # >100k 字符


def _latest_payload_and_idem(repo: Path):
    conn = index.connect(repo)
    try:
        payload = json.loads(conn.execute(
            "SELECT input_json FROM job_spool ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        idem = conn.execute(
            "SELECT idempotency_key FROM jobs ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        nseg = conn.execute("SELECT COUNT(*) FROM job_segments").fetchone()[0]
        return payload, idem, nseg
    finally:
        conn.close()


def test_cli_extract_stores_full_text_with_matching_hashes(isolated_repo, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "big.txt").write_text(BIG_TEXT, encoding="utf-8")
    assert cmd_import(str(src), "standard", "demo", False, "human:import",
                      mode="extract") == 0
    payload, idem, nseg = _latest_payload_and_idem(isolated_repo)
    # 全文存储，不再有 100000 截断
    assert len(payload["text"]) > 100000
    assert payload["text"].strip() == BIG_TEXT.strip()
    # 与 MCP 同构造：protocol v2、哈希按存储内容计算、manifest 已登记
    assert payload["protocol_version"] == 2
    assert all(s["text_hash"] == digest(s["text"]) for s in payload["segments"])
    assert nseg == len(payload["segments"]) > 0
    assert idem == f"extract:demo:{(src / 'big.txt').resolve()}:{payload['input_version']}"


def test_cli_and_mcp_extract_share_idempotency(isolated_repo, tmp_path):
    """同一文件经 CLI 与 MCP 两条路径导入，幂等键一致、不重复入队。"""
    from knowbase.server import import_impl
    src = tmp_path / "src"
    src.mkdir()
    (src / "doc.txt").write_text("一份普通文档内容，用于跨路径幂等校验。\n", encoding="utf-8")
    assert cmd_import(str(src), "standard", "demo", False, "human:import",
                      mode="extract") == 0
    out = import_impl(str(src), scope="demo", mode="extract")
    assert "入队 0 个 extract 任务" in out
    conn = index.connect(isolated_repo)
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
    finally:
        conn.close()


# ---------- 低危-3：staging 判定按目录而非路径子串 ----------

@pytest.fixture()
def repo_under_staging_path(tmp_path: Path, monkeypatch):
    repo = tmp_path / "staging-home" / "repo"  # 路径含 "staging"
    cfg_path = tmp_path / "config.json"
    cfg = config._deep_merge(config.DEFAULTS, {
        "repo_path": str(repo),
        "knowledge_path": "",
        "git": {"auto_commit": False, "auto_push": False, "auto_pull": False},
    })
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)
    monkeypatch.setenv("KNOWBASE_REPO_PATH", str(repo))
    monkeypatch.setenv("KNOWBASE_AGENT_NAME", "pytest-agent")
    assert cmd_init() == 0
    return repo


def test_index_md_staging_count_ignores_repo_path(repo_under_staging_path):
    _write_card(repo_under_staging_path, "P-2026-7011")
    _write_card(repo_under_staging_path, "P-2026-7012", staging=True)
    index.build_index_md(repo_under_staging_path)
    text = (repo_under_staging_path / "INDEX.md").read_text(encoding="utf-8")
    header = [ln for ln in text.splitlines() if "staging 提案" in ln][0]
    assert "staging 提案 1 条" in header  # 只有真 staging 目录里的那张


def test_cmd_list_staging_marker_ignores_repo_path(repo_under_staging_path, capsys):
    _write_card(repo_under_staging_path, "P-2026-7013")
    _write_card(repo_under_staging_path, "P-2026-7014", staging=True)
    assert cmd_list(None) == 0
    out = capsys.readouterr().out
    normal = [ln for ln in out.splitlines() if "P-2026-7013" in ln][0]
    staged = [ln for ln in out.splitlines() if "P-2026-7014" in ln][0]
    assert "(staging)" not in normal
    assert "(staging)" in staged


# ---------- 低危-4：写实现对齐（原 mutations 用例迁移到生产实现） ----------

def test_adapter_create_standard_lands_in_staging(isolated_repo):
    """standard 类型创建即进 staging（原 mutations.create_card 对齐用例，
    该函数已删除，迁移到生产实现 adapter_create，断言不变）。"""
    conn = index.connect(isolated_repo)
    try:
        result = commit_adapter.adapter_create(
            isolated_repo, conn, "standard", "规范卡", "正文。",
            scope="global", request_id="low4-staging")
        assert result["created"] is True
        path = store.find_file(isolated_repo, result["entity_id"])
        assert path is not None and path.parent.name == "staging"
    finally:
        conn.close()


def test_adapter_create_extra_meta_none_keeps_default(isolated_repo):
    """extra_meta 的 None 不覆盖默认字段（原 mutations.create_card 对齐
    用例，该函数已删除，迁移到生产实现 adapter_create，断言不变）。"""
    conn = index.connect(isolated_repo)
    try:
        result = commit_adapter.adapter_create(
            isolated_repo, conn, "pitfall", "普通卡", "正文。",
            scope="global", request_id="low4-none", confidence=None)
        meta, _body, _path = store.load(isolated_repo, result["entity_id"])
        assert meta["confidence"]  # 仍是 new_meta 的默认可信度，未被 None 覆盖
    finally:
        conn.close()


# ---------- 低危-5：semantic_index.build job_spool 缺失 ----------

def _insert_spoolless_embed_job(repo: Path):
    conn = index.connect(repo)
    try:
        conn.execute(
            "INSERT INTO jobs(job_id,kind,scope,input_hash,idempotency_key,state,attempt,"
            "max_attempts,next_run_at,fencing_token,batch_cursor,batch_total,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("J-spoolless", jobs.KIND_EMBED, "global", "x", "embed:spoolless", "queued",
             0, 3, "2000-01-01T00:00:00+00:00", 0, 0, 0,
             "2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00"))
        conn.commit()
    finally:
        conn.close()


def test_build_missing_job_spool_fails_explicitly(isolated_repo, monkeypatch):
    from knowbase import semantic_index
    monkeypatch.setattr(semantic_index, "schedule", lambda conn, force=False: None)
    _insert_spoolless_embed_job(isolated_repo)
    stub = types.SimpleNamespace(fingerprint="fp-test")
    out = semantic_index.build(isolated_repo, provider=stub)  # 不得抛 TypeError
    assert out == {"state": "failed", "reason": "job_spool_missing"}
    conn = index.connect(isolated_repo)
    try:
        row = conn.execute(
            "SELECT state,error FROM jobs WHERE job_id='J-spoolless'").fetchone()
        assert row == ("failed", "job_spool_missing")
    finally:
        conn.close()


def test_build_with_spool_does_not_hit_missing_guard(isolated_repo, monkeypatch):
    """有 spool 的 job 不被误伤：走到 provider 指纹校验并按既有语义处理。"""
    from knowbase import semantic_index
    monkeypatch.setattr(semantic_index, "schedule", lambda conn, force=False: None)
    conn = index.connect(isolated_repo)
    try:
        res = jobs.enqueue(conn, jobs.KIND_EMBED, "global",
                           {"provider_fp": "other-fp", "corpus": "x"},
                           idempotency_key="embed:with-spool")
        assert res["created"] is True
    finally:
        conn.close()
    stub = types.SimpleNamespace(fingerprint="fp-test", calls=0,
                                 usage_summary=lambda: {})
    out = semantic_index.build(isolated_repo, provider=stub)
    assert out.get("reason") == "configuration_changed"  # 指纹不符的既有路径


# ---------- 低危-6a：jobs.renew 过期不可续 ----------

def _claimed_job(conn) -> tuple[str, str]:
    res = jobs.enqueue(conn, jobs.KIND_EXTRACT, "global", {"segments": []})
    jid = res["job_id"]
    jobs.set_preparing(conn, jid)
    jobs.set_awaiting(conn, jid, 1)
    claim = jobs.claim(conn, jid, "sess-1")
    return jid, claim["lease_token"]


def test_renew_before_expiry_still_works(isolated_repo):
    conn = index.connect(isolated_repo)
    try:
        jid, token = _claimed_job(conn)
        out = jobs.renew(conn, jid, token)
        assert "expires_at" in out
    finally:
        conn.close()


def test_renew_after_expiry_rejected(isolated_repo):
    conn = index.connect(isolated_repo)
    try:
        jid, token = _claimed_job(conn)
        conn.execute("UPDATE jobs SET lease_until='2000-01-01T00:00:00+00:00' "
                     "WHERE job_id=?", (jid,))
        conn.commit()
        assert jobs.renew(conn, jid, token) == {"error": jobs.LEASE_EXPIRED}
    finally:
        conn.close()


# ---------- 低危-6b：gitops.push 空白名单拒绝 ----------

def _git(repo: Path, *args: str) -> None:
    env = dict(os.environ,
               GIT_AUTHOR_NAME="check", GIT_AUTHOR_EMAIL="check@local",
               GIT_COMMITTER_NAME="check", GIT_COMMITTER_EMAIL="check@local")
    subprocess.run(["git", "-C", str(repo), *args], check=True, env=env,
                   capture_output=True)


@pytest.fixture()
def two_clones(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(gitops, "_record_sync", lambda *_a, **_k: None)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    alice = tmp_path / "alice"
    env = dict(os.environ,
               GIT_AUTHOR_NAME="check", GIT_AUTHOR_EMAIL="check@local",
               GIT_COMMITTER_NAME="check", GIT_COMMITTER_EMAIL="check@local")
    subprocess.run(["git", "clone", "-q", str(remote), str(alice)],
                   check=True, env=env, capture_output=True)
    _git(alice, "config", "user.name", "Alice")
    _git(alice, "config", "user.email", "alice@test")
    (alice / "a.md").write_text("a\n", encoding="utf-8")
    _git(alice, "add", "a.md")
    _git(alice, "commit", "-q", "-m", "a")
    _git(alice, "branch", "-M", "main")
    _git(alice, "push", "-q", "-u", "origin", "main")
    subprocess.run(["git", "--git-dir", str(remote), "symbolic-ref", "HEAD",
                    "refs/heads/main"], check=True)
    (alice / "b.md").write_text("b\n", encoding="utf-8")
    _git(alice, "add", "b.md")
    _git(alice, "commit", "-q", "-m", "b")
    return alice, remote


def _remote_subjects(remote: Path) -> str:
    out = subprocess.run(["git", "--git-dir", str(remote), "log", "--format=%s", "main"],
                         capture_output=True, text=True)
    return out.stdout


def test_push_empty_whitelist_refused(two_clones):
    alice, remote = two_clones
    warning = gitops.push(alice, "", [])
    assert warning is not None and "白名单为空" in warning
    assert "b" not in _remote_subjects(remote).splitlines()  # 新提交未被推送


def test_push_with_matching_prefix_still_works(two_clones):
    alice, remote = two_clones
    assert gitops.push(alice, "", [str(remote)]) is None
    assert "b" in _remote_subjects(remote).splitlines()


# ---------- 低危-6c：RepoLock 异常路径释放句柄 ----------

def test_repolock_lock_error_closes_handle(isolated_repo, monkeypatch):
    def boom(_fh):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(locking, "_try_lock", boom)
    lock = locking.RepoLock(isolated_repo, timeout=1.0)
    with pytest.raises(OSError):
        with lock:
            pass
    assert lock._fh is None  # 句柄已关闭并清空，未泄漏


def test_repolock_timeout_closes_handle(isolated_repo, monkeypatch):
    monkeypatch.setattr(locking, "_try_lock", lambda _fh: False)
    lock = locking.RepoLock(isolated_repo, timeout=0.1)
    with pytest.raises(locking.LockTimeout):
        with lock:
            pass
    assert lock._fh is None


def test_repolock_normal_roundtrip(isolated_repo):
    with locking.RepoLock(isolated_repo, timeout=1.0):
        pass
    with locking.RepoLock(isolated_repo, timeout=1.0):  # 释放后可再次获取
        pass
