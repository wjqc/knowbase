import pytest
from knowbase.document_library import DocumentLibrary

@pytest.fixture
def library(tmp_path):
    root = tmp_path / 'inputs'
    root.mkdir()
    return DocumentLibrary(tmp_path / 'library'), root


def test_boundary_phrase_and_exact_lines(library):
    lib, root = library
    text = ['ordinary content\n'] * 80
    text[39], text[40] = 'crossboundaryalpha\n', 'crossboundarybeta\n'
    (root / 'a.txt').write_text(''.join(text))
    lib.sync(root, scope='x')
    hit = lib.search_page('crossboundaryalpha crossboundarybeta')['items'][0]
    assert hit['start_line'] == 40
    assert lib.read(hit['document_id'], start_line=40, end_line=41)['text'] == ''.join(text[39:41])


def test_path_title_ranking_and_file_aggregation(library):
    lib, root = library
    (root / 'pathonlyneedle.txt').write_text('nothing here')
    (root / 'body.txt').write_text('Body introduction\n' + ('pathonlyneedle\n' + 'ordinary\n' * 39) * 10)
    lib.sync(root, scope='x')
    page = lib.search_page('pathonlyneedle')
    assert page['total'] == 2
    assert page['items'][0]['path'] == 'pathonlyneedle.txt'
    assert len({r['document_id'] for r in page['items']}) == 2
    assert page['items'][1]['matched_chunks'] > 1
    assert lib.search_page('pathonlyneedle', scope='other')['total'] == 0


def test_seventy_files_pagination_without_gaps(library):
    lib, root = library
    for n in range(70):
        (root / f'{n:02d}.txt').write_text('sharedneedle\n')
    lib.sync(root, scope='x')
    pages = [lib.search_page('sharedneedle', limit=30, offset=n) for n in (0, 30, 60)]
    assert [len(p['items']) for p in pages] == [30, 30, 10]
    assert [p['has_more'] for p in pages] == [True, True, False]
    assert all(p['total'] == 70 for p in pages)
    assert len({r['document_id'] for p in pages for r in p['items']}) == 70
    assert lib.search_page('sharedneedle', offset=100)['total'] == 70


def test_structure_overlap_and_minified_bounds():
    text = ''.join('ordinary\n' for _ in range(24)) + '# Heading\n' + 'paragraph\n' * 50
    chunks = list(DocumentLibrary._structured_chunks(text))
    assert chunks[0][1] == 24
    assert chunks[1][0] == 20
    long = 'x' * 5900 + 'crossboundaryalpha crossboundarybeta' + 'y' * 7000
    parts = list(DocumentLibrary._structured_chunks(long))
    assert all(len(p[2]) <= 6000 for p in parts)
    assert any('crossboundaryalpha crossboundarybeta' in p[2] for p in parts)
    assert all(p[:2] == (1, 1) for p in parts)


def test_page_validation(library):
    lib, _ = library
    with pytest.raises(ValueError):
        lib.search_page('x', offset=-1)
    with pytest.raises(ValueError):
        lib.search_page('x' * 1001)
    assert lib.search_page('!!!')['items'] == []


def test_standalone_projection_version_migration(library, monkeypatch):
    from knowbase import sources
    import knowbase.document_library as module
    lib, _ = library
    sources.import_source(lib.repo, 'migration needle', title='Standalone', scope='x', fmt='text', parser_version='v1', imported_by='test')
    assert lib.reindex()['indexed'] == 1
    assert lib.reindex()['unchanged'] == 1
    monkeypatch.setattr(module, 'PARSER_VERSION', 'future-projection')
    assert lib.reindex()['indexed'] == 1
    assert lib.reindex()['unchanged'] == 1
    assert lib.search_page('migration')['total'] == 1


def test_question_retrieval_returns_specific_evidence(library):
    lib, root = library
    (root / "release.md").write_text(
        "# 安装运维\n\n更新失败时，执行回滚并恢复旧包。\n\n"
        "命令输出仅供排障。\n", encoding="utf-8")
    (root / "other.md").write_text(
        "# 数据恢复\n\n恢复备份之后检查项目配置。\n", encoding="utf-8")
    lib.sync(root, scope="x")
    hit = lib.search_page("升级失败后系统怎样恢复旧版本", scope="x")["items"][0]
    assert hit["path"] == "release.md"
    assert "执行回滚并恢复旧包" in hit["snippet"]
    read = lib.read(hit["document_id"], start_line=hit["start_line"], end_line=hit["end_line"])
    assert "执行回滚并恢复旧包" in read["text"]
    assert lib.search_page("火星水票 H9QQZ", scope="x")["total"] == 0


def test_version_question_finds_evidence_beyond_cover_and_abstains_without_evidence(library):
    lib, root = library
    before = "# 经营平台部署方案\n\nv4.2 评审稿\n\n" + "环境说明和部署约束。\n\n" * 100
    answer = (
        "### 四个后端的责任\n\n"
        "| 后端 | 职责 |\n|---|---|\n"
        "| 业务核心 | 管理订单 |\n"
        "| 调度中心 | 管理作业 |\n"
        "| 流程控制中心 | 管理审批 |\n"
        "| 风控中心 | 执行准入 |\n"
    )
    (root / "经营平台-v4.2.md").write_text(before + answer, encoding="utf-8")
    (root / "经营平台-v4.1.md").write_text(answer, encoding="utf-8")
    lib.sync(root, scope="ops")
    result = lib.search_page("按 v4.2 方案，后台拆成哪几块，各管什么？", scope="ops")
    assert result["total"] == 1
    hit = result["items"][0]
    assert hit["path"] == "经营平台-v4.2.md"
    assert all(role in hit["snippet"] for role in ("管理订单", "管理作业", "管理审批", "执行准入"))
    read = lib.read(hit["document_id"], sha256=hit["sha256"], start_line=hit["start_line"], end_line=hit["end_line"])
    assert hit["snippet"] == read["text"]
    assert lib.search_page("经营平台的量子点阵额度在哪儿开通？", scope="ops")["items"] == []
