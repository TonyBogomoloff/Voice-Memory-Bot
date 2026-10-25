import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from voice_memory_bot.config import Limits
from voice_memory_bot.store import Store


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


def test_failed_start_restores_code_but_keeps_new_database_and_originals(release, monkeypatch):
    r = release
    starts = 0

    def run(args, **kwargs):
        nonlocal starts
        r.calls.append(args)
        if args[:3] == ['systemctl', '--user', 'start']:
            starts += 1
            if starts == 1:
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
    assert (r.original.parent / 'new.bin').read_bytes() == b'new user media'
    assert r.original.read_bytes() == b'original bytes'
    assert (r.guard.app / 'config.json').read_text() == 'private configuration'


def test_successful_release_writes_verified_receipt_and_keeps_backup(release):
    r = release
    r.guard.deploy()
    receipt = json.loads((r.guard.app / 'deploy-receipt.json').read_text())
    assert receipt['commit'] == r.guard.sha and receipt['matches_manifest']
    assert receipt['configuration_unchanged'] and receipt['archive_preserved_during_swap']
    assert receipt['before_counts'] == receipt['after_counts']
    assert (Path(receipt['backup']) / 'archive.sqlite3').is_file()
    assert (r.guard.app / 'app.py').read_text() == 'new source'
    assert r.original.read_bytes() == b'original bytes'
