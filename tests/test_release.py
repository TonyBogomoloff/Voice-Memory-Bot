import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_memory_bot.config import Limits
from voice_memory_bot.store import Store
from voice_memory_bot.video_cache import CACHE_NAME, VideoFileCache


@pytest.fixture
def release(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('deploy_guard', Path(__file__).parents[1] / 'scripts/deploy_guard.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'data').mkdir()
    db = Store(app / 'data/archive.sqlite3', Limits())
    folder = db.ensure_user(1)
    note, _ = db.add_note(1, folder, {'message_id': 1}, 'text', transcript='before')
    db.update_note(1, note['id'], state='ready', delivery='sent', transcript_delivery='sent')
    db.close()
    (app / 'app.py').write_text('old source')
    (app / 'config.json').write_text('private configuration')
    original = app / 'data/notes/1/original.bin'
    original.parent.mkdir(parents=True)
    original.write_bytes(b'original bytes')
    old = {'commit': 'a' * 40, 'files': {'app.py': module.digest(app / 'app.py')}}
    (app / 'release.json').write_text(json.dumps(old))
    guard = module.ReleaseGuard(app, 'b' * 40)
    candidate = guard.candidate
    candidate.mkdir(parents=True)
    (candidate / 'app.py').write_text('new source')
    (candidate / 'added.py').write_text('new module')
    new = {'commit': guard.sha, 'base_commit': old['commit'],
           'files': {name: module.digest(candidate / name) for name in ('app.py', 'added.py')}}
    (candidate / 'release.json').write_text(json.dumps(new))
    validation = {'commit': guard.sha, 'manifest_sha256': module.digest(candidate / 'release.json'),
                  'full_suite': True, 'ruff': True, 'migration_rehearsal': True}
    (candidate / 'validation.json').write_text(json.dumps(validation))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == 'journalctl':
            return SimpleNamespace(stdout='' if '-p' in args else 'started bot_id=123 folder_mode=custom')
        return SimpleNamespace(stdout='ActiveState=active\nSubState=running\nNRestarts=0\n')

    monkeypatch.setattr(module, 'run', run)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    return SimpleNamespace(module=module, guard=guard, old=old, new=new, calls=calls, original=original)


def test_release_rejects_changed_production_before_stopping_service(release):
    r = release
    changed = {**r.old, 'commit': 'c' * 40}
    (r.guard.app / 'release.json').write_text(json.dumps(changed))
    with pytest.raises(RuntimeError, match='concurrent_source_change'):
        r.guard.deploy()
    assert r.calls == [] and (r.guard.app / 'app.py').read_text() == 'old source'
    assert not (r.guard.app / '.deploy-backups').exists()


@pytest.mark.parametrize('problem', ['hash', 'symlink', 'protected', 'validation'])
def test_release_rejects_unverified_or_unsafe_candidate(release, problem):
    r, candidate = release, release.guard.candidate
    if problem == 'hash':
        (candidate / 'app.py').write_text('changed after testing')
    elif problem == 'symlink':
        (candidate / 'app.py').unlink()
        (candidate / 'app.py').symlink_to(r.guard.app / 'config.json')
    elif problem == 'protected':
        r.new['files']['data/archive.sqlite3'] = r.module.digest(r.guard.db)
        (candidate / 'release.json').write_text(json.dumps(r.new))
        validation = json.loads((candidate / 'validation.json').read_text())
        validation['manifest_sha256'] = r.module.digest(candidate / 'release.json')
        (candidate / 'validation.json').write_text(json.dumps(validation))
    else:
        (candidate / 'validation.json').write_text('{}')
    with pytest.raises(RuntimeError):
        r.guard.deploy()
    assert not r.calls and r.original.read_bytes() == b'original bytes'


def test_work_arriving_during_shutdown_resumes_without_replacing_files(release, monkeypatch):
    r = release
    answers = iter([0, 0, 0, 1])
    monkeypatch.setattr(r.guard, 'busy', lambda: next(answers))
    with pytest.raises(RuntimeError, match='work_arrived_during_shutdown'):
        r.guard.deploy()
    assert [args[2] for args in r.calls] == ['stop', 'start']
    assert (r.guard.app / 'app.py').read_text() == 'old source'
    assert not (r.guard.app / 'added.py').exists()


@pytest.mark.parametrize('schema_upgrade', [None, 3, 4, 5])
def test_failed_start_restores_code_but_keeps_new_database_and_originals(release, monkeypatch, schema_upgrade):
    r = release
    starts = 0
    if schema_upgrade:
        with sqlite3.connect(r.guard.db) as db:
            if schema_upgrade == 3:
                db.execute('DROP TABLE obsidian_pairing')
                db.execute('DROP TABLE obsidian_devices')
            db.execute('DROP TABLE trash_cleanup')
            if schema_upgrade < 5:
                db.execute('DROP INDEX one_trash_per_owner')
                db.execute('ALTER TABLE folders DROP COLUMN is_trash')
            db.execute(f'PRAGMA user_version={schema_upgrade}')

    def run(args, **kwargs):
        nonlocal starts
        r.calls.append(args)
        if args[:3] == ['systemctl', '--user', 'start']:
            starts += 1
            if starts == 1:
                Store(r.guard.db, Limits()).close()
                with sqlite3.connect(r.guard.db) as db:
                    db.execute("UPDATE notes SET transcript='saved after new startup'")
                (r.original.parent / 'new.bin').write_bytes(b'new user media')
                raise RuntimeError('startup_failed')
        return SimpleNamespace(stdout='')

    monkeypatch.setattr(r.module, 'run', run)
    with pytest.raises(RuntimeError, match='startup_failed'):
        r.guard.deploy()
    assert starts == 2 and (r.guard.app / 'app.py').read_text() == 'old source'
    assert not (r.guard.app / 'added.py').exists()
    assert json.loads((r.guard.app / 'release.json').read_text()) == r.old
    with sqlite3.connect(r.guard.db) as db:
        assert db.execute('SELECT transcript FROM notes').fetchone()[0] == 'saved after new startup'
        if schema_upgrade:
            assert db.execute('PRAGMA user_version').fetchone()[0] == schema_upgrade
            assert db.execute("SELECT name FROM sqlite_master WHERE name='obsidian_devices'").fetchone()
    assert (r.original.parent / 'new.bin').read_bytes() == b'new user media'
    assert r.original.read_bytes() == b'original bytes'
    assert (r.guard.app / 'config.json').read_text() == 'private configuration'


def test_successful_release_writes_verified_receipt_and_keeps_backup(release):
    r = release
    cache = VideoFileCache(r.guard.app / 'data' / CACHE_NAME, '123')
    cache.put('content-hash', 'telegram-file-id')
    r.guard.deploy()
    receipt = json.loads((r.guard.app / 'deploy-receipt.json').read_text())
    assert receipt['commit'] == r.guard.sha and receipt['matches_manifest']
    assert receipt['configuration_unchanged'] and receipt['archive_preserved_during_swap']
    assert receipt['before_counts'] == receipt['after_counts']
    assert (Path(receipt['backup']) / 'archive.sqlite3').is_file()
    assert VideoFileCache(Path(receipt['backup']) / CACHE_NAME, '123').get('content-hash') == 'telegram-file-id'
    assert cache.get('content-hash') == 'telegram-file-id'
    assert (r.guard.app / 'app.py').read_text() == 'new source'
    assert r.original.read_bytes() == b'original bytes'


@pytest.mark.parametrize('prepared', [False, True])
def test_video_cache_readiness_is_required_before_stopping_service(release, monkeypatch, prepared):
    r = release
    for name in ('output/remotion/help/help-save-v1-ru.mp4', 'scripts/prepare_video_cache.py'):
        path = r.guard.candidate / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'candidate file')
        r.new['files'][name] = r.module.digest(path)
    (r.guard.candidate / 'release.json').write_text(json.dumps(r.new))
    validation = json.loads((r.guard.candidate / 'validation.json').read_text())
    validation.update(manifest_sha256=r.module.digest(r.guard.candidate / 'release.json'), video_cache=prepared)
    (r.guard.candidate / 'validation.json').write_text(json.dumps(validation))
    def run(args, **kwargs):
        r.calls.append(args)
        assert args[1].endswith('/scripts/prepare_video_cache.py') and args[-1] == '--check'
        raise RuntimeError('video_cache_not_ready')
    monkeypatch.setattr(r.module, 'run', run)
    with pytest.raises(RuntimeError, match='video_cache_'):
        r.guard.deploy()
    assert len(r.calls) == int(prepared)
    assert not any(args[0] == 'systemctl' for args in r.calls)
    assert (r.guard.app / 'app.py').read_text() == 'old source'



def test_trash_schema_rollback_refuses_unexpected_schema_change(release, tmp_path):
    r = release
    backup = tmp_path / 'before-v6.sqlite3'
    with sqlite3.connect(r.guard.db) as db:
        db.execute('DROP TABLE trash_cleanup')
        db.execute('PRAGMA user_version=5')
        with sqlite3.connect(backup) as snapshot:
            db.backup(snapshot)
    Store(r.guard.db, Limits()).close()
    with sqlite3.connect(r.guard.db) as db:
        db.execute('ALTER TABLE notes ADD COLUMN unexpected TEXT')
    with pytest.raises(RuntimeError, match='rollback_schema_not_additive'):
        r.module.restore_additive_schema_marker(r.guard.db, backup)
    with sqlite3.connect(r.guard.db) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 6
