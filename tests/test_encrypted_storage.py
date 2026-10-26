import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_storage_refuses_unmounted_or_wrong_identity_without_creating_plaintext(tmp_path, monkeypatch):
    storage = load_script('encrypted_storage')
    settings = {'mount': tmp_path / 'open', 'app': tmp_path / 'app', 'storage_id': 'expected'}
    monkeypatch.setattr(storage, 'mounted', lambda _: False)
    with pytest.raises(RuntimeError, match='not_mounted'):
        storage.check(settings)
    assert not list(tmp_path.iterdir())
    settings['mount'].mkdir()
    (settings['mount'] / '.storage-id').write_text('wrong')
    monkeypatch.setattr(storage, 'mounted', lambda _: True)
    with pytest.raises(RuntimeError, match='identity_mismatch'):
        storage.check(settings)


def test_storage_rejects_readable_or_symlinked_key(tmp_path):
    storage = load_script('encrypted_storage')
    tmp_path.chmod(0o700)
    key = tmp_path / 'secret.key'
    key.write_bytes(b'test-only-password')
    key.chmod(0o644)
    with pytest.raises(RuntimeError, match='permissions'):
        storage.private_file(key)
    key.chmod(0o600)
    storage.private_file(key)
    link = tmp_path / 'link'
    link.symlink_to(key)
    with pytest.raises(RuntimeError, match='permissions'):
        storage.private_file(link)


def test_key_cannot_be_inside_the_app_or_encrypted_backup(tmp_path):
    storage = load_script('encrypted_storage')
    tmp_path.chmod(0o700)
    config = tmp_path / 'storage.json'
    value = {name: str(tmp_path / name) for name in
             ('app', 'cipher', 'mount', 'key_file', 'binary', 'bootstrap', 'auth_source')}
    value['key_file'] = str(tmp_path / 'app/hidden/key')
    storage.write_private(config, json.dumps(value))
    with pytest.raises(RuntimeError, match='separate'):
        storage.load(config)


def test_copy_preserves_content_modes_and_links_without_copying_external_secrets(tmp_path):
    storage = load_script('encrypted_storage')
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'data').mkdir(mode=0o700)
    (source / 'data/note.txt').write_text('private note')
    (source / 'data/note.txt').chmod(0o600)
    secret = tmp_path / 'shared-auth'
    secret.write_bytes(b'external credential')
    (source / 'auth.json').symlink_to(secret)
    destination = tmp_path / 'destination'
    manifest = storage.copy_verified(source, destination)
    assert manifest == storage.tree_manifest(destination)
    assert (destination / 'auth.json').is_symlink()
    assert os.readlink(destination / 'auth.json') == str(secret)
    (destination / 'data/note.txt').write_text('corrupted')
    assert storage.tree_manifest(destination) != manifest
    with pytest.raises(RuntimeError, match='destination_exists'):
        storage.copy_verified(source, destination)


def test_encrypted_app_root_allows_release_but_nested_symlink_stays_rejected(tmp_path):
    guard = load_script('deploy_guard')
    actual = tmp_path / 'open/application'
    actual.mkdir(parents=True)
    logical = tmp_path / 'app'
    logical.symlink_to(actual)
    (actual / 'code.py').write_text('safe source')
    assert guard.safe_path(logical, 'code.py') == actual / 'code.py'
    (actual / 'leak.py').symlink_to(tmp_path / 'secret')
    with pytest.raises(RuntimeError, match='symlink'):
        guard.safe_path(logical, 'leak.py')


def test_private_logs_are_not_emitted_to_journal_and_release_reads_only_new_warnings(tmp_path):
    log = tmp_path / '.logs/bot.log'
    code = ('from voice_memory_bot.cli import configure_logging; import logging; '
            'configure_logging(); logging.warning("private-log-marker")')
    env = {**os.environ, 'VOICE_MEMORY_LOG_PATH': str(log),
           'PYTHONPATH': str(Path(__file__).parents[1] / 'src')}
    result = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True, check=True)
    assert 'private-log-marker' in log.read_text()
    assert 'private-log-marker' not in result.stdout + result.stderr
    log.write_text('2026-01-01 00:00:00,000 WARNING bot old\n'
                   '2026-10-26 10:00:00,000 INFO bot started bot_id=123 folder_mode=custom\n'
                   '2026-10-26 10:00:01,000 ERROR bot new\n')
    guard = load_script('deploy_guard').ReleaseGuard(tmp_path, 'a' * 40)
    assert 'started bot_id=' in guard.service_log('2026-10-26T10:00:00+00:00')
    assert guard.service_log('2026-10-26T10:00:00+00:00', warnings=True).endswith('ERROR bot new')
    assert 'old' not in guard.service_log('2026-10-26T10:00:00+00:00', warnings=True)


@pytest.mark.parametrize('failure', ['copy', 'start'])
def test_migration_failure_never_discards_original_or_replaces_live_database(tmp_path, monkeypatch, failure):
    import hashlib
    import sqlite3

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / 'scripts'))
    migration = load_script('migrate_encrypted_storage')
    app, mount = tmp_path / 'app', tmp_path / 'open'
    app.mkdir()
    mount.mkdir()
    (app / 'data').mkdir()
    (app / 'code.py').write_bytes(b'original code')
    with sqlite3.connect(app / 'data/archive.sqlite3') as db:
        db.execute('CREATE TABLE notes (text)')
        db.execute("INSERT INTO notes VALUES ('original private note')")
    sha = 'a' * 40
    (app / 'release.json').write_text(json.dumps({'commit': sha, 'files': {
        'code.py': hashlib.sha256(b'original code').hexdigest()}}))
    inventory = {'quick_check': 'ok', 'foreign_keys': [], 'note_ids': ['n1'], 'counts': {'notes': 1}}

    class Guard:
        def __init__(self, *_):
            pass

        def busy(self):
            return 0

        def inventory(self):
            return inventory

    calls = []

    def run(args):
        calls.append(args)
        if failure == 'start' and args[2] == 'start':
            # Emulate a message saved by the new process before it failed.
            with sqlite3.connect(app / 'data/archive.sqlite3') as db:
                db.execute("INSERT INTO notes VALUES ('newly received note')")
            raise RuntimeError('simulated_start_failure')
        return b''

    monkeypatch.setattr(migration, 'ReleaseGuard', Guard)
    monkeypatch.setattr(migration, 'check', lambda *args, **kwargs: None)
    monkeypatch.setattr(migration, 'configure_bot', lambda *args: None)
    monkeypatch.setattr(migration, 'run', run)
    monkeypatch.setattr(migration.time, 'sleep', lambda _: None)
    if failure == 'copy':
        def fail_copy(*args):
            raise RuntimeError('simulated_copy_failure')
        monkeypatch.setattr(migration, 'copy_verified', fail_copy)
    with pytest.raises(RuntimeError, match='simulated_'):
        migration.migrate({'app': app, 'mount': mount}, sha, tmp_path / 'config/storage.json')
    with sqlite3.connect(app / 'data/archive.sqlite3') as db:
        rows = db.execute('SELECT text FROM notes').fetchall()
    assert rows[0][0] == 'original private note'
    if failure == 'copy':
        assert not app.is_symlink()
        assert calls[-1][2] == 'start'
    else:
        assert app.is_symlink()
        assert rows[-1][0] == 'newly received note'
        assert list(tmp_path.glob('app.pre-encryption-*'))
        assert list(mount.glob('migration/*/application-before.tar.gz'))
