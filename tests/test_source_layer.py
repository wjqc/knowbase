"""source/card 分层（P0）测试：source 导入行为、reference 退出检索、八项结构强制、存量兼容。"""

import json
from datetime import date
from pathlib import Path

import pytest

from knowbase import config, index, sources, store
from knowbase.__main__ import cmd_import, cmd_init
from knowbase.locking import RepoLock
from knowbase.server import read_impl, save_impl, search_impl, update_impl


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


CARD_OK = """## 结论
聚合目录本身不是 Git 仓库时，Git 操作必须进入子仓库执行。

## 解决的问题
避免把“当前目录不是仓库”误判为项目没有版本控制。

## 适用条件
- 工作区根目录聚合多个独立 Git 仓库
- 任务涉及 git 提交或分支操作

## 不适用条件
- 根目录本身是 monorepo
- 子目录只是普通模块

## 可执行动作
1. 按目标文件定位所属子仓库
2. 在子仓库内执行 git status 确认分支
3. 提交与推送限定在该子仓库

## 关键证据
聚合根目录执行 git status 返回 not a git repository；子仓库内正常返回分支与远端。

## 验证情况
2026-09-21 macOS arm64 Git 2.39 实测两个子仓库确认。

## 未知与待确认
尚未验证 Windows worktree 行为。
"""


def _import_one_file(repo: Path, tmp_path: Path, name: str, text: str, scope: str = "demo"):
    src = tmp_path / "outside-docs"
    src.mkdir(exist_ok=True)
    (src / name).write_text(text, encoding="utf-8")
    assert cmd_import(str(src), "reference", scope, False, "human:import") == 0
    return src


# ---------- source 导入 ----------

def test_import_creates_source_not_card(isolated_repo, tmp_path):
    _import_one_file(isolated_repo, tmp_path, "架构设计.md",
                     "# 架构设计\n\n数据库连接池上限与回收策略说明。\n")
    manifests = list(sources.iter_manifests(isolated_repo))
    assert len(manifests) == 1
    mf = manifests[0]
    assert mf["id"].startswith("SRC-")
    assert mf["scope"] == "demo"
    obj = isolated_repo / mf["object"]
    assert obj.is_file() and "连接池" in obj.read_text(encoding="utf-8")
    # manifest 内容寻址：object 相对路径 + sha 匹配（正文按导入时的 strip 形态）
    import hashlib
    assert mf["sha256"] == hashlib.sha256(
        "# 架构设计\n\n数据库连接池上限与回收策略说明。\n".strip().encode("utf-8")).hexdigest()
    # 不产生任何知识卡（各类型目录与 staging 均为空）
    for d in store.ALL_DIRS:
        assert not list((isolated_repo / d).glob("*.md")), d


def test_source_not_in_default_search_or_index(isolated_repo, tmp_path):
    _import_one_file(isolated_repo, tmp_path, "量子计算编译框架调研.md",
                     "# 量子计算编译框架调研\n\n量子计算编译框架选型对比。\n", scope="global")
    assert "未命中" in search_impl("量子计算 编译框架", scope="global")
    assert "未命中" in search_impl("量子计算 编译框架", scope="global", include_inactive=True)
    idx = (isolated_repo / "INDEX.md").read_text(encoding="utf-8")
    assert "量子计算" not in idx


def test_reimport_same_content_dedup(isolated_repo, tmp_path, capsys):
    src = _import_one_file(isolated_repo, tmp_path, "架构设计.md",
                           "# 架构设计\n\n连接池策略。\n")
    assert cmd_import(str(src), "reference", "demo", False, "human:import") == 0
    out = capsys.readouterr().out
    assert "导入 0 个 source artifact" in out and "内容重复" in out
    assert sources.count_sources(isolated_repo) == 1


def test_source_manifest_has_no_machine_path(isolated_repo, tmp_path):
    src = tmp_path / "outside-docs"
    src.mkdir()
    (src / "lesson.md").write_text("# 导入经验\n\n正文已经完整复制。\n", encoding="utf-8")
    assert cmd_import(str(src), "reference", "demo", False, "human:import") == 0
    for mf in (isolated_repo / sources.MANIFESTS_DIR).glob("*.yaml"):
        assert str(tmp_path) not in mf.read_text(encoding="utf-8")
    # 本机幂等状态仍保留绝对源路径（memory.db，不进共享内容）
    conn = index.connect(isolated_repo)
    row = conn.execute("SELECT COUNT(*) FROM source_state").fetchone()
    conn.close()
    assert row[0] == 1


# ---------- reference 退出可发布与可检索 ----------

def test_save_rejects_reference_type(isolated_repo):
    r = save_impl("reference", "整篇架构文档", CARD_OK)
    assert r.startswith("错误：")
    assert "reference" in r and "knowbase import" in r


def test_search_rejects_reference_type_param(isolated_repo):
    r = search_impl("任意", type="reference", scope="global")
    assert r.startswith("错误：") and "退出默认检索" in r


def test_legacy_reference_card_excluded_from_search_and_index(isolated_repo):
    meta = {
        "id": "R-2026-0001", "type": "reference", "title": "量子计算编译框架文档",
        "scope": "global", "tags": ["import"], "source": "human:import",
        "confidence": "once", "status": "active", "last_verified": "",
        "helpful_count": 0, "unhelpful_count": 0,
        "created": date.today().isoformat(), "updated": date.today().isoformat(),
        "relations": [],
    }
    path = isolated_repo / "references" / "R-2026-0001.md"
    path.write_text(store.render(meta, "# 量子计算编译框架\n\n正文快照。"), encoding="utf-8")
    with RepoLock(isolated_repo, 10.0):
        index.rebuild(isolated_repo)
    assert "未命中" in search_impl("量子计算 编译框架", scope="global")
    idx = (isolated_repo / "INDEX.md").read_text(encoding="utf-8")
    assert "R-2026-0001" not in idx and "references 1 条" in idx
    # 兼容读取：memory_read 仍可单条查看，并带原始材料提示
    out = read_impl("R-2026-0001")
    assert "量子计算编译框架" in out and "原始材料卡" in out


# ---------- 八项结构（card-v2） ----------

def test_card_v2_missing_section_rejected(isolated_repo):
    body = CARD_OK.replace("## 不适用条件\n- 根目录本身是 monorepo\n- 子目录只是普通模块\n\n", "")
    r = save_impl("pitfall", "缺少不适用条件的卡", body)
    assert r.startswith("错误：lint") and "缺少必填小节: 不适用条件" in r


def test_card_v2_empty_section_rejected(isolated_repo):
    body = CARD_OK.replace(
        "聚合根目录执行 git status 返回 not a git repository；子仓库内正常返回分支与远端。",
        "")
    r = save_impl("pitfall", "关键证据只有标题", body)
    assert "小节「关键证据」仅有标题无内容" in r


def test_card_v2_vague_condition_rejected(isolated_repo):
    body = CARD_OK.replace(
        "- 工作区根目录聚合多个独立 Git 仓库\n- 任务涉及 git 提交或分支操作",
        "- 视情况而定\n- 根据实际情况处理")
    r = save_impl("pitfall", "适用条件空洞", body)
    assert "视情况而定" in r and "不可判断" in r


def test_card_v2_reference_only_evidence_rejected(isolated_repo):
    body = CARD_OK.replace(
        "聚合根目录执行 git status 返回 not a git repository；子仓库内正常返回分支与远端。",
        "参见团队架构设计文档。")
    r = save_impl("pitfall", "证据只有参考引用", body)
    assert "关键证据不能只写" in r


def test_card_v2_test_pass_only_verification_rejected(isolated_repo):
    body = CARD_OK.replace(
        "2026-09-21 macOS arm64 Git 2.39 实测两个子仓库确认。",
        "测试通过")
    r = save_impl("pitfall", "验证只有测试通过", body)
    assert "不构成验证记录" in r


def test_card_v2_valid_card_saved(isolated_repo):
    r = save_impl("pitfall", "多项目聚合目录的 git 操作位置", CARD_OK)
    assert r.startswith("已保存 P-"), r


def test_new_card_carries_schema_marker(isolated_repo):
    save_impl("pitfall", "带 schema 标记的新卡", CARD_OK)
    for meta, _body, _path in store.iter_all(isolated_repo):
        assert meta["schema"] == store.CARD_V2_SCHEMA


# ---------- 存量卡兼容（不迁移存量） ----------

def test_legacy_card_update_keeps_old_rules(isolated_repo):
    meta = {
        "id": "", "type": "pitfall", "title": "存量三段式旧卡", "scope": "global",
        "tags": ["legacy"], "source": "agent:legacy:old", "confidence": "once",
        "status": "active", "last_verified": "", "helpful_count": 0,
        "unhelpful_count": 0, "created": date.today().isoformat(),
        "updated": date.today().isoformat(), "relations": [],
    }
    mid = store.alloc_id(isolated_repo, "pitfall")
    meta["id"] = mid
    path = store.mem_path(isolated_repo, "pitfall", mid)
    path.write_text(store.render(meta, "## 现象\n旧。\n\n## 原因\n旧。\n\n## 正确做法\n旧。\n"),
                    encoding="utf-8")
    conn = index.connect(isolated_repo)
    index.upsert(conn, meta, "旧", path, False)
    conn.close()
    r = update_impl(mid, body="## 现象\n新。\n\n## 原因\n新。\n\n## 正确做法\n新。\n")
    assert r.startswith("已更新"), r
