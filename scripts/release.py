"""Prepare and deploy an immutable commit to this project's established server."""
import argparse
import hashlib
import io
import json
import shlex
import subprocess
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
APP = '/home/tony/Desktop/_code/voice_memory_bot'
SSH = ['ssh', '-F', str(Path.home() / '.ssh/config'), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'my-server']


PREPARE_REMOTE = """
import fcntl,hashlib,json,os,pathlib,subprocess,sys,tarfile
candidate, app = map(pathlib.Path, sys.argv[1:3])
base = sys.argv[3]
os.umask(0o077)
with (app / '.deploy.lock').open('a') as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise RuntimeError('release_or_storage_operation_in_progress') from None
    if json.loads((app / 'release.json').read_text())['commit'] != base:
        raise RuntimeError('concurrent_source_change')
    # Never overwrite a candidate that may already have been validated.
    candidate.mkdir(parents=True, exist_ok=False)
    with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
        archive.extractall(candidate, filter='data')
    os.chdir(candidate)
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    os.environ['PYTHONPATH'] = str(candidate / 'src')
    private_tmp = app / '.private-tmp'
    private_tmp.mkdir(exist_ok=True, mode=0o700)
    os.environ['TMPDIR'] = str(private_tmp)
    python = str(app / '.venv/bin/python')
    subprocess.run([python,'-m','pytest','-q','-p','no:cacheprovider'],check=True)
    subprocess.run([python,'-m','ruff','check','--no-cache','src','tests','scripts'],check=True)
    subprocess.run([python,'scripts/check_migration.py','--database',str(app / 'data/archive.sqlite3')],check=True)
    manifest = (candidate / 'release.json').read_bytes()
    receipt = {'commit':json.loads(manifest)['commit'], 'manifest_sha256':hashlib.sha256(manifest).hexdigest(),
               'full_suite':True, 'ruff':True, 'migration_rehearsal':True}
    (candidate / 'validation.json').write_text(json.dumps(receipt,indent=2))
"""


def remote(args, **kwargs):
    return subprocess.run([*SSH, shlex.join(args)], check=True, **kwargs)


def full_sha(value):
    if len(value) != 40 or any(c not in '0123456789abcdef' for c in value):
        raise argparse.ArgumentTypeError('Укажи полный SHA коммита')
    return value


def prepare(sha, base):
    subprocess.run(['git', 'merge-base', '--is-ancestor', base, sha], cwd=REPO, check=True)
    code = "import json,pathlib,sys; print(json.loads(pathlib.Path(sys.argv[1]).read_text())['commit'])"
    current = remote(['python3', '-c', code, APP + '/release.json'], capture_output=True).stdout.decode().strip()
    if current != base:
        raise RuntimeError('concurrent_source_change')
    raw = subprocess.check_output(['git', 'archive', sha], cwd=REPO)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            if member.isfile():
                files[member.name] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            elif not member.isdir():
                raise RuntimeError('unsupported_archive_member')
    manifest = json.dumps({'commit': sha, 'base_commit': base, 'files': files}, indent=2).encode()
    package = io.BytesIO()
    with tarfile.open(fileobj=package, mode='w') as archive:
        with tarfile.open(fileobj=io.BytesIO(raw)) as source:
            for member in source:
                archive.addfile(member, source.extractfile(member) if member.isfile() else None)
        member = tarfile.TarInfo('release.json')
        member.size, member.mode = len(manifest), 0o600
        archive.addfile(member, io.BytesIO(manifest))
    candidate = APP + '/.deploy-candidates/' + sha
    # Preparation shares the deployment/migration lock, including extraction and tests.
    remote([APP + '/.venv/bin/python', '-c', PREPARE_REMOTE, candidate, APP, base], input=package.getvalue())



def deploy(sha, base):
    published = subprocess.check_output(['git', 'ls-remote', '--exit-code', 'origin', 'refs/heads/main'], cwd=REPO)
    if published.decode().split()[0] != sha:
        raise RuntimeError('candidate_not_current_origin_main')
    candidate = APP + '/.deploy-candidates/' + sha
    code = ("import json,pathlib,sys; m=json.loads(pathlib.Path(sys.argv[1]).read_text());"
            "assert m['base_commit']==sys.argv[2], 'candidate_base_mismatch'")
    remote(['python3', '-c', code, candidate + '/release.json', base])
    remote(['python3', candidate + '/scripts/deploy_guard.py', sha, '--app', APP])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('prepare', 'deploy'))
    parser.add_argument('sha', type=full_sha)
    parser.add_argument('base', type=full_sha, help='Production SHA из release.json')
    args = parser.parse_args()
    (prepare if args.mode == 'prepare' else deploy)(args.sha, args.base)


if __name__ == '__main__':
    main()
