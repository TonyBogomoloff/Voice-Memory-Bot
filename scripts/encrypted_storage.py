"""Private gocryptfs storage lifecycle. This bootstrap lives outside the locked app.

No password or recovery key is ever printed. Run as the existing bot service user.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import stat
import subprocess
import time
from pathlib import Path

CONFIG = Path.home() / '.config/voice-memory-vault/storage.json'
SERVICE = 'voice-memory-storage.service'
BOT_SERVICE = 'voice-memory-bot.service'
FSNAME = 'voice-memory-storage'


def run(args):
    # gocryptfs -init prints a recovery key even when called noninteractively.
    # Never include subprocess output or command arguments in exceptions.
    result = subprocess.run([str(a) for a in args], capture_output=True)
    if result.returncode:
        raise RuntimeError('storage_command_failed_' + str(result.returncode))
    return result.stdout


def private_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o077):
        raise RuntimeError('storage_private_file_permissions')
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise RuntimeError('storage_private_directory_permissions')


def write_private(path, content, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, 'wb') as handle:
        handle.write(content.encode() if isinstance(content, str) else content)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def load(path=CONFIG):
    private_file(path)
    value = json.loads(path.read_text())
    for name in ('app', 'cipher', 'mount', 'key_file', 'binary', 'bootstrap', 'auth_source'):
        p = Path(value[name])
        if not p.is_absolute() or any(c in str(p) for c in '\n\r\x00"%'):
            raise RuntimeError('storage_invalid_path')
        value[name] = p
    for parent in (value['app'], value['cipher'], value['mount']):
        if value['key_file'].resolve().is_relative_to(parent.resolve()):
            raise RuntimeError('storage_key_must_be_separate')
    return value


def mounted(settings):
    target = str(settings['mount'])
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        before, after = line.split(' - ', 1)
        fields = after.split()
        actual = before.split()[4].replace('\\040', ' ').replace('\\134', '\\')
        if actual == target:
            return fields[0] == 'fuse.gocryptfs' and fields[1] == FSNAME
    return False


def check(settings, *, app=True):
    if not mounted(settings):
        raise RuntimeError('encrypted_storage_not_mounted')
    marker = settings['mount'] / '.storage-id'
    if not marker.is_file() or marker.read_text().strip() != settings['storage_id']:
        raise RuntimeError('encrypted_storage_identity_mismatch')
    if app:
        root = settings['app']
        if not root.is_symlink() or root.resolve() != settings['mount'] / 'application':
            raise RuntimeError('application_not_in_encrypted_storage')


def wait(settings):
    for _ in range(150):
        if mounted(settings):
            return
        time.sleep(.1)
    raise RuntimeError('encrypted_storage_mount_timeout')


def serve(settings):
    private_file(settings['key_file'])
    if not (settings['cipher'] / 'gocryptfs.conf').is_file():
        raise RuntimeError('encrypted_storage_not_initialized')
    if mounted(settings):
        # Recover only a disconnected mount left by our own crashed FUSE process.
        try:
            settings['mount'].stat()
        except OSError as exc:
            if exc.errno != 107:  # ENOTCONN
                raise
            run(['fusermount3', '-u', settings['mount']])
        else:
            raise RuntimeError('encrypted_storage_already_mounted')
    if any(settings['mount'].iterdir()):
        raise RuntimeError('encrypted_mountpoint_not_empty')
    args = [str(settings['binary']), '-fg', '-q', '-nosyslog', '-fsname', FSNAME,
            '-passfile', str(settings['key_file']), str(settings['cipher']), str(settings['mount'])]
    os.execv(args[0], args)


def init(config=CONFIG):
    if config.exists():
        settings = load(config)
        private_file(settings['key_file'])
        if not (settings['cipher'] / 'gocryptfs.conf').is_file():
            raise RuntimeError('storage_initialization_incomplete_do_not_replace_key')
        return settings
    root = Path.home() / '.local/share/voice-memory-storage'
    helper = Path.home() / '.local/libexec/voice-memory-storage'
    values = {'app': str(Path.home() / 'Desktop/_code/voice_memory_bot'),
              'cipher': str(root / 'cipher'), 'mount': str(root / 'open'),
              'key_file': str(config.parent / 'storage.key'), 'binary': str(helper / 'gocryptfs'),
              'bootstrap': str(helper / 'storage.py'),
              'auth_source': str(Path.home() / '.codex/auth.json'),
              'storage_id': secrets.token_hex(16), 'schema_version': 1}
    for p in (config.parent, root, root / 'cipher', root / 'open', helper):
        if p.is_symlink():
            raise RuntimeError('storage_directory_must_not_be_symlink')
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
        p.chmod(0o700)
    if any((root / 'cipher').iterdir()) or any((root / 'open').iterdir()):
        raise RuntimeError('storage_already_contains_files')
    write_private(Path(values['key_file']), secrets.token_urlsafe(48) + '\n', exclusive=True)
    write_private(config, json.dumps(values, indent=2) + '\n', exclusive=True)
    settings = load(config)
    run([settings['binary'], '-q', '-init', '-passfile', settings['key_file'], settings['cipher']])
    return settings


def install(settings, config=CONFIG):
    bootstrap = settings['bootstrap']
    if Path(__file__).resolve() != bootstrap.resolve():
        write_private(bootstrap, Path(__file__).read_bytes())
    unit = f'''[Unit]
Description=Encrypted storage for Voice Memory Bot
Before={BOT_SERVICE}

[Service]
Type=exec
ExecStart=/usr/bin/python3 "{bootstrap}" serve --config "{config}"
ExecStartPost=/usr/bin/python3 "{bootstrap}" wait --config "{config}"
ExecStop=/usr/bin/fusermount3 -u "{settings['mount']}"
TimeoutStartSec=30
TimeoutStopSec=60
Restart=on-failure
RestartSec=5
UMask=0077
LimitCORE=0
MemorySwapMax=0
StandardOutput=null
StandardError=journal

[Install]
WantedBy=default.target
'''
    units = Path.home() / '.config/systemd/user'
    write_private(units / SERVICE, unit)
    run(['systemctl', '--user', 'daemon-reload'])
    run(['systemctl', '--user', 'enable', '--now', SERVICE])
    wait(settings)
    marker = settings['mount'] / '.storage-id'
    if not marker.exists():
        write_private(marker, settings['storage_id'] + '\n', exclusive=True)
    check(settings, app=False)


def configure_bot(settings, config=CONFIG):
    check(settings)
    app = settings['app']
    for name in ('.private-tmp', '.logs', '.codex-runtime'):
        path = app / name
        path.mkdir(exist_ok=True, mode=0o700)
        path.chmod(0o700)
    auth = app / '.codex-runtime/auth.json'
    if not settings['auth_source'].is_file():
        raise RuntimeError('existing_codex_auth_missing')
    if auth.is_symlink():
        if auth.resolve() != settings['auth_source'].resolve():
            raise RuntimeError('unexpected_codex_auth_link')
    elif auth.exists():
        raise RuntimeError('refuse_to_replace_codex_auth')
    else:
        auth.symlink_to(settings['auth_source'])
    dropin = f'''[Unit]
Requires={SERVICE}
After={SERVICE}
BindsTo={SERVICE}
PartOf={SERVICE}

[Service]
ExecStartPre=/usr/bin/python3 "{settings['bootstrap']}" check --config "{config}"
Environment="TMPDIR={app / '.private-tmp'}"
Environment="CODEX_HOME={app / '.codex-runtime'}"
Environment="VOICE_MEMORY_LOG_PATH={app / '.logs/bot.log'}"
MemorySwapMax=0
LimitCORE=0
'''
    write_private(Path.home() / '.config/systemd/user' / (BOT_SERVICE + '.d') / 'storage.conf', dropin)
    run(['systemctl', '--user', 'daemon-reload'])


def tree_manifest(root):
    """Do not follow symlinks, including the app's existing venv and shared auth."""
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            p = Path(directory) / name
            rel = str(p.relative_to(root))
            info = p.lstat()
            if stat.S_ISLNK(info.st_mode):
                result[rel] = {'kind': 'link', 'target': os.readlink(p)}
            elif stat.S_ISDIR(info.st_mode):
                result[rel] = {'kind': 'directory', 'mode': stat.S_IMODE(info.st_mode)}
            elif stat.S_ISREG(info.st_mode):
                with p.open('rb') as handle:
                    digest = hashlib.file_digest(handle, 'sha256').hexdigest()
                result[rel] = {'kind': 'file', 'size': info.st_size, 'sha256': digest,
                               'mode': stat.S_IMODE(info.st_mode)}
            else:
                raise RuntimeError('unsupported_file_type_in_storage')
    return result


def copy_verified(source, destination):
    before = tree_manifest(source)
    if destination.exists():
        raise RuntimeError('migration_destination_exists')
    shutil.copytree(source, destination, symlinks=True)
    if tree_manifest(destination) != before or tree_manifest(source) != before:
        raise RuntimeError('migration_copy_verification_failed')
    return before


def status(settings):
    result = {'mounted': mounted(settings), 'key_file': str(settings['key_file']),
              'cipher_directory': str(settings['cipher']),
              'application_in_storage': settings['app'].is_symlink()
              and settings['app'].resolve() == settings['mount'] / 'application'}
    if result['mounted']:
        check(settings, app=result['application_in_storage'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('init', 'install', 'serve', 'wait', 'check', 'status', 'configure-bot'))
    parser.add_argument('--config', type=Path, default=CONFIG)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        settings = init(args.config) if args.mode == 'init' else load(args.config)
        if args.mode == 'serve':
            serve(settings)
        elif args.mode == 'wait':
            wait(settings)
        elif args.mode == 'install':
            install(settings, args.config)
        elif args.mode == 'configure-bot':
            configure_bot(settings, args.config)
        elif args.mode == 'check':
            check(settings)
        else:
            print(json.dumps(status(settings)))
    except Exception as exc:
        # No secret, provider response, subprocess output, or arbitrary path is printed.
        code = str(exc) if type(exc) is RuntimeError else type(exc).__name__
        print('encrypted_storage_error: ' + code)
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
