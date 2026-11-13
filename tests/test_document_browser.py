"""The document browser never exposes a filesystem API or cross-origin access."""
import hashlib
import http.client
import json
import threading

import pytest

from knowbase.document_browser import _HTML, create_server


class Library:
    calls = []
    def __init__(self, repo):
        self.repo = repo
    def list_documents(self, **kwargs):
        self.calls.append(('list', kwargs))
        return [{"id": "doc-1", "title": "<script>alert(1)</script>"}]
    def search(self, query, **kwargs):
        self.calls.append(('search', query, kwargs))
        return [{"document_id": "doc-1", "snippet": "原文", "start_line": 2, "end_line": 3}]
    def read(self, identifier, **kwargs):
        self.calls.append(('read', identifier, kwargs))
        return {"id": identifier, "text": "<img src=x onerror=alert(1)>", "sha256": "a" * 64,
                "stale": kwargs.get('sha256') == 'b' * 64}


@pytest.fixture
def browser(tmp_path):
    Library.calls.clear()
    server = create_server(tmp_path, port=0, library_factory=Library)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def request(server, path='/', method='GET', headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
    conn.request(method, path, headers=headers or {})
    response = conn.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    conn.close()
    return result


def test_loopback_only(tmp_path):
    for host in ('0.0.0.0', 'localhost', 'evil.example', '::'):
        with pytest.raises(ValueError):
            create_server(tmp_path, host=host, library_factory=Library)


def test_page_is_self_contained_and_escaped(browser):
    status, headers, body = request(browser)
    assert status == 200
    assert '文档资料库' in body.decode()
    assert "frame-ancestors 'none'" in headers['Content-Security-Policy']
    assert "script-src 'sha256-" in headers['Content-Security-Policy']
    assert headers['Cache-Control'] == 'no-store'
    assert 'innerHTML' not in _HTML
    assert '.textContent =' in _HTML
    assert 'https://' not in _HTML
    assert 'Access-Control-Allow-Origin' not in headers
    assert Library.calls == []


def test_read_routes_and_snapshot(browser):
    status, _, data = request(browser, '/api/documents?scope=demo&limit=10')
    assert status == 200
    assert json.loads(data)['items'][0]['title'] == '<script>alert(1)</script>'
    assert Library.calls[-1] == ('list', {'scope': 'demo', 'limit': 10})
    status, _, data = request(browser, '/api/search?q=hello&scope=demo')
    assert status == 200
    assert json.loads(data)['items'][0]['start_line'] == 2
    status, _, data = request(browser, '/api/document?id=doc-1&sha256='+'b'*64+'&start_line=2&end_line=4')
    assert status == 200
    assert json.loads(data)['stale'] is True
    assert Library.calls[-1][2]['start_line'] == 2


@pytest.mark.parametrize('path', [
    '/api/document?path=/etc/passwd', '/api/document?id=../../etc/passwd',
    '/api/document?id=x&sha256=garbage', '/api/documents?limit=-1',
    '/api/documents?limit=201', '/api/search?q=', '/api/search?q='+('x'*501),
    '/api/search?q=hi&q=again', '/api/document?id=x&start_line=3&end_line=2',
])
def test_bad_parameters_never_reach_library(browser, path):
    status, _, _ = request(browser, path)
    assert status == 400
    assert Library.calls == []


def test_rebinding_and_cross_origin_denied(browser):
    for headers in ({'Host': 'evil.example'}, {'Origin': 'https://evil.example'},
                    {'Host': 'localhost:'+str(browser.server_port)}):
        status, _, _ = request(browser, '/api/documents', headers=headers)
        assert status == 403
    assert Library.calls == []


def test_no_writes_or_arbitrary_paths(browser):
    assert request(browser, '/etc/passwd')[0] == 404
    for method in ('POST', 'PUT', 'DELETE', 'PATCH'):
        assert request(browser, '/api/document?id=doc-1', method=method)[0] == 405
    assert Library.calls == []


def test_stale_response_does_not_masquerade_as_latest(browser):
    _, _, data = request(browser, '/api/document?id=doc-1&sha256='+'b'*64)
    payload = json.loads(data)
    assert payload['stale']
    assert payload['sha256'] != 'b'*64
    assert '源文件或索引版本已变化' in _HTML
    assert 'ticket !== readRequest' in _HTML
    assert 'ticket !== listRequest' in _HTML


def test_real_library_import_search_read_and_stale(tmp_path):
    from knowbase.document_library import DocumentLibrary
    repo = tmp_path / 'memory'
    documents = tmp_path / 'documents'
    documents.mkdir()
    source = documents / 'plan.md'
    source.write_text('# Sample\n\nAn original source needle.\n', encoding='utf-8')
    library = DocumentLibrary(repo)
    library.sync(documents, scope='example')
    server = create_server(repo, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, body = request(server, '/api/search?q=needle&scope=example')
        assert status == 200
        hit = json.loads(body)['items'][0]
        path = '/api/document?id='+hit['document_id']+'&sha256='+hit['sha256']
        status, _, body = request(server, path)
        assert status == 200
        assert not json.loads(body)['stale']
        assert 'original source needle' in json.loads(body)['text']
        source.write_text('# Changed\n\nA new version.\n', encoding='utf-8')
        assert json.loads(request(server, path)[2])['stale']
        library.sync(documents, scope='example')
        assert request(server, path)[0] == 400  # Old line locator must not render new text.
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
