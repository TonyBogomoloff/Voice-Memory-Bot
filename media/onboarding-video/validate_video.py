"""Inspect and fully decode the rendered promo; save a portable evidence receipt."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path


def sha256(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def main():
    video = Path(sys.argv[1]).resolve()
    receipt = Path(sys.argv[2]).resolve()
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-count_frames', '-show_streams', '-show_format',
        '-of', 'json', str(video),
    ], capture_output=True, text=True, check=True)
    metadata = json.loads(result.stdout)
    streams = metadata['streams']
    assert len(streams) == 1 and streams[0]['codec_type'] == 'video', 'silent_video_expected'
    stream = streams[0]
    assert stream['codec_name'] == 'h264', 'wrong_codec'
    assert (stream['width'], stream['height']) == (720, 1280), 'wrong_size'
    assert stream['pix_fmt'] == 'yuv420p', 'wrong_pixel_format'
    assert stream['avg_frame_rate'] == '30/1', 'wrong_frame_rate'
    assert int(stream['nb_read_frames']) == 900, 'wrong_frame_count'
    assert abs(float(metadata['format']['duration']) - 30) < 0.01, 'wrong_duration'
    assert stream['color_space'] == 'bt709', 'wrong_color_space'
    data = video.read_bytes()
    assert 0 <= data.find(b'moov') < data.find(b'mdat'), 'faststart_missing'
    decode = subprocess.run([
        'ffmpeg', '-hide_banner', '-v', 'error', '-xerror', '-i', str(video),
        '-map', '0:v:0', '-f', 'null', '-',
    ], capture_output=True, text=True, check=True)
    assert not decode.stderr.strip(), 'decode_errors'
    root = Path(__file__).resolve().parent
    sources = [root / 'package.json', root / 'pnpm-lock.yaml', root / 'remotion.config.ts']
    sources += [p for folder in ('src', 'public') for p in (root / folder).rglob('*') if p.is_file()]
    evidence = {
        'file': video.name,
        'sha256': sha256(video),
        'bytes': video.stat().st_size,
        'duration_seconds': 30,
        'width': 720,
        'height': 1280,
        'frames': 900,
        'fps': 30,
        'codec': 'h264',
        'pixel_format': 'yuv420p',
        'color_space': 'bt709',
        'audio': False,
        'faststart': True,
        'full_decode': 'passed',
        'source_hashes': {str(p.relative_to(root)): sha256(p) for p in sorted(sources)},
    }
    receipt.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in evidence.items() if k != 'source_hashes'}))


if __name__ == '__main__':
    main()
