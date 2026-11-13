"""Paged discovery and explicitly pinned historical read HTTP contracts."""
import json
import threading

import pytest

from knowbase.document_browser import _HTML, create_server
from test_document_browser import request


class Library:
    calls = []
    def __init__(self, repo):
        pass
    def search_page(self, query, **kwargs):
        self.calls.append(('search_page', query, kwargs))
        return {'items': [{'document_id': 'abc', 'title': '<script>bad</script>'}], 'total': 71, **kwargs}
    def history(self, identifier):
        self.calls.append(('history', identifier))
        return [{'sha256': 'a' * 64, 'current': False}]
    def read(self, identifier, **kwargs):
        self.calls.append(('read', identifier, kwargs))
        return {'id': identifier, 'sha256': kwargs['sha256'], 'historical': True,
                'citation': 'snapshot:a:L1-L2', 'related_cards': [{'title': '<img src=x>'}]}


@pytest.fixture
def server(tmp_path):
    Library.calls.clear()
    instance = create_server(tmp_path, port=0, library_factory=Library)
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance
    instance.shutdown()
    instance.server_close()
    thread.join(2)


def test_paged_search_contract(server):
    status, _, body = request(server, '/api/search?q=needle&offset=30&limit=30&scope=test')
    assert status == 200
    assert json.loads(body)['total'] == 71
    assert Library.calls == [('search_page', 'needle', {'scope': 'test', 'offset': 30, 'limit': 30})]


def test_history_and_explicit_historical_snapshot(server):
    status, _, body = request(server, '/api/history?id=abc')
    assert status == 200
    assert not json.loads(body)['items'][0]['current']
    status, _, body = request(server, '/api/document?id=abc&historical=1&sha256='+'a'*64)
    assert status == 200
    assert json.loads(body)['historical']
    assert Library.calls[-1][-1]['historical'] is True


@pytest.mark.parametrize('path', [
    '/api/search?q=x&offset=-1', '/api/search?q=x&offset=1.5',
    '/api/search?q=x&offset=10000000', '/api/document?id=abc&historical=true',
    '/api/document?id=abc&historical=1', '/api/history?id=../../etc/passwd',
    '/api/history?id=abc&path=/tmp',
])
def test_invalid_paging_and_history(server, path):
    assert request(server, path)[0] == 400
    assert not Library.calls


def test_ui_keeps_untrusted_material_plain_text():
    assert 'innerHTML' not in _HTML
    for identifier in ['result-pages', 'versions', 'copy-citation', 'related-cards']:
        assert f'id="{identifier}"' in _HTML
    assert '历史快照，内容不代表当前源文件' in _HTML
    assert 'ticket !== readRequest' in _HTML
