"""Validate the committed, installable Obsidian release assets before publication."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path


def check(root, tag=None):
    plugin = root / 'plugins/obsidian'
    manifest = json.loads((plugin / 'manifest.json').read_text())
    package = json.loads((plugin / 'package.json').read_text())
    assert manifest['id'] == 'voice-memory' and manifest['isDesktopOnly'] is False
    assert package['version'] == manifest['version']
    assert tag is None or tag == manifest['version'], 'release_tag_must_match_manifest'
    for name in ('manifest.json', 'styles.css'):
        assert (plugin / name).read_bytes() == (plugin / 'dist' / name).read_bytes()
    names = ('main.js', 'manifest.json', 'styles.css')
    with zipfile.ZipFile(plugin / 'dist/voice-memory.zip') as archive:
        assert set(archive.namelist()) == {*(f'voice-memory/{name}' for name in names), 'voice-memory/INSTALL.txt'}
        assert archive.testzip() is None
        for name in names:
            assert archive.read('voice-memory/' + name) == (plugin / 'dist' / name).read_bytes()
    return {'version': manifest['version'], 'assets': {
        name: hashlib.sha256((plugin / 'dist' / name).read_bytes()).hexdigest()
        for name in (*names, 'voice-memory.zip')}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag')
    args = parser.parse_args()
    print(json.dumps(check(Path(__file__).resolve().parents[1], args.tag)))
