"""中危缺陷永久回归测试（代码深审中危 4 项）。

- 中危-1 card-v2 小节校验子串绕过：「不适用条件」包含「适用条件」，
  缺失「适用条件」的卡片曾能零错误通过 lint。
- 中危-2 坏卡静默消失：iter_all 吞异常跳过解析失败的卡片，doctor
  基于同一计数自洽，健康检查照样通过。
- 中危-3 gitops 把一切 rebase 失败都报成“真实内容冲突”，新机器未配
  git 身份等环境问题被误报，误导排查。
- 中危-4 config.json 损坏零防护：裸 JSONDecodeError 不带文件路径，
  所有工具与 hook 崩溃且无法定位。
"""

import os
import subprocess
from pathlib import Path

import pytest

from knowbase import config, doctor, gitops, store


# ---------- 中危-1：card-v2 八项小节精确匹配 ----------

CARD_V2_FULL = (
    "## 结论\n结论内容。\n\n## 解决的问题\n问题内容。\n\n"
    "## 适用条件\n已结算订单。\n\n## 不适用条件\n草稿订单。\n\n"
    "## 可执行动作\n执行动作。\n\n## 关键证据\ncode:demo/a.py 第 3 行说明。\n\n"
    "## 验证情况\n2026-10-24 在测试环境运行回归并记录结果。\n\n## 未知与待确认\n暂无。\n"
)


def test_card_v2_full_card_passes():
    assert store.card_v2_errors(CARD_V2_FULL) == []


def test_card_v2_missing_applies_cannot_borrow_not_applies():
    """只有「不适用条件」、缺「适用条件」必须报错（子串绕过回归）。"""
    body = CARD_V2_FULL.replace("## 适用条件\n已结算订单。\n\n", "")
    assert "不适用条件" in body and "## 适用条件" not in body.replace("## 不适用条件", "")
    errs = store.card_v2_errors(body)
    assert "缺少必填小节: 适用条件" in errs


def test_card_v2_missing_not_applies_still_caught():
    body = CARD_V2_FULL.replace("## 不适用条件\n草稿订单。\n\n", "")
    assert "缺少必填小节: 不适用条件" in store.card_v2_errors(body)


def test_card_v2_empty_applies_not_satisfied_by_not_applies_text():
    """「适用条件」只有标题无内容时，不得拿「不适用条件」的正文充数。"""
    body = CARD_V2_FULL.replace("## 适用条件\n已结算订单。", "## 适用条件")
    errs = store.card_v2_errors(body)
    assert "小节「适用条件」仅有标题无内容" in errs


def test_card_v2_each_section_individually_required():
    for sec in store.CARD_SECTIONS:
        import re as _re
        body = _re.sub(rf"## {_re.escape(sec)}\n.*?\n\n", "", CARD_V2_FULL, flags=_re.S)
        if body == CARD_V2_FULL:  # 最后一节没有尾随空行
            body = CARD_V2_FULL.split(f"## {sec}")[0]
        assert f"缺少必填小节: {sec}" in store.card_v2_errors(body), sec


# ---------- 中危-2：坏卡显性可见 ----------

def _write_card(repo: Path, mid: str, title: str) -> Path:
    p = repo / "pitfalls" / f"{mid}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(store.render(
        {"id": mid, "type": "pitfall", "title": title, "scope": "global", "tags": []},
        "## 现象\nx\n\n## 原因\ny\n\n## 正确做法\nz\n"), encoding="utf-8")
    return p


def test_iter_all_collects_parse_errors(tmp_path):
    good = _write_card(tmp_path, "P-2026-0001", "好卡")
    bad = tmp_path / "pitfalls" / "P-2026-0002.md"
    bad.write_text("损坏内容 {{{ 没有 frontmatter", encoding="utf-8")

    errors: list = []
    cards = list(store.iter_all(tmp_path, errors=errors))
    assert [c[2] for c in cards] == [good]
    assert len(errors) == 1 and errors[0][0] == bad
    assert isinstance(errors[0][1], ValueError)


def test_iter_all_without_errors_param_keeps_legacy_semantics(tmp_path):
    _write_card(tmp_path, "P-2026-0001", "好卡")
    (tmp_path / "pitfalls" / "P-2026-0002.md").write_text("坏卡", encoding="utf-8")
    # 不传 errors：仍静默跳过、不抛异常（既有调用方语义不变）
    assert len(list(store.iter_all(tmp_path))) == 1


def test_doctor_reports_unparseable_cards(memory_repo):
    checks = {c["name"]: c for c in doctor.inspect(memory_repo)}
    assert checks["unparseable_cards"]["level"] == "pass"

    bad = memory_repo / "pitfalls" / "P-2026-0999.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("这张卡坏了 {{{", encoding="utf-8")
    checks = {c["name"]: c for c in doctor.inspect(memory_repo)}
    entry = checks["unparseable_cards"]
    assert entry["level"] == "fail"
    assert "P-2026-0999.md" in entry["message"]


# ---------- 中危-3：rebase 失败分类 ----------

def _fake_run_factory(rebase_exc: Exception, conflicted: str = ""):
    def fake_run(_repo, *args, **_kwargs):
        if args[:1] == ("rebase",) and args[1:2] != ("--abort",):
            raise rebase_exc
        if args[:3] == ("diff", "--name-only", "--diff-filter=U"):
            return conflicted
        if args[:2] == ("rebase", "--abort"):
            return ""
        if args[:2] == ("rev-parse", "--git-dir"):
            return ".git"
        raise AssertionError(args)
    return fake_run


def test_rebase_identity_failure_not_reported_as_conflict(tmp_path, monkeypatch):
    exc = RuntimeError(
        "Committer identity unknown\n\n*** Please tell me who you are.\n\n"
        "fatal: unable to auto-detect email address (got 'x@(none)')")
    monkeypatch.setattr(gitops, "_run", _fake_run_factory(exc))
    with pytest.raises(RuntimeError) as caught:
        gitops._rebase_onto(tmp_path, "origin/main")
    assert not isinstance(caught.value, gitops.GitSyncConflict)
    assert "身份未配置" in str(caught.value)
    assert "真实内容冲突" not in str(caught.value)


def test_rebase_other_failure_not_reported_as_conflict(tmp_path, monkeypatch):
    exc = RuntimeError("error: cannot rebase: You have unstaged changes.")
    monkeypatch.setattr(gitops, "_run", _fake_run_factory(exc))
    with pytest.raises(RuntimeError) as caught:
        gitops._rebase_onto(tmp_path, "origin/main")
    assert not isinstance(caught.value, gitops.GitSyncConflict)
    assert "非内容冲突" in str(caught.value)
    assert "保留本地提交" in str(caught.value)


def test_rebase_real_conflict_still_raises_sync_conflict(tmp_path, monkeypatch):
    exc = RuntimeError("CONFLICT (content): Merge conflict in pitfalls/P-1.md")
    monkeypatch.setattr(gitops, "_run", _fake_run_factory(exc, conflicted="pitfalls/P-1.md"))
    with pytest.raises(gitops.GitSyncConflict) as caught:
        gitops._rebase_onto(tmp_path, "origin/main")
    assert "真实内容冲突" in str(caught.value)
    assert "pitfalls/P-1.md" in str(caught.value)


def test_rebase_identity_failure_end_to_end(tmp_path, monkeypatch):
    """真实 git：仓库未配 user.name/user.email 且环境无身份时，
    rebase 分叉提交必须报身份问题，不得报内容冲突。"""
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_") and k != "HOME"}
    env.update(HOME=str(home), GIT_CONFIG_NOSYSTEM="1")

    def git(repo: Path, *args, ident=False):
        cmd = ["git", "-C", str(repo)]
        if ident:
            cmd += ["-c", "user.name=T", "-c", "user.email=t@t"]
        r = subprocess.run(cmd + list(args), env=env, capture_output=True, text=True)
        assert r.returncode == 0, (args, r.stderr)
        return r

    base = tmp_path / "base"; base.mkdir()
    git(base, "init", "-q", "-b", "main")
    (base / "a.txt").write_text("a\n")
    git(base, "add", "-A"); git(base, "commit", "-qm", "init", ident=True)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(base), str(remote)],
                   env=env, check=True)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], env=env, check=True)
    (other / "b.txt").write_text("b\n")
    git(other, "add", "-A"); git(other, "commit", "-qm", "remote", ident=True)
    git(other, "push", "-q")
    local = tmp_path / "local"
    subprocess.run(["git", "clone", "-q", str(remote), str(local)], env=env, check=True)
    git(local, "reset", "-q", "--hard", "HEAD~1")
    (local / "c.txt").write_text("c\n")
    git(local, "add", "-A"); git(local, "commit", "-qm", "local", ident=True)
    git(local, "fetch", "-q", "origin")

    # gitops 子进程继承 pytest 进程 env：清空身份来源，只留 hermetic 环境
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL",
                "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(RuntimeError) as caught:
        gitops._rebase_onto(local, "origin/main")
    assert not isinstance(caught.value, gitops.GitSyncConflict)
    assert "身份未配置" in str(caught.value)
    # 本地提交保留、未留下半截 rebase
    assert git(local, "log", "--oneline", "-1", ident=False).stdout.strip().endswith("local")
    assert not (local / ".git" / "rebase-merge").exists()
    assert not (local / ".git" / "rebase-apply").exists()


# ---------- 中危-4：config 损坏显性报错 ----------

def test_load_config_corrupt_json_raises_locatable_error(tmp_path, monkeypatch):
    bad = tmp_path / "config.json"
    bad.write_text('{"repo_path": "~/knowbase", "git": {', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", bad)
    with pytest.raises(config.ConfigError) as caught:
        config.load_config()
    assert str(bad) in str(caught.value)
    # 兼容既有按 ValueError 捕获的调用方
    assert isinstance(caught.value, ValueError)


def test_load_config_non_dict_json_raises_locatable_error(tmp_path, monkeypatch):
    bad = tmp_path / "config.json"
    bad.write_text("[1, 2, 3]", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", bad)
    with pytest.raises(config.ConfigError) as caught:
        config.load_config()
    assert str(bad) in str(caught.value)


def test_load_config_valid_json_still_merges(tmp_path, monkeypatch):
    good = tmp_path / "config.json"
    good.write_text('{"lock_timeout": 3.5, "git": {"auto_push": true}}', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_PATH", good)
    cfg = config.load_config()
    assert cfg["lock_timeout"] == 3.5
    assert cfg["git"]["auto_push"] is True
    assert cfg["git"]["auto_commit"] is True  # 未覆盖项保留默认


def test_load_config_missing_file_uses_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "absent.json")
    cfg = config.load_config()
    assert cfg["repo_path"] == config.DEFAULTS["repo_path"]
