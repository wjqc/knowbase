"""P0 永久回归测试。

覆盖此前修复的 6 个缺陷，防止回退：
- P0-1 卡片 ID 路径穿越 / 仓库边界
- P0-2 凭据脱敏绕过（frontmatter、嵌套结构、URL 密码）
- P0-3 检索召回坍塌（主体词 + 现象词型提问）
- P0-4 extract 任务材料读取（segments 而非 checkpoint.events）
- P0-5 空 query 不得全库召回
"""

import json
from pathlib import Path

import pytest

from knowbase import config, index, sources, store
from knowbase.__main__ import cmd_init
from knowbase.extraction import sanitizer, validator
from knowbase.extraction import recall
from knowbase.server import _extract_events


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


def _card(repo: Path, mid: str, dtype: str, title: str, body: str, scope="proj-a"):
    meta = store.new_meta(dtype, title, scope, [], "test")
    meta["id"] = mid
    path = store.mem_path(repo, dtype, mid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(store.render(meta, body), encoding="utf-8")
    conn = index.connect(repo)
    try:
        index.upsert(conn, meta, body, path)
    finally:
        conn.close()
    return meta


# ---------- P0-1 路径穿越 ----------

class TestPathTraversal:
    @pytest.mark.parametrize("bad", [
        "../../victim",
        "../victim",
        "P-2026-0001/../../victim",
        "/etc/passwd",
        "P-2026-0001\x00",
    ])
    def test_mem_path_rejects_illegal_id(self, isolated_repo, bad):
        with pytest.raises(ValueError):
            store.mem_path(isolated_repo, "pitfall", bad)

    def test_find_file_cannot_escape_repo(self, isolated_repo, tmp_path):
        for d in store.ALL_DIRS:
            (isolated_repo / d).mkdir(parents=True, exist_ok=True)

        # 仓库外存在同名文件，任何穿越形式都必须解析不到
        (tmp_path / "victim.md").write_text("secret", encoding="utf-8")
        assert store.find_file(isolated_repo, "../../victim") is None
        assert store.find_file(isolated_repo, "../victim") is None
        assert store.find_file(isolated_repo, "/etc/passwd") is None

    def test_find_file_returns_legal_card(self, isolated_repo):
        _card(isolated_repo, "P-2026-0001", "pitfall", "标题", "正文")
        found = store.find_file(isolated_repo, "P-2026-0001")
        assert found is not None and found.name == "P-2026-0001.md"

    def test_load_manifest_rejects_traversal(self, isolated_repo, tmp_path):
        assert sources.load_manifest(isolated_repo, "../../etc/passwd") is None
        assert sources.load_manifest(isolated_repo, "SRC-2026-0001") is None

    def test_symlink_out_of_repo_is_not_returned(self, isolated_repo, tmp_path):
        outside = tmp_path / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        d = isolated_repo / store.TYPE_DIR["pitfall"]
        d.mkdir(parents=True, exist_ok=True)
        link = d / "P-2026-0002.md"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("平台不支持符号链接")
        assert store.find_file(isolated_repo, "P-2026-0002") is None


# ---------- P0-2 凭据脱敏 ----------

SECRET_SAMPLES = [
    'password: "Sup3rSecret!"',
    'api_key = "ak-live-9988776655"',
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",
    "authorization: Basic YWRtaW46YWRtaW4xMjM=",
    "aws key AKIAIOSFODNN7EXAMPLE here",
    "mysql://root:p@ssw0rd123@10.0.0.1:3306/db",
]

CLEAN_SAMPLES = [
    "结论：把配置集中到环境变量，避免散落在代码里。",
    "排查步骤：先看日志，再看请求参数，最后看下游响应。",
    "接口返回 401 时先确认 token 是否过期。",
    "password 字段为必填，且长度不得小于 8 位。",
    "配置项示例：retry_attempts: 3, backoff: exponential",
]


class TestCredentialRedaction:
    @pytest.mark.parametrize("sample", SECRET_SAMPLES)
    def test_find_secrets_detects(self, sample):
        assert store.find_secrets(sample), sample

    @pytest.mark.parametrize("sample", CLEAN_SAMPLES)
    def test_clean_text_not_flagged(self, sample):
        assert store.find_secrets(sample) == []

    def test_frontmatter_nested_dict_detected(self):
        meta = {"auth": {"api_key": "sk-live-abcdef123456"}}
        assert store.find_secrets(meta)
        assert sanitizer.has_credentials(
            json.dumps(meta, ensure_ascii=False)
        )

    def test_nested_list_of_dict_detected(self):
        meta = {"conns": [{"user": "root", "secret_token": "tok-abcdef123456"}]}
        assert store.find_secrets(meta)

    def test_sanitize_removes_every_secret(self):
        for sample in SECRET_SAMPLES:
            cleaned = sanitizer.sanitize(sample)
            assert not sanitizer.has_credentials(cleaned), sample
            assert store.find_secrets(cleaned) == [], sample

    def test_validator_flags_nested_secret(self):
        """嵌套在 list/dict 里的密钥不能绕过 validator。"""
        candidate = {
            "body": {
                "conclusion": "把凭据交给密钥管理服务。",
                "problem": "本地明文配置容易泄漏。",
                "scope": "proj-a",
                "notes": [{"api_key": "sk-live-abcdef123456"}],
            },
            "type": "pitfall",
            "scope": "proj-a",
            "evidence": ["file.py:10"],
        }
        result = validator.validate_candidate(candidate)
        assert not result.ok
        assert any("密钥" in e for e in result.errors)

    def test_validator_flags_nested_top_level_secret(self):
        candidate = {
            "body": {
                "conclusion": "结论",
                "problem": "问题",
                "scope": "proj-a",
            },
            "type": "pitfall",
            "scope": "proj-a",
            "evidence": ["file.py:10"],
            "extra": {"password": "Sup3rSecret!"},
        }
        result = validator.validate_candidate(candidate)
        assert not result.ok
        assert any("密钥" in e for e in result.errors)


# ---------- P0-3 检索召回 ----------

class TestRecallNotCollapsed:
    def test_multi_term_question_hits(self, isolated_repo):
        _card(
            isolated_repo, "P-2026-0101", "pitfall",
            "EasyConnect 死锁",
            "## 现象\nEasyConnect 在长时间空闲后重连会死锁。\n\n"
            "## 原因\n连接池未设置保活。\n\n"
            "## 正确做法\n配置 keepalive 并重启客户端。\n",
        )
        _card(
            isolated_repo, "P-2026-0102", "pitfall",
            "Docker 镜像加速",
            "## 现象\nDocker 拉取镜像很慢。\n\n"
            "## 正确做法\n配置 registry mirror。\n",
        )

        conn = index.connect(isolated_repo)
        try:
            hits = index.search(conn, "EasyConnect 死锁", limit=5)
            ids = [h["id"] for h in hits]
            assert "P-2026-0101" in ids, "主体词 + 现象词型提问必须召回"

            # 单个词命中不应被清空
            assert index.search(conn, "死锁", limit=5)
        finally:
            conn.close()

    def test_short_term_question_not_gated_to_empty(self, isolated_repo):
        _card(
            isolated_repo, "W-2026-0103", "workflow",
            "上线流程",
            "## 步骤\n发布前先跑一遍回归测试，确认核心链路可用。\n",
        )
        conn = index.connect(isolated_repo)
        try:
            assert index.search(conn, "上线 回归", limit=5), "短词组合不应被硬闸门过滤"
        finally:
            conn.close()

    def test_scope_isolation_kept(self, isolated_repo):
        _card(isolated_repo, "P-2026-0104", "pitfall", "项目 A 经验", "只属于 proj-a。", scope="proj-a")
        _card(isolated_repo, "P-2026-0105", "pitfall", "项目 B 经验", "只属于 proj-b。", scope="proj-b")
        conn = index.connect(isolated_repo)
        try:
            hits = index.search(conn, "经验", scope="proj-a", limit=10)
            assert hits
            assert all(h["scope"] in ("proj-a", "global") for h in hits)
        finally:
            conn.close()


# ---------- P0-5 空 query ----------

class TestEmptyQuery:
    def test_wide_recall_empty_query_returns_nothing(self, isolated_repo):
        _card(isolated_repo, "P-2026-0201", "pitfall", "任意卡片", "正文内容")
        conn = index.connect(isolated_repo)
        try:
            assert recall.wide_recall(conn, "", "proj-a") == []
            assert recall.wide_recall(conn, "   ", "proj-a") == []
            assert recall.wide_recall(conn, None, "proj-a") == []
        finally:
            conn.close()

    def test_index_search_empty_query_returns_nothing(self, isolated_repo):
        _card(isolated_repo, "P-2026-0202", "pitfall", "任意卡片", "正文内容")
        conn = index.connect(isolated_repo)
        try:
            assert index.search(conn, "", limit=10) == []
            assert index.search(conn, "  ", limit=10) == []
        finally:
            conn.close()


# ---------- P0-4 extract 材料 ----------

class TestExtractMaterials:
    def test_extract_reads_segments_from_cursor(self):
        materials = {
            "segments": [
                {"segment_id": "s1", "text": "第一段"},
                {"segment_id": "s2", "text": "第二段"},
                {"segment_id": "s3", "text": "第三段"},
            ]
        }
        events = _extract_events(materials, 0)
        assert [e["segment_id"] for e in events] == ["s1", "s2", "s3"]
        assert [e["role"] for e in events] == ["material"] * 3

        rest = _extract_events(materials, 1)
        assert [e["segment_id"] for e in rest] == ["s2", "s3"]

    def test_extract_falls_back_to_text(self):
        events = _extract_events({"text": "整体文本"}, 0)
        assert events == [{"segment_id": "", "role": "material", "text": "整体文本"}]

    def test_extract_without_material_returns_empty(self):
        assert _extract_events({}, 0) == []
        assert _extract_events({"segments": []}, 0) == []

    def test_extract_skips_non_dict_segments(self):
        materials = {"segments": [{"segment_id": "s1", "text": "ok"}, "垃圾", None]}
        events = _extract_events(materials, 0)
        assert len(events) == 1

    def test_cursor_beyond_segments_returns_empty(self):
        materials = {"segments": [{"segment_id": "s1", "text": "a"}]}
        assert _extract_events(materials, 5) == []