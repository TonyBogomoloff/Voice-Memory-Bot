"""One-time, guarded migration of the entire installed app into encrypted storage."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from deploy_guard import ReleaseGuard, verify
from encrypted_storage import (
    BOT_SERVICE,
    CONFIG,
    check,
    configure_bot,
    copy_verified,
    load,
    run,
    tree_manifest,
    write_private,
)


def backup_verified(source, path, manifest):
    with tarfile.open(path, 'w:gz') as archive:
        archive.add(source, arcname='application', recursive=True)
    checked = set()
    with tarfile.open(path, 'r:gz') as archive:
        for entry in archive:
            if entry.name == 'application':
                continue
            name = str(Path(entry.name).relative_to('application'))
            expected = manifest[name]
            if entry.isfile() or entry.islnk():
                with archive.extractfile(entry) as handle:
                    actual = hashlib.file_digest(handle, 'sha256').hexdigest()
                if expected['kind'] != 'file' or actual != expected['sha256']:
                    raise RuntimeError('encrypted_rollback_file_mismatch')
            elif entry.issym():
                if expected['kind'] != 'link' or entry.linkname != expected['target']:
                    raise RuntimeError('encrypted_rollback_link_mismatch')
            elif not entry.isdir() or expected['kind'] != 'directory':
                raise RuntimeError('encrypted_rollback_type_mismatch')
            checked.add(name)
    if checked != set(manifest):
        raise RuntimeError('encrypted_rollback_incomplete')


def migrate(settings, sha, config=CONFIG):
    check(settings, app=False)
    app, target = settings['app'], settings['mount'] / 'application'
    if app.is_symlink() or target.exists():
        raise RuntimeError('migration_already_started_use_receipt_for_recovery')
    guard = ReleaseGuard(app, sha)
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')
    legacy = app.with_name(app.name + '.pre-encryption-' + stamp)
    records = settings['mount'] / 'migration' / stamp
    records.mkdir(parents=True, mode=0o700)
    state_file = config.parent / 'migration-state.json'

    def state(phase):
        write_private(state_file, json.dumps({'phase': phase, 'commit': sha,
            'app': str(app), 'legacy': str(legacy), 'target': str(target),
            'encrypted_records': str(records)}, indent=2) + '\n')

    with (app / '.deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release = json.loads((app / 'release.json').read_text())
        if release['commit'] != sha:
            raise RuntimeError('concurrent_source_change')
        verify(app, release)
        stable, deadline = 0, time.monotonic() + 180
        while time.monotonic() < deadline:
            stable = stable + 1 if guard.busy() == 0 else 0
            if stable >= 3:
                break
            time.sleep(1)
        else:
            raise RuntimeError('processing_not_idle_no_restart')
        run(['systemctl', '--user', 'stop', BOT_SERVICE])
        swapped = False
        try:
            if guard.busy():
                raise RuntimeError('work_arrived_during_shutdown')
            verify(app, release)
            before = guard.inventory()
            if before['quick_check'] != 'ok' or before['foreign_keys']:
                raise RuntimeError('source_database_integrity_failed')
            protected = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (app / 'config.json', app.parent / 'config.json') if p.is_file()}
            state('copying')
            manifest = copy_verified(app, target)
            write_private(records / 'files.json', json.dumps(manifest, sort_keys=True))
            write_private(records / 'database-before.json', json.dumps(before, sort_keys=True))
            backup_verified(app, records / 'application-before.tar.gz', manifest)
            # A concurrent candidate preparation may not honor .deploy.lock: detect it too.
            if tree_manifest(app) != manifest:
                raise RuntimeError('source_changed_during_migration')
            with closing(sqlite3.connect((target / 'data/archive.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise RuntimeError('destination_database_integrity_failed')
            # Serialize future releases through the copied lock as soon as the symlink is visible.
            with (target / '.deploy.lock').open('a') as new_lock:
                fcntl.flock(new_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                state('ready_to_switch')
                app.rename(legacy)
                try:
                    app.symlink_to(target, target_is_directory=True)
                except BaseException:
                    legacy.rename(app)
                    raise
                swapped = True
                state('switched')
                check(settings)
                configure_bot(settings, config)
                if guard.inventory() != before:
                    raise RuntimeError('archive_changed_during_storage_migration')
                if any(hashlib.sha256(p.read_bytes()).hexdigest() != h for p, h in protected.items()):
                    raise RuntimeError('protected_config_changed')
                started = datetime.now(UTC).isoformat()
                run(['systemctl', '--user', 'start', BOT_SERVICE])
                for _ in range(100):
                    log = guard.service_log(started)
                    if 'started bot_id=' in log and 'folder_mode=custom' in log:
                        break
                    time.sleep(.5)
                else:
                    raise RuntimeError('encrypted_bot_startup_not_confirmed')
                time.sleep(2)
                service = run(['systemctl', '--user', 'show', BOT_SERVICE,
                               '-p', 'ActiveState', '-p', 'SubState', '-p', 'NRestarts']).decode()
                if not all(v in service for v in ('ActiveState=active', 'SubState=running', 'NRestarts=0')):
                    raise RuntimeError('encrypted_bot_not_running')
                after = guard.inventory()
                if (after['quick_check'] != 'ok' or after['foreign_keys']
                        or not set(before['note_ids']) <= set(after['note_ids'])):
                    raise RuntimeError('archive_integrity_failed_after_migration')
                if guard.service_log(started, warnings=True).strip():
                    raise RuntimeError('new_bot_warnings_after_migration')
                verify(app, release)
                if tree_manifest(legacy) != manifest:
                    raise RuntimeError('legacy_copy_changed_do_not_remove')
                # The rollback archive was fully read and hash-verified inside the encrypted mount.
                state('verified_removing_plaintext_duplicate')
                shutil.rmtree(legacy)
                receipt = {'commit': sha, 'migrated_at': stamp, 'encrypted_storage': True,
                    'all_files_verified': len(manifest), 'file_bytes_verified': sum(
                        v.get('size', 0) for v in manifest.values()), 'configs_preserved': True,
                    'before_counts': before['counts'], 'after_counts': after['counts'],
                    'sqlite_integrity': 'ok', 'plaintext_duplicate_removed': not legacy.exists(),
                    'encrypted_rollback': str(records / 'application-before.tar.gz'),
                    'service': service.strip().splitlines(), 'new_warnings_errors': 0}
                write_private(records / 'receipt.json', json.dumps(receipt, indent=2) + '\n')
                write_private(app / 'encryption-receipt.json', json.dumps(receipt, indent=2) + '\n')
                state('complete')
                return receipt
        except BaseException:
            state('encrypted_app_needs_attention' if swapped else 'original_app_preserved')
            if not swapped:
                run(['systemctl', '--user', 'start', BOT_SERVICE])
            # After a switch never restore a stale database over newly received messages.
            # Keep both copies and the verified encrypted rollback for explicit recovery.
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha', required=True)
    parser.add_argument('--config', type=Path, default=CONFIG)
    args = parser.parse_args()
    os.umask(0o077)
    print(json.dumps(migrate(load(args.config), args.expected_sha, args.config)), flush=True)


if __name__ == '__main__':
    main()
