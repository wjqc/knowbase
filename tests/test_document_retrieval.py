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
