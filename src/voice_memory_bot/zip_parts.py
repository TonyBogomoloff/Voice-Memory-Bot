"""Streaming ZIP parts with checksums and all-or-nothing output cleanup."""
import hashlib
import json
import zipfile


def write_parts(items, output, metadata, directories, part_digits, part_bytes):
    prefix = json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))[:-1].encode() + b',"files":['
    suffix = b"]}\n"
    manifest_name = "manifest-" + "0" * part_digits + ".json"
    def entry_size(name, size):
        # Local header + central directory entry; UTF-8 filenames appear twice.
        return 76 + 2 * len(name.encode("utf-8")) + size
    base_size = (22 + sum(entry_size(name, 0) for name in directories)
                 + entry_size(manifest_name, len(prefix) + len(suffix)))
    if base_size > part_bytes:
        raise ValueError("export_part_too_large")
    paths, handle, used, records = [], None, base_size, []
    def start():
        nonlocal handle, used, records
        path = output / f"voice-memory-{metadata['content']}-{len(paths) + 1:03d}.zip"
        handle = zipfile.ZipFile(path, "x", compression=zipfile.ZIP_STORED)
        paths.append(path)
        used, records = base_size, []
        for name in directories:
            handle.writestr(name, b"")
    def finish():
        nonlocal handle
        if handle:
            handle.writestr(f"manifest-{len(paths):0{part_digits}d}.json", prefix + b",".join(records) + suffix)
            handle.close()
            handle = None
    try:
        for name, data, record in items:
            record["path"] = name
            if isinstance(data, bytes):
                record.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
            size = entry_size(name, record["bytes"]) + len(encoded)
            if base_size + size > part_bytes:
                raise ValueError("export_document_too_large")
            if handle and used + size + bool(records) > part_bytes:
                finish()
            if handle is None:
                start()
            if isinstance(data, bytes):
                handle.writestr(name, data)
            else:
                digest, copied = hashlib.sha256(), 0
                with data.open("rb") as original, handle.open(name, "w") as target:
                    while chunk := original.read(1024 * 1024):
                        copied += len(chunk)
                        if copied > record["bytes"]:
                            raise ValueError("archive_audio_checksum_mismatch")
                        digest.update(chunk)
                        target.write(chunk)
                if copied != record["bytes"] or digest.hexdigest() != record["sha256"]:
                    raise ValueError("archive_audio_checksum_mismatch")
            used += size + bool(records)
            records.append(encoded)
        if not paths:
            start()
        finish()
        if any(path.stat().st_size > part_bytes for path in paths):
            raise ValueError("export_part_too_large")
        return paths
    except BaseException:
        if handle:
            handle.close()
        for path in paths:
            path.unlink(missing_ok=True)
        raise
