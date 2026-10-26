"""Guarded code-only release on the configured host; runtime data is never replaced."""
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def safe_path(root, name):
    root = Path(root).resolve()
    path = Path(name)
    protected = {'data', 'config.json', '.venv', '.git', '.deploy-candidates', '.deploy-backups',
                 '.deploy.lock', 'release.json', 'validation.json', '.env'}
    if (not path.parts or path.is_absolute() or '..' in path.parts or path.parts[0] in protected
            or (len(path.parts) == 1 and name.endswith('receipt.json'))):
        raise RuntimeError('unsafe_manifest_path')
    target = root / path
    if any(p.is_symlink() for p in (target, *target.parents) if p != root.parent):
        raise RuntimeError('symlink_in_manifest_path')
    if not target.resolve().is_relative_to(root.resolve()):
        raise RuntimeError('unsafe_manifest_path')
    return target


def verify(root, manifest):
    if not manifest.get('files'):
        raise RuntimeError('empty_manifest')
    for name, expected in manifest['files'].items():
        path = safe_path(root, name)
        if not path.is_file() or digest(path) != expected:
            raise RuntimeError('source_mismatch:' + name)


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='.' + path.name + '.', dir=path.parent, delete=False) as handle:
        part = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(part, path)
        finally:
            part.unlink(missing_ok=True)


def restore_additive_schema_marker(database, backup):
    """Version 4 only adds connector tables; allow v3 code to reopen the live DB.

    Preserve all rows, including writes since startup. Never restore the backup DB.
    Refuse to downgrade when any pre-existing schema object has changed.
    """
    with closing(sqlite3.connect(backup.as_uri() + '?mode=ro', uri=True)) as old, closing(
            sqlite3.connect(database)) as current:
        previous = old.execute('PRAGMA user_version').fetchone()[0]
        version = current.execute('PRAGMA user_version').fetchone()[0]
        if (previous, version) != (3, 4):
            return
        query = ("SELECT type,name,tbl_name,sql FROM sqlite_master "
                 "WHERE tbl_name NOT IN ('obsidian_pairing','obsidian_devices') ORDER BY name")
        if old.execute(query).fetchall() != current.execute(query).fetchall():
            raise RuntimeError('rollback_schema_not_additive')
        current.execute('PRAGMA user_version=3')
        current.commit()



class ReleaseGuard:
    def __init__(self, app, sha, service='voice-memory-bot.service'):
        if len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha):
            raise ValueError('full_commit_sha_required')
        self.app, self.sha, self.service = Path(app), sha, service
        self.candidate = self.app / '.deploy-candidates' / sha
        self.db = self.app / 'data/archive.sqlite3'


    @contextmanager
    def database(self):
        with closing(sqlite3.connect(self.db.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            yield db


    def busy(self):
        with self.database() as db:
            queries = [
                "SELECT count(*) FROM notes WHERE deleted=0 AND (state IN ('queued','processing') OR delivery IN ('sending','pending') OR transcript_delivery='sending' OR archive_dirty=1)",
                "SELECT count(*) FROM updates WHERE state IN ('pending','processing')",
                "SELECT count(*) FROM ai_requests WHERE state!='deleted' AND (state IN ('queued','running') OR delivery IN ('pending','sending'))",
                "SELECT count(*) FROM note_moves WHERE state IN ('publishing','cleanup')",
                "SELECT count(*) FROM deletion_jobs WHERE files_pending=1",
            ]
            for table in ('ai_note_jobs', 'ai_message_jobs'):
                if db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    queries.append(f"SELECT count(*) FROM {table} WHERE state='pending'")
            return sum(db.execute(query).fetchone()[0] for query in queries) + len(list((self.app / 'data/exports').glob('export-*')))


    def inventory(self):
        with self.database() as db:
            tables = ('folders', 'notes', 'updates', 'ai_requests')
            return {
                'counts': {t: db.execute('SELECT count(*) FROM ' + t).fetchone()[0] for t in tables},
                'rows': {t: hashlib.sha256(json.dumps([dict(r) for r in db.execute('SELECT * FROM ' + t + ' ORDER BY rowid')], sort_keys=True).encode()).hexdigest() for t in tables},
                'note_ids': [r[0] for r in db.execute('SELECT id FROM notes ORDER BY id')],
                'quick_check': db.execute('PRAGMA quick_check').fetchone()[0],
                'foreign_keys': [tuple(r) for r in db.execute('PRAGMA foreign_key_check')],
                'archive': {str(p.relative_to(self.app)): digest(p) for p in (self.app / 'data/notes').rglob('*') if p.is_file()},
            }


    def service_log(self, since, *, warnings=False):
        private = self.app / '.logs/bot.log'
        if private.exists():
            threshold = datetime.fromisoformat(since).astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')
            lines = []
            for path in private.parent.glob('bot.log*'):
                for line in path.read_text(errors='replace').splitlines():
                    if line[:19] >= threshold and (not warnings or any(
                            level in line for level in (' WARNING ', ' ERROR ', ' CRITICAL '))):
                        lines.append(line)
            return '\n'.join(sorted(lines))
        args = ['journalctl', '--user', '-u', self.service, '--since', since,
                '--no-pager', '--quiet', '-o', 'cat']
        if warnings:
            args += ['-p', 'warning']
        return run(args, capture_output=True).stdout


    def deploy(self):
        with (self.app / '.deploy.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            old = json.loads((self.app / 'release.json').read_text())
            new = json.loads((self.candidate / 'release.json').read_text())
            if old['commit'] != new['base_commit'] or new['commit'] != self.sha:
                raise RuntimeError('concurrent_source_change')
            validation = json.loads((self.candidate / 'validation.json').read_text())
            if (validation.get('commit') != self.sha
                    or validation.get('manifest_sha256') != digest(self.candidate / 'release.json')
                    or not all(validation.get(key) is True for key in ('full_suite', 'ruff', 'migration_rehearsal'))):
                raise RuntimeError('candidate_validation_missing_or_stale')
            verify(self.app, old)
            verify(self.candidate, new)
            cache_check = None
            if any(name.startswith('output/remotion/help/') and name.endswith('.mp4') for name in new['files']):
                if validation.get('video_cache') is not True or 'scripts/prepare_video_cache.py' not in new['files']:
                    raise RuntimeError('video_cache_preparation_missing')
                # Check the real persistent cache against this candidate's exact assets
                # before stopping the service. This command cannot upload anything.
                checked = run([str(self.app / '.venv/bin/python'), str(self.candidate / 'scripts/prepare_video_cache.py'),
                               '--app', str(self.app), '--check'], capture_output=True,
                              env={**os.environ, 'PYTHONPATH': str(self.candidate / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'})
                cache_check = json.loads(checked.stdout)
                if cache_check.get('ready') is not True or cache_check.get('uploaded') != 0:
                    raise RuntimeError('video_cache_not_ready')
            for name in set(new['files']) - set(old['files']):
                if safe_path(self.app, name).exists():
                    raise RuntimeError('untracked_source_conflict:' + name)
            stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
            backup = self.app / '.deploy-backups' / (stamp + '-release-' + old['commit'][:12])
            backup.mkdir(parents=True)
            with tarfile.open(backup / 'sources.tar.gz', 'w:gz') as archive:
                for name in old['files']:
                    archive.add(self.app / name, arcname=name)
                archive.add(self.app / 'release.json', arcname='release.json')
            for receipt in self.app.glob('*receipt.json'):
                if receipt.is_file():
                    shutil.copy2(receipt, backup / receipt.name)
            protected = {p: digest(p) for p in (self.app / 'config.json', self.app.parent / 'config.json') if p.is_file()}
            stable = 0
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                stable = stable + 1 if self.busy() == 0 else 0
                if stable >= 3:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('processing_not_idle_no_restart')
            if json.loads((self.app / 'release.json').read_text())['commit'] != old['commit']:
                raise RuntimeError('concurrent_source_change')
            verify(self.app, old)
            changed = False
            try:
                run(['systemctl', '--user', 'stop', self.service])
                if self.busy():
                    raise RuntimeError('work_arrived_during_shutdown_no_files_changed')
                before = self.inventory()
                if before['quick_check'] != 'ok' or before['foreign_keys']:
                    raise RuntimeError('database_integrity_failed_before_deploy')
                with self.database() as source, closing(sqlite3.connect(backup / 'archive.sqlite3')) as dest:
                    source.backup(dest)
                media_cache = self.app / 'data/telegram-video-cache.sqlite3'
                if media_cache.is_file():
                    with closing(sqlite3.connect(media_cache.resolve().as_uri() + '?mode=ro', uri=True)) as source, closing(
                            sqlite3.connect(backup / media_cache.name)) as dest:
                        source.backup(dest)
                (backup / 'before.json').write_text(json.dumps(before, indent=2))
                verify(self.app, old)
                verify(self.candidate, new)
                changed = True
                for name in new['files']:
                    atomic(self.app / name, (self.candidate / name).read_bytes())
                for name in set(old['files']) - set(new['files']):
                    retired = self.app / name
                    retired.unlink()
                    if retired.suffix == '.py':
                        for cached in (retired.parent / '__pycache__').glob(retired.stem + '.*.pyc'):
                            cached.unlink()
                atomic(self.app / 'release.json', (self.candidate / 'release.json').read_bytes())
                if self.inventory() != before:
                    raise RuntimeError('archive_changed_while_service_stopped')
                started_at = datetime.now(UTC).isoformat()
                run(['systemctl', '--user', 'start', self.service])
                for _ in range(60):
                    output = self.service_log(started_at)
                    if 'started bot_id=' in output and 'folder_mode=custom' in output:
                        break
                    time.sleep(.5)
                else:
                    raise RuntimeError('bot_startup_not_confirmed')
                time.sleep(2)
                state = run(['systemctl', '--user', 'show', self.service, '-p', 'ActiveState', '-p', 'SubState', '-p', 'NRestarts'], capture_output=True).stdout
                if 'ActiveState=active' not in state or 'SubState=running' not in state or 'NRestarts=0' not in state:
                    raise RuntimeError('service_not_running')
                verify(self.app, new)
                if any(digest(p) != h for p, h in protected.items()):
                    raise RuntimeError('protected_configuration_changed')
                after = self.inventory()
                if after['quick_check'] != 'ok' or after['foreign_keys'] or not set(before['note_ids']) <= set(after['note_ids']):
                    raise RuntimeError('archive_integrity_failed_after_startup')
                log = self.service_log(started_at, warnings=True)
                if log.strip():
                    raise RuntimeError('new_warning_or_error_in_service_log')
                receipt = {'commit': self.sha, 'previous_commit': old['commit'], 'installed_at': stamp,
                           'matches_manifest': True, 'configuration_unchanged': True, 'archive_preserved_during_swap': True,
                           'before_counts': before['counts'], 'after_counts': after['counts'], 'sqlite_integrity': 'ok',
                           'service': state.strip().splitlines(), 'telegram_startup': 'ok',
                           'archive_files': len(after['archive']), 'new_warnings_errors': 0, 'queue_idle': self.busy() == 0, 'backup': str(backup)}
                if cache_check:
                    receipt['video_cache'] = cache_check
                (backup / 'receipt.json').write_text(json.dumps(receipt, indent=2))
                atomic(self.app / 'deploy-receipt.json', json.dumps(receipt, indent=2).encode())
                print(json.dumps(receipt), flush=True)
            except BaseException:
                if changed:
                    run(['systemctl', '--user', 'stop', self.service])
                    with tarfile.open(backup / 'sources.tar.gz') as archive:
                        archive.extractall(self.app, filter='data')
                    for name in set(new['files']) - set(old['files']):
                        (self.app / name).unlink(missing_ok=True)
                    restore_additive_schema_marker(self.db, backup / 'archive.sqlite3')
                run(['systemctl', '--user', 'start', self.service])
                print('code_rolled_back_database_preserved' if changed else 'no_files_changed_service_resumed', flush=True)
                raise



def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sha')
    parser.add_argument('--app', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    ReleaseGuard(args.app, args.sha).deploy()


if __name__ == '__main__':
    main()
