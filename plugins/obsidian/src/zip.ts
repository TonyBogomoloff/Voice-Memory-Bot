import { Unzip, UnzipInflate } from 'fflate';
import { Attachment, checked, decode, FORMAT, MAX_BATCH, MAX_FILE, Note, safePath, Source, validateRecord } from './engine';

function checkDirectory(bytes: Uint8Array): void {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  // This export uses ordinary single-volume ZIP; reject truncation before unpacking.
  for (let end = bytes.length - 22; end >= Math.max(0, bytes.length - 65557); end--) {
    if (view.getUint32(end, true) !== 0x06054b50 || end + 22 + view.getUint16(end + 20, true) !== bytes.length) continue;
    const count = view.getUint16(end + 10, true), size = view.getUint32(end + 12, true);
    let pos = view.getUint32(end + 16, true);
    if (view.getUint32(end + 4, true) || count === 65535 || count !== view.getUint16(end + 8, true) || pos + size !== end) break;
    for (let n = 0; n < count; n++) {
      if (pos + 46 > end || view.getUint32(pos, true) !== 0x02014b50) throw new Error('ZIP повреждён или не завершён.');
      pos += 46 + view.getUint16(pos + 28, true) + view.getUint16(pos + 30, true) + view.getUint16(pos + 32, true);
    }
    if (pos === end) return;
    break;
  }
  throw new Error('ZIP повреждён или не завершён.');
}

export async function loadZips(inputs: Uint8Array[]): Promise<{ notes: Note[]; source: Source; pending: number }> {
  if (inputs.reduce((n, bytes) => n + bytes.length, 0) > MAX_BATCH) throw new Error('Выбери ZIP общим размером до 200 МБ.');
  const files = new Map<string, Uint8Array>();
  const records = new Map<string, Note | Attachment>();
  let total = 0, count = 0, schema = 0;
  for (const bytes of inputs) {
    checkDirectory(bytes);
    let failed: Error | null = null;
    const extracted = new Map<string, Uint8Array>();
    const active = new Set<string>();
    const unzip = new Unzip(file => {
      try {
        if (++count > 30000) throw new Error('Слишком много файлов в ZIP.');
        if (file.name.endsWith('/')) { safePath(file.name.slice(0, -1)); return; }
        const path = safePath(file.name);
        if (extracted.has(path) || active.has(path) || (file.originalSize ?? 0) > MAX_FILE) throw new Error('Повторный или слишком большой файл в ZIP.');
        active.add(path);
        let size = 0;
        const chunks: Uint8Array[] = [];
        file.ondata = (error, chunk, final) => {
          if (failed) return;
          if (error) { failed = error; return; }
          size += chunk.length; total += chunk.length;
          if (size > MAX_FILE || total > MAX_BATCH) { failed = new Error('Распакованный ZIP превышает лимит памяти.'); file.terminate(); return; }
          chunks.push(chunk);
          if (final) {
            const data = new Uint8Array(size); let offset = 0;
            for (const part of chunks) { data.set(part, offset); offset += part.length; }
            extracted.set(path, data); active.delete(path);
          }
        };
        file.start();
      } catch (error) { failed = error as Error; file.terminate(); }
    });
    unzip.register(UnzipInflate);
    for (let offset = 0; offset < bytes.length && !failed; offset += 65536) {
      unzip.push(bytes.subarray(offset, offset + 65536), offset + 65536 >= bytes.length);
    }
    if (failed) throw failed;
    if (active.size) throw new Error('ZIP повреждён или не завершён.');
    const manifests = [...extracted.keys()].filter(path => /^manifest-[0-9]+\.json$/.test(path));
    if (manifests.length !== 1) throw new Error('Это не ZIP-экспорт Obsidian из Voice Memory.');
    const manifest = JSON.parse(decode(extracted.get(manifests[0])!));
    if (manifest.format !== FORMAT || ![1, 2].includes(manifest.schema_version) || !Array.isArray(manifest.files)) throw new Error('Версия ZIP не поддерживается.');
    if (schema && schema !== manifest.schema_version) throw new Error('Выбраны части разных экспортов.');
    schema = manifest.schema_version;
    const declared = new Set<string>();
    for (const value of manifest.files) {
      const record = validateRecord(value);
      if (record.kind === 'note' && (schema === 2) !== (record.layout === 'folder')) throw new Error('Формат заметки не совпадает с ZIP.');
      if (declared.has(record.path) || !extracted.has(record.path)) throw new Error('Состав ZIP не совпадает с манифестом.');
      declared.add(record.path);
      const previous = records.get(record.path);
      if (previous && previous.sha256 !== record.sha256) throw new Error('Выбраны части разных экспортов.');
      records.set(record.path, record); files.set(record.path, extracted.get(record.path)!);
    }
    if (declared.size !== extracted.size - 1) throw new Error('В ZIP есть незаявленные файлы.');
  }
  const source: Source = { preserveExisting: true, read: async record => {
    const bytes = files.get(record.path);
    if (!bytes) throw new Error('Не хватает ZIP-части с вложением. Выбери все части вместе.');
    return bytes;
  } };
  // Validate everything before the first write to the vault.
  for (const record of records.values()) await checked(record, source);
  const notes = [...records.values()].filter((r): r is Note => r.kind === 'note');
  if (new Set(notes.map(n => n.note_id)).size !== notes.length) throw new Error('Выбраны разные версии одной заметки.');
  const complete = notes.filter(note => note.attachments.every(a => records.get(a.path)?.sha256 === a.sha256));
  for (const note of complete) for (const attachment of note.attachments) await checked(attachment, source);
  return { notes: complete, source, pending: notes.length - complete.length };
}
