"""Render and validate the eight bundled Help examples from the shared Remotion UI."""
import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOPICS = ('save', 'folders', 'ai', 'images', 'search', 'actions', 'export', 'settings')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser-executable', default='/usr/bin/google-chrome')
    parser.add_argument('--topics', nargs='+', choices=TOPICS, default=TOPICS)
    args = parser.parse_args()
    output = ROOT.parents[1] / 'output' / 'remotion' / 'help'
    output.mkdir(parents=True, exist_ok=True)
    for topic in args.topics:
        path = output / f'help-{topic}-v2-square-ru.mp4'
        subprocess.run([
            str(ROOT / 'node_modules/.bin/remotion'), 'render', 'src/help-index.tsx', f'Help-{topic}',
            str(path), '--codec=h264', '--crf=19', '--pixel-format=yuv420p', '--concurrency=2',
            '--chrome-mode=chrome-for-testing', f'--browser-executable={args.browser_executable}',
        ], cwd=ROOT, check=True)
        subprocess.run(['python3', str(ROOT / 'validate_video.py'), str(path),
                        str(path.with_suffix('.json')), '--duration=10', '--shape=square'], check=True)


if __name__ == '__main__':
    main()
