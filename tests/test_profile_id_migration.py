import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from profile_os.api import create_app
from profile_os.migrate_profile_id import migrate
from profile_os.storage import Store
from profile_os.access import AccessControl
from profile_os.dynstores import DynamicStores
from profile_os.projects import Projects


def setup_profile(root):
    store = Store(root)
    store.create_profile('vertice', 'Siro', 'Identity', 'Career strategy')
    store.create_profile('hilo', 'Hilo', 'Fox', 'Maintainer')
    memory = store.remember('vertice', {'kind': 'note', 'content': 'Keep vertice in historical prose', 'tags': ['vertice']})
    message = store.send_message('hilo', 'vertice', 'Unread mail')
    store.send_message('vertice', 'hilo', 'Outgoing mail')
    store.write_file('vertice', 'SOUL.md', 'An elephant and his kite')
    dyn = DynamicStores(store)
    dyn.propose('vertice', 'career_data', 'Career', 'admin', {'fields': {
        'performer_profile_id': {'type': 'string'}, 'title': {'type': 'string'}}})
    dyn.approve('vertice', 'career_data')
    record = dyn.add_record('vertice', 'career_data', {
        'performer_profile_id': 'vertice', 'title': 'vertice'})
    projects = Projects(store)
    project = projects.propose_create('vertice', 'shared', 'Work', {'fields': {'title': {'type': 'string'}}})
    projects.approve_create(project['id'])
    projects.add_record('vertice', project['id'], {'title': 'keep'})
    access = AccessControl(store)
    p = access.create_principal('app', 'Siro worker')
    access.create_credential(p['id'], 'worker', 'siro-secret')
    for op in ['boot', 'search', 'remember', 'records:read', 'records:write']:
        access.grant(p['id'], op, profile_id='vertice')
    approval = access.propose_approval('project_join', p['id'],
        {'joining_profile_id': 'vertice', 'notes': 'vertice'}, profile_id='vertice')
    return store, memory, message, record, project, approval


def test_migration_preserves_identity_history_permissions_and_files(tmp_path):
    root = tmp_path / 'data'
    store, memory, message, record, project, approval = setup_profile(root)
    before = {str(p.relative_to(root/'profiles'/'vertice')): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (root/'profiles'/'vertice').rglob('*') if p.is_file()}
    store.close()
    result = migrate(root, 'vertice', 'siro')
    assert result['foreign_keys_ok']
    store = Store(root)
    assert [p['id'] for p in store.list_profiles()] == ['hilo', 'siro']
    assert store.resolve_profile('vertice')['resolved_profile_id'] == 'siro'
    assert store.canonical_profile_id('vertice') == 'siro'
    assert store.all_memories('siro')[0]['id'] == memory['id']
    assert store.all_memories('siro')[0]['tags'] == ['vertice']
    assert store.list_inbox('siro', True)[0]['id'] == message['id']
    assert store.list_inbox('siro', True)[0]['read_at'] is None
    data = DynamicStores(store).get_record('siro', 'career_data', record['id'])['data']
    assert data == {'performer_profile_id': 'siro', 'title': 'vertice'}
    assert Projects(store).is_member(project['id'], 'siro')
    payload = AccessControl(store).get_approval(approval['id'])['payload']
    assert payload == {'joining_profile_id': 'siro', 'notes': 'vertice'}
    assert before == {str(p.relative_to(root/'profiles'/'siro')): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (root/'profiles'/'siro').rglob('*') if p.is_file()}
    assert not (root/'profiles'/'vertice').exists()
    assert store.db.execute('PRAGMA foreign_key_check').fetchall() == []
    with pytest.raises(Exception, match='retired'):
        store.create_profile('vertice', 'Duplicate', 'x', 'x')
    app = create_app(data_dir=str(root), auth_enabled=True, do_seed=False)
    with TestClient(app) as client:
        auth = {'Authorization': 'Bearer siro-secret'}
        for profile in ['siro', 'vertice']:
            r = client.post(f'/profiles/{profile}/session', headers=auth)
            assert r.status_code == 200, r.text
            assert r.json()['selection']['profile_id'] == 'siro'
            assert client.get(f'/profiles/{profile}/files/SOUL.md', headers=auth).status_code == 200
            assert client.get('/projects/'+project['id']+'/records', params={'profile_id': profile}, headers=auth).status_code == 200
        # Old recipient and sender IDs continue to work and persist canonical IDs.
        r = client.post('/profiles/vertice/messages', json={'to_profile_id': 'vertice', 'content': 'loop'}, headers=auth)
        assert r.status_code == 201, r.text
        assert r.json()['from_profile_id'] == r.json()['to_profile_id'] == 'siro'
        assert client.post('/profiles/hilo/session', headers=auth).status_code == 403
        assert client.post('/profiles/vertice/session').status_code == 401


def test_existing_target_aborts_without_mutation(tmp_path):
    root = tmp_path / 'data'
    store, *_ = setup_profile(root)
    store.create_profile('siro', 'Collision', 'x', 'x')
    store.close()
    with pytest.raises(ValueError):
        migrate(root, 'vertice', 'siro')
    assert Store(root).get_profile('vertice')['id'] == 'vertice'
    assert (root/'profiles'/'vertice').exists()


def test_failure_after_sql_updates_rolls_back(tmp_path, monkeypatch):
    root = tmp_path / 'data'
    store, memory, *_ = setup_profile(root)
    store.close()
    from pathlib import Path
    original = Path.rename
    def fail(self, target):
        if self.name == 'vertice':
            raise OSError('simulated filesystem failure')
        return original(self, target)
    monkeypatch.setattr(Path, 'rename', fail)
    with pytest.raises(OSError, match='simulated'):
        migrate(root, 'vertice', 'siro')
    store = Store(root)
    assert store.get_profile('vertice')['id'] == 'vertice'
    assert store.all_memories('vertice')[0]['id'] == memory['id']
    assert store.db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert store.canonical_profile_id('vertice') == 'vertice'


def test_signed_old_path_authenticates_original_bytes(tmp_path):
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from profile_os.sign import sign_request
    root = tmp_path / 'data'
    store, *_ = setup_profile(root)
    access = AccessControl(store)
    principal = access.create_principal('app', 'Signed worker')
    private = Ed25519PrivateKey.generate()
    public = base64.b64encode(private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    key = access.add_public_key(principal['id'], 'key', public)
    access.grant(principal['id'], 'boot', profile_id='vertice')
    store.close()
    migrate(root, 'vertice', 'siro')
    with TestClient(create_app(data_dir=str(root), auth_enabled=True, do_seed=False)) as client:
        path = '/profiles/vertice/session'
        auth = sign_request(private, key['id'], 'POST', path)
        r = client.post(path, headers={'Authorization': auth})
        assert r.status_code == 200, r.text
        assert r.json()['selection']['profile_id'] == 'siro'


def test_failure_after_directory_move_compensates(tmp_path, monkeypatch):
    import profile_os.migrate_profile_id as migration
    root = tmp_path / 'data'
    store, *_ = setup_profile(root)
    store.close()
    original = migration._hashes
    calls = 0
    def wrong_after_move(directory):
        nonlocal calls
        calls += 1
        return original(directory) if calls == 1 else {'mismatch': 'simulated'}
    monkeypatch.setattr(migration, '_hashes', wrong_after_move)
    with pytest.raises(ValueError, match='hash'):
        migrate(root, 'vertice', 'siro')
    assert (root/'profiles'/'vertice').is_dir()
    assert not (root/'profiles'/'siro').exists()
    assert Store(root).get_profile('vertice')['id'] == 'vertice'
