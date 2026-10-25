"""Download and transcribe originals without Telegram presentation or routing."""
import asyncio
import hashlib
import json

from .errors import ProviderError


def fingerprint(path):
    with path.open("rb") as handle:
        checksum = hashlib.file_digest(handle, "sha256").hexdigest()
    return path.stat().st_size, checksum


class MediaProcessor:
    def __init__(self, store, archive, telegram, groq, limits):
        self.store, self.archive = store, archive
        self.telegram, self.groq, self.limits = telegram, groq, limits

    async def capture(self, note):
        path = self.archive.audio(note)
        if path.is_file():
            size, checksum = await asyncio.to_thread(fingerprint, path)
        else:
            size, checksum = await self.telegram.download(note["file_id"], path, self.limits.max_file_bytes)
        # Persist the original receipt before any optional processing can fail.
        self.store.update_note(note["owner"], note["id"], audio_bytes=size, sha256=checksum)
        return path, size

    async def transcribe(self, note):
        if note["file_id"] and note["transcript"] is None:
            path, size = await self.capture(note)
            if size > 24_000_000:
                raise ProviderError("asr_file_too_large")
            duration = await self.probe_duration(path)
            self.store.reserve_asr(note["owner"], duration)
            transcript = await self.groq.transcribe(path)
            if not transcript:
                raise ProviderError("no_speech")
            self.store.update_note(note["owner"], note["id"], transcript=transcript)
            note = self.store.note(note["owner"], note["id"])
            self.archive.persist_text(note, self.store.folder(note["owner"], note["folder_id"]))
        return note

    async def probe_duration(self, path):
        process = await asyncio.create_subprocess_exec("ffprobe", "-v", "error", "-show_entries",
            "format=duration", "-of", "json", "-protocol_whitelist", "file,pipe",
            "-format_whitelist", "ogg,mp3,wav,flac,mov,matroska,webm,mpeg,aac",
            "-max_alloc", "67108864", "-probesize", "1048576", "-analyzeduration", "5000000", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 15)
        except (TimeoutError, asyncio.CancelledError):
            process.kill()
            await process.wait()
            raise
        try:
            duration = float(json.loads(stdout)["format"]["duration"])
            if process.returncode or not 0 < duration <= self.limits.max_audio_seconds:
                raise ValueError
            return max(1, int(duration + .999))
        except (ValueError, TypeError, KeyError):
            raise ProviderError("audio_duration_invalid") from None
