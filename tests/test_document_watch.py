import threading

import pytest

from knowbase.document_library import DocumentLibrary
from knowbase.document_watch import DirectoryWatch


@pytest.fixture
def setup(tmp_path):
    directory = tmp_path / 'input'
    directory.mkdir()
    return DirectoryWatch(DocumentLibrary(tmp_path / 'library')), directory


def test_persistent_configuration_and_retained_snapshots(setup):
    watcher, directory = setup
    (directory / 'a.txt').write_text('hello')
    (directory / 'skip.txt').write_text('excluded')
    root = watcher.register(directory, scope='work', exclude=['skip*'], interval=2)
    watcher = DirectoryWatch(DocumentLibrary(watcher.library.repo))
    assert watcher.roots()[0]['exclude'] == ['skip*']
    events = []
    result = watcher.run(once=True, emit=events.append)
    assert result['results'][root['id']]['indexed'] == 1
    assert any(e['event'] == 'sync_progress' for e in events)
    assert watcher.roots()[0]['last_result']['indexed'] == 1
    assert watcher.remove(root['id'])['snapshots_retained']
    assert watcher.library.search('hello')
    assert not watcher.roots()


def test_retry_and_cancellation(setup, monkeypatch):
    watcher, directory = setup
    root = watcher.register(directory, scope='a')
    calls = []
    def flaky(*args, **kw):
        calls.append(1)
        return {'errors': ['temporary']} if len(calls) == 1 else {'errors': []}
    monkeypatch.setattr(watcher.library, 'sync', flaky)
    class NoWait(threading.Event):
        def wait(self, timeout=None):
            return self.is_set()
    result = watcher.run(once=True, stop=NoWait())
    assert len(calls) == 2
    assert not result['results'][root['id']]['errors']
    stop = threading.Event()
    stop.set()
    assert watcher.run(once=True, stop=stop)['cancelled']
    assert len(calls) == 2


def test_cancel_before_mutation(setup):
    watcher, directory = setup
    (directory / 'a.txt').write_text('hello')
    with pytest.raises(KeyboardInterrupt):
        watcher.library.sync(directory, scope='a', cancelled=lambda: True)
    assert not watcher.library.list_documents()


def test_cancel_rolls_back_projection(setup):
    watcher, directory = setup
    (directory / 'a.txt').write_text('hello')
    state = {'cancel': False}
    def progress(event):
        if event['phase'] == 'complete':
            raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        watcher.library.sync(directory, scope='a', progress=progress)
    assert not watcher.library.list_documents()


def test_validation_and_upsert(setup):
    watcher, directory = setup
    root = watcher.register(directory, scope='a')
    assert watcher.register(directory, scope='a', exclude=['*.md'])['id'] == root['id']
    assert len(watcher.roots()) == 1
    for interval in (0, 3601, float('nan'), float('inf')):
        with pytest.raises(ValueError):
            watcher.register(directory, scope='a', interval=interval)
    with pytest.raises(ValueError):
        watcher.register(watcher.library.repo, scope='a')
    link = directory.parent / 'link'
    link.symlink_to(directory)
    with pytest.raises(ValueError):
        watcher.register(link, scope='a')


def test_cli_saved_validation(setup, monkeypatch, capsys):
    import argparse
    from knowbase import document_cli
    watcher, directory = setup
    monkeypatch.setattr(document_cli.config, 'repo_path', lambda: watcher.library.repo)
    (watcher.library.repo / '.git').mkdir()
    parser = argparse.ArgumentParser()
    document_cli.add_parser(parser.add_subparsers())
    assert document_cli.run(parser.parse_args(['documents', 'register', str(directory), '--scope', 'x'])) == 0
    assert document_cli.run(parser.parse_args(['documents', 'sync', '--saved'])) == 0
    assert document_cli.run(parser.parse_args(['documents', 'sync', str(directory)])) == 2
    assert document_cli.run(parser.parse_args(['documents', 'sync', '--saved', '--scope', 'x'])) == 2


def test_replaced_root_symlink_not_followed(setup):
    watcher, directory = setup
    root = watcher.register(directory, scope='x')
    other = directory.parent / 'other'
    other.mkdir()
    (other / 'secret.txt').write_text('outside data')
    directory.rmdir()
    directory.symlink_to(other)
    result = watcher.sync_root(root, retries=0)
    assert result['errors']
    assert not watcher.library.list_documents()


def test_cancel_via_progress_before_commit(setup):
    watcher, directory = setup
    (directory / 'a.txt').write_text('hello')
    stop = threading.Event()
    def progress(event):
        if event['phase'] == 'complete':
            stop.set()
    with pytest.raises(KeyboardInterrupt):
        watcher.library.sync(directory, scope='a', progress=progress, cancelled=stop.is_set)
    assert not watcher.library.list_documents()


def test_replaced_intermediate_root_component(setup):
    watcher, directory = setup
    nested = directory / 'nested'
    nested.mkdir()
    root = watcher.register(nested, scope='x')
    old = directory.with_name('old')
    directory.rename(old)
    outside = directory.with_name('outside')
    (outside / 'nested').mkdir(parents=True)
    (outside / 'nested' / 'a.txt').write_text('outside data')
    directory.symlink_to(outside)
    result = watcher.sync_root(root, retries=0)
    assert result['errors']
    assert not watcher.library.list_documents()


def test_transient_relative_directory(setup, monkeypatch):
    watcher, directory = setup
    (directory / 'a.txt').write_text('hello')
    monkeypatch.chdir(directory.parent)
    result = watcher.sync_root({'id': 'transient', 'directory': directory.name, 'scope': 'a', 'exclude': []}, retries=0)
    assert result['indexed'] == 1


def test_cancellation_during_retry_persists(setup, monkeypatch):
    watcher, directory = setup
    root = watcher.register(directory, scope='x')
    monkeypatch.setattr(watcher.library, 'sync', lambda *a, **kw: {'errors': ['retry']})
    stop = threading.Event()
    def emit(event):
        if event['event'] == 'retry_wait':
            stop.set()
    result = watcher.sync_root(root, stop=stop, emit=emit)
    assert result['cancelled']
    assert watcher.roots()[0]['last_result']['cancelled']


def test_unregister_queued_root_is_not_imported(setup):
    watcher, directory = setup
    first = directory / 'a'
    second = directory / 'b'
    first.mkdir()
    second.mkdir()
    (first / 'a.txt').write_text('first')
    (second / 'b.txt').write_text('second')
    root1 = watcher.register(first, scope='x')
    root2 = watcher.register(second, scope='x')
    def emit(event):
        if event['event'] == 'sync_finished' and event['id'] == root1['id']:
            watcher.remove(root2['id'])
    result = watcher.run(once=True, emit=emit)
    assert result['results'][root2['id']]['cancelled']
    assert not result['cancelled']
    assert [d['path'] for d in watcher.library.list_documents()] == ['a.txt']


def test_unregister_active_scan_cancels_only_that_root(setup):
    watcher, directory = setup
    first = directory / 'a'
    second = directory / 'b'
    first.mkdir()
    second.mkdir()
    (first / 'a.txt').write_text('first')
    (second / 'b.txt').write_text('second')
    root1 = watcher.register(first, scope='x')
    root2 = watcher.register(second, scope='x')
    def emit(event):
        if event['event'] == 'sync_progress' and event['id'] == root1['id'] and event['phase'] == 'prepared':
            watcher.remove(root1['id'])
    result = watcher.run(once=True, emit=emit)
    assert result['results'][root1['id']]['unregistered']
    assert result['results'][root2['id']]['indexed'] == 1
    assert not result['cancelled']
    assert [d['path'] for d in watcher.library.list_documents()] == ['b.txt']


def test_unregister_on_start_does_not_import(setup):
    watcher, directory = setup
    (directory / 'a.txt').write_text('first')
    root = watcher.register(directory, scope='x')
    def emit(event):
        if event['event'] == 'sync_started':
            watcher.remove(root['id'])
    assert watcher.sync_root(root, emit=emit)['unregistered']
    assert not watcher.library.list_documents()
