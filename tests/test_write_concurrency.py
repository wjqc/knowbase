"""并发竞态回归测试：writer 两段锁 + CLI verify/archive 的 CAS TOCTOU。

覆盖两个缺陷（修复前均可稳定复现）：

1. writer 两段锁竞态（create/update）：
   adapter 在第一段 RepoLock 内写文件后释放锁，writer 在锁外 store.load
   读快照，再进第二段锁用该快照 _post_save → index.upsert 回写索引
   （upsert 同时回写 entity_heads）。两段之间另一写入者把同一张卡更新到
   更新版本时，先行者随后用旧快照回写，索引（FTS/meta/entity_heads）
   被降级到旧版本，与文件（唯一真相）分叉且无任何报错，直到全量
   rebuild 才恢复。

2. CLI verify/archive 的 CAS 检查在锁外（_human_op）：
   expect_revision 的 governance_revision 检查在获取 RepoLock 之前，
   检查通过后、fn 执行前状态可被其他写入者改变（如先 archive），
   verify 仍按旧期望执行，把已归档的卡改回 active。

复现手法（确定性，不靠运气）：用 _GatedRepoLock 替换被测模块的
RepoLock，在指定线程进入临界区之前用 Event 冻结它，等另一个写入者
完整走完自己的写入后再放行。冻结发生在获取锁之前，被冻结线程不持有
锁，因此不会死锁；交错顺序由事件保证，与机器速度无关。
"""

import json
import threading
from pathlib import Path

import pytest

from knowbase import commit_adapter, config, index, store, writer
from knowbase.__main__ import cmd_archive, cmd_init, cmd_verify
from knowbase.locking import RepoLock
from knowbase.mutations import _compute_revision


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


def _body(marker: str) -> str:
    """card-v2 八项小节正文（writer 路径会 lint），marker 放在结论里做版本标记。"""
    return (
        f"## 结论\n{marker}：并发写入时索引必须与文件保持一致。\n"
        "## 解决的问题\n并发更新同一张卡时索引被旧快照覆盖。\n"
        "## 适用条件\n多 Agent 同时通过 MCP 更新同一知识库时。\n"
        "## 不适用条件\n单进程串行写入时不涉及。\n"
        "## 可执行动作\n索引回写前在锁内重读最新快照。\n"
        "## 关键证据\n复现测试中 FTS 正文曾停留在旧版本标记。\n"
        "## 验证情况\n2026-10-24 本地 pytest 并发回归用例复现并验证。\n"
        "## 未知与待确认\n无。"
    )


class _GatedRepoLock(RepoLock):
    """在 gate_thread 进入临界区前冻结它：先发 at_gate 信号，再等 release。

    等待发生在 super().__enter__() 之前（尚未持锁），主线程可在此期间
    让另一个写入者完整执行，不会死锁。其他线程直接放行。
    """

    gate_thread: str | None = None
    at_gate: threading.Event | None = None
    release: threading.Event | None = None

    def __enter__(self):
        cls = type(self)
        if (cls.gate_thread is not None
                and threading.current_thread().name == cls.gate_thread):
            cls.at_gate.set()
            if not cls.release.wait(timeout=30):
                raise TimeoutError("gated lock was never released")
        return super().__enter__()


def _arm_gate(monkeypatch, module, thread_name: str):
    """把 module.RepoLock 换成门控锁，返回 (at_gate, release) 两个事件。"""
    at_gate, release = threading.Event(), threading.Event()
    _GatedRepoLock.gate_thread = thread_name
    _GatedRepoLock.at_gate = at_gate
    _GatedRepoLock.release = release
    monkeypatch.setattr(module, "RepoLock", _GatedRepoLock)
    return at_gate, release


def _run_in_thread(name: str, fn, outcome: dict):
    def target():
        try:
            outcome["result"] = fn()
        except Exception as exc:  # 线程内异常带回主线程断言，避免静默吞掉
            outcome["error"] = exc

    t = threading.Thread(target=target, name=name)
    t.start()
    return t


def _index_state(repo: Path, mid: str):
    conn = index.connect(repo)
    try:
        fts_body = conn.execute(
            "SELECT body FROM mem_fts WHERE id=?", (mid,)).fetchone()[0]
        head_rev = conn.execute(
            "SELECT content_revision FROM entity_heads WHERE entity_id=?",
            (mid,)).fetchone()[0]
    finally:
        conn.close()
    return fts_body, head_rev


# ---------- writer 两段锁竞态 ----------

class TestWriterTwoPhaseLockRace:
    def test_update_stale_snapshot_must_not_downgrade_index(
            self, isolated_repo, monkeypatch):
        """A 更新到 v2 后被冻结在第二段锁前，主线程把卡更新到 v3 并完成
        索引回写；A 恢复后不得用 v2 快照把索引降回 v2。"""
        repo = isolated_repo
        created = writer.create(repo, "pitfall", "并发更新竞态卡",
                                _body("第一版"), scope="test-proj", push=False)
        assert "error" not in created, created
        mid = created["entity_id"]

        at_gate, release = _arm_gate(monkeypatch, writer, "writer-A")
        outcome = {}
        t = _run_in_thread("writer-A", lambda: writer.update(
            repo, mid, body=_body("第二版"), push=False), outcome)
        assert at_gate.wait(timeout=30), "A 未到达第二段锁"

        # A 的第一段锁已释放（v2 已落盘）；主线程完整更新到 v3。
        rb = writer.update(repo, mid, body=_body("第三版"), push=False)
        assert "error" not in rb, rb
        release.set()
        t.join(timeout=30)
        assert not t.is_alive()
        assert "error" not in outcome, outcome
        assert "error" not in outcome["result"], outcome["result"]

        meta, body, _ = store.load(repo, mid)
        assert "第三版" in body
        fts_body, head_rev = _index_state(repo, mid)
        # 索引必须停留在最新版本 v3，而不是被 A 的旧快照降级到 v2。
        assert "第三版" in fts_body and "第二版" not in fts_body
        assert head_rev == _compute_revision(meta, body)

    def test_create_stale_snapshot_must_not_downgrade_index(
            self, isolated_repo, monkeypatch):
        """A 创建卡（v1）后被冻结在第二段锁前，主线程随即把卡更新到 v2；
        A 恢复后不得用 v1 快照把索引降回 v1。"""
        repo = isolated_repo
        at_gate, release = _arm_gate(monkeypatch, writer, "writer-A")
        outcome = {}
        t = _run_in_thread("writer-A", lambda: writer.create(
            repo, "pitfall", "并发创建竞态卡", _body("第一版"),
            scope="test-proj", push=False), outcome)
        assert at_gate.wait(timeout=30), "A 未到达第二段锁"

        # A 的 adapter 已落盘建卡，从仓库里找到这张卡并更新到 v2。
        cards = list(store.iter_all(repo, include_staging=True))
        assert len(cards) == 1
        mid = cards[0][0]["id"]
        rb = writer.update(repo, mid, body=_body("第二版"), push=False)
        assert "error" not in rb, rb
        release.set()
        t.join(timeout=30)
        assert not t.is_alive()
        assert "error" not in outcome, outcome
        assert "error" not in outcome["result"], outcome["result"]

        meta, body, _ = store.load(repo, mid)
        assert "第二版" in body
        fts_body, head_rev = _index_state(repo, mid)
        assert "第二版" in fts_body and "第一版" not in fts_body
        assert head_rev == _compute_revision(meta, body)


# ---------- CLI verify/archive CAS TOCTOU ----------

class TestHumanOpCasToctou:
    def test_verify_expect_revision_rechecked_under_lock(
            self, isolated_repo, monkeypatch):
        """A 以 expect_revision=R1 执行 verify，在 CAS 检查通过后、进锁前
        被冻结；主线程先 archive（治理 revision 变为 R2）。A 恢复后必须
        按版本冲突拒绝（rc=1），不得把已归档的卡改回 active。"""
        repo = isolated_repo
        conn = index.connect(repo)
        try:
            r1 = commit_adapter.adapter_create(
                repo, conn, "pitfall", "CAS TOCTOU 测试卡",
                _body("第一版"), scope="test-proj")
            assert "entity_id" in r1, r1
            mid, gov_rev = r1["entity_id"], r1["governance_revision"]
        finally:
            conn.close()

        from knowbase import __main__ as cli
        at_gate, release = _arm_gate(monkeypatch, cli, "cli-A")
        outcome = {}
        t = _run_in_thread("cli-A", lambda: cmd_verify(
            mid, expect_revision=gov_rev), outcome)
        assert at_gate.wait(timeout=30), "A 未到达 RepoLock"

        # 检查与执行之间，另一写入者先把卡归档（治理状态已变）。
        assert cmd_archive(mid) == 0
        release.set()
        t.join(timeout=30)
        assert not t.is_alive()
        assert "error" not in outcome, outcome

        assert outcome["result"] == 1, outcome
        meta, _, _ = store.load(repo, mid)
        assert meta["status"] == "archived"
