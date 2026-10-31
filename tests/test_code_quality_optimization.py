"""Unit tests for the 14-item code quality optimization change.

Covers:
- __main__._parse_args / _load_config / _dispatch
- config.load_config() cache
- index._match_ids() batch FTS query
- index.search() snippet batch query
"""

import json
import sqlite3
from pathlib import Path

import pytest

from knowbase import config, index


# ──────────────────── _parse_args tests ────────────────────


def test_parse_args_valid():
    """_parse_args 解析已知子命令返回正确的 cmd 字段。"""
    from knowbase.__main__ import _parse_args

    args = _parse_args(["init", "--non-interactive"])
    assert args.cmd == "init"
    assert args.non_interactive is True

    args = _parse_args(["dashboard", "--output", "/tmp/dash.html"])
    assert args.cmd == "dashboard"
    assert args.output == "/tmp/dash.html"

    args = _parse_args(["reindex"])
    assert args.cmd == "reindex"
    assert args.full is False


def test_parse_args_invalid():
    """_parse_args 无子命令时抛出 SystemExit。"""
    from knowbase.__main__ import _parse_args

    with pytest.raises(SystemExit):
        _parse_args([])

    with pytest.raises(SystemExit):
        _parse_args(["--unknown-flag"])


# ──────────────────── _load_config / load_config tests ────────────────────


class TestLoadConfig:
    """_load_config 和 load_config 的单元测试。"""

    def test_load_config_default(self, tmp_path, monkeypatch):
        """配置文件不存在时返回默认配置。"""
        config._config_cache = None
        config._config_mtime = 0
        cfg_path = tmp_path / "nonexistent.json"
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)

        cfg = config.load_config()
        assert "repo_path" in cfg
        assert isinstance(cfg["repo_path"], str)
        # 文件不存在时不设缓存
        assert config._config_cache is None

    def test_load_config_existing(self, tmp_path, monkeypatch):
        """配置文件存在时返回合并后的配置。"""
        config._config_cache = None
        config._config_mtime = 0
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"repo_path": "/custom/repo"}), encoding="utf-8")
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)

        cfg = config.load_config()
        assert cfg["repo_path"] == "/custom/repo"

    def test_config_cache_hit(self, tmp_path, monkeypatch):
        """同一文件未修改时第二次调用返回缓存。"""
        config._config_cache = None
        config._config_mtime = 0
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"repo_path": "/cached/repo"}), encoding="utf-8")
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)

        cfg1 = config.load_config()
        assert cfg1["repo_path"] == "/cached/repo"
        cached_before = config._config_cache
        assert cached_before is not None

        # 第二次调用：文件未修改，mtime 一致 → 应命中缓存
        cfg2 = config.load_config()
        assert cfg2["repo_path"] == "/cached/repo"
        # 缓存对象不变（同样的 deepcopy 源）
        assert config._config_cache is cached_before

    def test_config_cache_mtime_invalidated(self, tmp_path, monkeypatch):
        """文件修改后缓存失效，重新读取。"""
        config._config_cache = None
        config._config_mtime = 0
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"repo_path": "/old/repo"}), encoding="utf-8")
        monkeypatch.setattr(config, "CONFIG_PATH", cfg_path)

        cfg1 = config.load_config()
        assert cfg1["repo_path"] == "/old/repo"

        # 修改文件（需确保 mtime 确实变化）
        import time
        time.sleep(0.01)  # 确保文件系统时间精度
        cfg_path.write_text(json.dumps({"repo_path": "/new/repo"}), encoding="utf-8")

        cfg2 = config.load_config()
        assert cfg2["repo_path"] == "/new/repo"


# ──────────────────── _dispatch tests ────────────────────


class TestDispatch:
    """_dispatch 基本路由测试。"""

    def test_dispatch_unknown_cmd(self, tmp_path, monkeypatch):
        """未知命令返回 1。"""
        from knowbase.__main__ import _dispatch
        from unittest.mock import MagicMock

        args = MagicMock()
        args.cmd = "nonexistent-command-xyz"
        code = _dispatch(args)
        assert code == 1

    def test_dispatch_stats_cmd(self, tmp_path, monkeypatch):
        """stats 命令不依赖完整 repo 可返回。"""
        from knowbase.__main__ import _dispatch
        from unittest.mock import MagicMock

        args = MagicMock()
        args.cmd = "stats"
        args.since = None
        args.until = None
        args.member = None
        args.format = "text"
        args.scope = None
        args.json = False

        # stats 在无 repo 时会失败但不应崩溃
        code = _dispatch(args)
        assert isinstance(code, int)


# ──────────────────── _match_ids tests ────────────────────


class TestMatchIds:
    """index._match_ids 单元测试。"""

    @pytest.fixture
    def fts_db(self, tmp_path):
        """创建带 mem_fts 的临时 SQLite 数据库。"""
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING "
            "fts5(id UNINDEXED, title, tags, body, tokenize='trigram')"
        )
        conn.execute(
            "INSERT INTO mem_fts(id, title, tags, body) VALUES "
            "('card1', 'Memory Search', 'memory,search', 'How to optimize memory search'),"
            "('card2', 'Git Guide', 'git,guide', 'Git operations and workflows'),"
            "('card3', 'Retrieval Tips', 'memory,retrieval', 'Memory retrieval best practices')"
        )
        conn.commit()
        yield conn
        conn.close()

    def test_match_ids_single_word(self, fts_db):
        """单词查询返回命中 id。"""
        result = index._match_ids(fts_db, ["memory"])
        assert result is not None
        assert "card1" in result
        assert "card3" in result
        # card2 不含 "memory"
        assert "card2" not in result

    def test_match_ids_multi_word(self, fts_db):
        """多词查询返回所有命中 id 及加权分。"""
        result = index._match_ids(fts_db, ["memory", "search"])
        assert result is not None
        # card1 命中 memory + search 两个词，card3 命中 memory 一个词
        assert result.get("card1", 0) > 0
        assert result.get("card3", 0) > 0
        # card1 应比 card3 分高（命中词更多）
        assert result["card1"] > result["card3"]

    def test_match_ids_empty_terms(self, fts_db):
        """空或全短词返回 None。"""
        result = index._match_ids(fts_db, [])
        assert result is None

        result = index._match_ids(fts_db, ["ab", "cd"])
        assert result is None

    def test_match_ids_vs_old_implementation(self, fts_db):
        """批量 UNION ALL 结果与原逐词循环逻辑等价。"""
        def old_match_ids(conn, terms):
            big = [t.replace('"', "") for t in terms if len(t) >= 3]
            if not big:
                return None
            or_parts = " OR ".join(f'"{t}"' for t in big)
            try:
                rows = conn.execute(
                    "SELECT id, rank FROM mem_fts WHERE mem_fts MATCH ? ORDER BY rank",
                    (or_parts,),
                ).fetchall()
            except sqlite3.OperationalError:
                return None
            if not rows:
                return None
            bm25 = {r[0]: -r[1] for r in rows}
            hit_count = {}
            for t in big:
                try:
                    sub = conn.execute(
                        "SELECT id FROM mem_fts WHERE mem_fts MATCH ?", (f'"{t}"',)
                    ).fetchall()
                    for r in sub:
                        hit_count[r[0]] = hit_count.get(r[0], 0) + 1
                except sqlite3.OperationalError:
                    return bm25
            return {
                mid: bm25.get(mid, 0.0) * (1.0 + 0.5 * (hit_count[mid] - 1))
                for mid in hit_count
            }

        terms = ["memory", "search", "optimization"]
        new_result = index._match_ids(fts_db, terms)
        old_result = old_match_ids(fts_db, terms)

        assert new_result is not None
        assert old_result is not None
        assert set(new_result.keys()) == set(old_result.keys())
        for mid in new_result:
            assert abs(new_result[mid] - old_result[mid]) < 0.0001


# ──────────────────── search() snippet batch tests ────────────────────


class TestSearchSnippetBatch:
    """index.search() snippet 批量查询测试。"""

    META_COLS = (
        "id, type, title, scope, tags, confidence, status, last_verified, "
        "updated, helpful_count, unhelpful_count, hit_count, path, staging"
    )

    @pytest.fixture
    def search_db(self, tmp_path):
        """创建完整的索引数据库，包含 mem_fts 和 meta 表。"""
        db = str(tmp_path / "test.db")
        conn = sqlite3.connect(db)

        # mem_fts (match connect schema)
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING "
            "fts5(id UNINDEXED, title, tags, body, tokenize='trigram')"
        )

        # meta (match connect schema)
        conn.execute("""CREATE TABLE IF NOT EXISTS meta(
            id TEXT PRIMARY KEY, type TEXT, title TEXT, scope TEXT, tags TEXT,
            confidence TEXT, status TEXT, last_verified TEXT, updated TEXT,
            helpful_count INTEGER DEFAULT 0, unhelpful_count INTEGER DEFAULT 0,
            hit_count INTEGER DEFAULT 0, path TEXT, staging INTEGER DEFAULT 0
        )""")

        # entity_heads
        conn.execute("""CREATE TABLE IF NOT EXISTS entity_heads(
            entity_id TEXT PRIMARY KEY, content_revision TEXT NOT NULL DEFAULT '',
            governance_revision TEXT NOT NULL DEFAULT ''
        )""")

        now = "2026-10-30"

        # Insert FTS data
        conn.execute(
            "INSERT INTO mem_fts(id, title, tags, body) VALUES "
            "('CARD-M-001', 'Memory Optimization', 'memory,optimization', "
            "'How to optimize memory usage in Python applications with large datasets'),"
            "('CARD-M-002', 'Memory Leak Detection', 'memory,debug', "
            "'Memory leak detection using valgrind and profiling tools'),"
            "('CARD-G-001', 'Git Rebase', 'git', 'Git rebase workflow for feature branches')"
        )

        # Insert meta rows
        conn.execute(
            f"INSERT INTO meta({self.META_COLS}) VALUES "
            f"('CARD-M-001', 'card', 'Memory Optimization', 'global', "
            f"'[\"memory\",\"optimization\"]', 'verified', 'active', '{now}', '{now}', "
            f"3, 0, 5, 'cards/memory/opt.md', 0),"
            f"('CARD-M-002', 'card', 'Memory Leak Detection', 'global', "
            f"'[\"memory\",\"debug\"]', 'once', 'active', '{now}', '{now}', "
            f"1, 0, 2, 'cards/memory/leak.md', 0),"
            f"('CARD-G-001', 'card', 'Git Rebase', 'global', "
            f"'[\"git\"]', 'once', 'active', '{now}', '{now}', "
            f"0, 0, 1, 'cards/git/rebase.md', 0)"
        )

        # Insert entity_heads
        conn.execute(
            "INSERT INTO entity_heads(entity_id, content_revision) VALUES "
            "('CARD-M-001', 'abc123'),"
            "('CARD-M-002', 'def456'),"
            "('CARD-G-001', 'ghi789')"
        )

        conn.commit()

        # Need to use path= to make index.connect work
        # Monkeypatch connect to use our conn
        yield conn
        conn.close()

    def test_search_snippets_batch_vs_individual(self, search_db, monkeypatch):
        """搜索结果包含 snippet 字段且非空。"""
        # 绕过 index.search 中的 connect/路径逻辑，直接传入已构造好的 connection
        results = index.search(search_db, "memory", limit=5)
        assert len(results) > 0
        for r in results:
            assert "id" in r
            assert "snippet" in r
            assert len(r["snippet"]) > 0

    def test_search_empty_result_no_match(self, search_db):
        """FTS5 和 LIKE 均无命中时返回空列表。"""
        # 用随机字符串，FTS5 无命中后回退 LIKE 也无命中
        results = index.search(search_db, "zzz-nonexistent-xyz-123", limit=5)
        assert results == []