import pytest
from knowbase.document_library import DocumentLibrary


def test_history_pinned_read_and_unrelated_hash(tmp_path):
    root = tmp_path / 'inputs'
    root.mkdir()
    lib = DocumentLibrary(tmp_path / 'repo')
    file = root / 'a.txt'
    file.write_text('original evidence\nsecond line\n')
    lib.sync(root, scope='project')
    old = lib.list_documents()[0]
    file.write_text('changed evidence\n')
    lib.sync(root, scope='project')
    with pytest.raises(ValueError, match='snapshot changed'):
        lib.read(old['id'], sha256=old['sha256'])
    result = lib.read(old['id'], sha256=old['sha256'], historical=True, start_line=1, end_line=1)
    assert result['text'] == 'original evidence\n'
    assert result['stale'] and result['historical']
    assert old['sha256'] in result['citation']
    assert result['source_ref']['locator'] == {'type': 'line', 'start_line': 1, 'end_line': 1}
    assert len(lib.history(old['id'])) == 2
    assert all(v['created_at'] for v in lib.history(old['id']))
    with pytest.raises(ValueError, match='does not belong'):
        lib.read(old['id'], sha256='a'*64, historical=True)
    file.unlink()
    lib.sync(root, scope='project')
    assert lib.read(old['id'], sha256=old['sha256'], historical=True)['text'].startswith('original')


def test_related_cards_are_published_scope_and_version_specific(tmp_path):
    from knowbase import store
    root = tmp_path / 'inputs'
    root.mkdir()
    (root / 'a.txt').write_text('evidence\n')
    lib = DocumentLibrary(tmp_path / 'repo')
    lib.sync(root, scope='project')
    doc = lib.list_documents()[0]
    first = lib.read(doc['id'])
    ref = {'source_id': first['source_id'], 'sha256': first['sha256']}
    records = [
        ('pitfalls', 'P-2026-0001', 'project', 'active', ref),
        ('pitfalls', 'P-2026-0002', 'other', 'active', ref),
        ('pitfalls', 'P-2026-0003', 'project', 'active', dict(ref, sha256='0'*64)),
        ('staging', 'P-2026-0004', 'project', 'active', ref),
        ('pitfalls', 'P-2026-0005', 'project', 'archived', ref),
    ]
    for directory, card_id, scope, status, source_ref in records:
        folder = lib.repo / directory
        folder.mkdir(exist_ok=True)
        meta = {'id': card_id, 'scope': scope, 'status': status, 'type': 'pitfall',
                'title': card_id, 'source_refs': [source_ref]}
        (folder / f'{card_id}.md').write_text(store.render(meta, 'Example'), encoding='utf-8')
    assert [c['id'] for c in lib.read(doc['id'])['related_cards']] == ['P-2026-0001']


def test_history_does_not_trust_mutated_manifest(tmp_path):
    from knowbase import sources
    import yaml
    root = tmp_path / 'inputs'
    root.mkdir()
    lib = DocumentLibrary(tmp_path / 'repo')
    file = root / 'a.txt'
    file.write_text('original history\n')
    lib.sync(root, scope='project')
    doc = lib.list_documents()[0]
    old = lib.read(doc['id'])
    file.write_text('new snapshot\n')
    lib.sync(root, scope='project')
    new = lib.read(doc['id'])
    path = lib.repo / sources.MANIFESTS_DIR / (old['source_id'] + '.yaml')
    manifest = yaml.safe_load(path.read_text())
    manifest['sha256'] = new['sha256']
    path.write_text(yaml.safe_dump(manifest))
    pinned = lib.read(doc['id'], sha256=old['sha256'], historical=True)
    assert pinned['text'] == old['text']
    assert {v['sha256'] for v in lib.history(doc['id'])} == {old['sha256'], new['sha256']}


def test_migration_only_pins_existing_current_version(tmp_path):
    root = tmp_path / 'inputs'
    root.mkdir()
    repo = tmp_path / 'repo'
    lib = DocumentLibrary(repo)
    file = root / 'a.txt'
    file.write_text('old content')
    lib.sync(root, scope='x')
    old = lib.list_documents()[0]
    file.write_text('current content')
    lib.sync(root, scope='x')
    current = lib.list_documents()[0]
    with lib._connect() as conn:
        conn.execute('DROP TABLE source_snapshot_versions')
        conn.execute('DELETE FROM schema_migrations WHERE version=38')
    migrated = DocumentLibrary(repo)
    assert [v['sha256'] for v in migrated.history(old['id'])] == [current['sha256']]
    with pytest.raises(ValueError, match='does not belong'):
        migrated.read(old['id'], sha256=old['sha256'], historical=True)
