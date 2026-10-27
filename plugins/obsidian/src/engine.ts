export const FORMAT = 'voice-memory-obsidian';
export const MAX_FILE = 64 * 1024 * 1024;
export const MAX_DOCUMENT = 8 * 1024 * 1024;
export const MAX_BATCH = 200 * 1024 * 1024;
export const HISTORY_END = '<!-- voice-memory:history-end -->';
export const ID = /^[A-Za-z0-9]{16}$/;
export const HASH = /^[0-9a-f]{64}$/;
export const utf8 = new TextEncoder();
export const decode = (data: Uint8Array) => new TextDecoder('utf-8', { fatal: true }).decode(data);
export async function sha256(data: Uint8Array): Promise<string> {
  const bytes = new Uint8Array(data).buffer;
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(v => v.toString(16).padStart(2, '0')).join('');
}
export function safePath(value: unknown): string {
  if (typeof value !== 'string' || !value || value.length > 700 || /[\\:\x00-\x1f\x7f]/.test(value)
      || value.split('/').some(p => !p || p.startsWith('.') || /[<>"|?*]/.test(p) || /[ .]$/.test(p))) {
    throw new Error('Недопустимый путь в экспорте.');
  }
  return value;
}
export interface Attachment { kind: 'attachment'; note_id: string; path: string; bytes: number; sha256: string }
export interface Note { kind: 'note'; layout?: 'folder'; note_id: string; folder_id: string; path: string; bytes: number; sha256: string; attachments: Attachment[] }
export interface Saved { path: string; remoteHash: string; localHash: string; historyHash?: string }
export type State = Record<string, Saved>;
export interface VaultIO {
  exists(path: string): Promise<boolean>;
  readText(path: string): Promise<string>;
  readBytes(path: string): Promise<Uint8Array>;
  createText(path: string, text: string): Promise<void>;
  createBytes(path: string, data: Uint8Array): Promise<void>;
  replaceText(path: string, expected: string, text: string): Promise<boolean>;
  findNotes(root: string): Promise<Map<string, string[]>>;
}
export interface Source {
  read(record: Note | Attachment): Promise<Uint8Array>;
  preserveExisting?: boolean;
}
export interface Report { added: number; updated: number; unchanged: number; conflicts: number; pending: number }
export const newReport = (): Report => ({ added: 0, updated: 0, unchanged: 0, conflicts: 0, pending: 0 });

export function validateRecord(value: unknown): Note | Attachment {
  if (!value || typeof value !== 'object') throw new Error('Некорректный манифест.');
  const r = value as Note | Attachment;
  safePath(r.path);
  if (!ID.test(r.note_id) || !HASH.test(r.sha256) || !Number.isSafeInteger(r.bytes) || r.bytes < 0
      || r.bytes > (r.kind === 'note' ? MAX_DOCUMENT : MAX_FILE)) throw new Error('Некорректный или слишком большой файл.');
  if (r.kind === 'attachment') {
    if (!r.path.startsWith(`attachments/${r.note_id}/`) || !r.path.split('/').pop()!.startsWith(r.sha256 + '.')) {
      throw new Error('Некорректный путь вложения.');
    }
  } else if (r.kind === 'note') {
    if (!ID.test(r.folder_id) || !r.path.endsWith('.md') || r.path.startsWith('attachments/')
        || (r.layout !== undefined && r.layout !== 'folder')
        || (r.layout === 'folder' && r.note_id !== r.folder_id)
        || !Array.isArray(r.attachments) || r.attachments.length > (r.layout === 'folder' ? 10000 : 10)
        || new Set(r.attachments.map(a => a.path)).size !== r.attachments.length) throw new Error('Некорректная заметка.');
    for (const attachment of r.attachments) {
      if (attachment.kind !== 'attachment' || (r.layout !== 'folder' && attachment.note_id !== r.note_id)) throw new Error('Некорректное вложение.');
      validateRecord(attachment);
    }
  } else throw new Error('Неизвестный тип файла.');
  return r;
}
export async function checked(record: Note | Attachment, source: Source): Promise<Uint8Array> {
  validateRecord(record);
  const data = await source.read(record);
  if (data.length !== record.bytes || await sha256(data) !== record.sha256) throw new Error('Контрольная сумма не совпала. Повтори загрузку.');
  if (record.kind === 'note') {
    const text = decode(data);
    if (noteId(text) !== record.note_id) throw new Error('ID заметки не совпал.');
    if (record.layout === 'folder' && !historyParts(text)) throw new Error('Некорректная история папки.');
  }
  return data;
}
export function noteId(text: string): string | null {
  if (!text.startsWith('---\n')) return null;
  const end = text.indexOf('\n---', 4);
  if (end < 0 || end > 16000) return null;
  const match = text.slice(4, end).match(/^voice_memory_id: "([A-Za-z0-9]{16})"\s*$/m);
  return match?.[1] ?? null;
}
export function historyParts(text: string): { history: string; personal: string } | null {
  const end = text.indexOf('\n---', 4);
  if (!text.startsWith('---\n') || end < 0 || end > 16000
      || !/^voice_memory_layout: "folder"\s*$/m.test(text.slice(4, end))) return null;
  const marker = '\n' + HISTORY_END + '\n';
  const index = text.indexOf(marker, end);
  if (index < 0 || text.indexOf(marker, index + marker.length) >= 0) return null;
  return { history: text.slice(0, index + marker.length), personal: text.slice(index + marker.length) };
}
function relative(from: string, to: string): string {
  const a = from.split('/').slice(0, -1), b = to.split('/');
  while (a.length && b.length && a[0] === b[0]) { a.shift(); b.shift(); }
  return [...a.map(() => '..'), ...b].map(encodeURIComponent).join('/');
}
function variant(path: string, hash: string, index = 0): string {
  const dot = path.lastIndexOf('.');
  return path.slice(0, dot) + ` (Voice Memory ${hash.slice(0, 8)}${index ? '-' + index : ''})` + path.slice(dot);
}

export class Importer {
  private index = new Map<string, string[]>();
  readonly report = newReport();
  private consumed = 0;
  constructor(private vault: VaultIO, readonly root: string, private state: State,
              private save: () => Promise<void>) { safePath(root); }
  async init(): Promise<void> { this.index = await this.vault.findNotes(this.root); }
  private inside(path: string): boolean {
    try { safePath(path); return path.startsWith(this.root + '/') && path.endsWith('.md'); } catch { return false; }
  }
  private async existing(row: Note): Promise<string | null> {
    const saved = this.state[row.note_id];
    if (saved && this.inside(saved.path) && await this.vault.exists(saved.path)
        && noteId(await this.vault.readText(saved.path)) === row.note_id) return saved.path;
    const candidates = this.index.get(row.note_id) ?? [];
    return candidates.length === 1 && this.inside(candidates[0]) ? candidates[0] : null;
  }
  private async download(record: Note | Attachment, source: Source): Promise<Uint8Array> {
    this.consumed += record.bytes;
    if (this.consumed > MAX_BATCH) throw new Error('Загружено 200 МБ. Запусти получение ещё раз для оставшихся заметок.');
    return checked(record, source);
  }
  private async attachment(record: Attachment, source: Source): Promise<{ path: string; conflict: boolean }> {
    const target = this.root + '/' + record.path;
    if (await this.vault.exists(target) && await sha256(await this.vault.readBytes(target)) === record.sha256) {
      return { path: target, conflict: false };
    }
    const data = await this.download(record, source);
    for (let i = 0; i < 20; i++) {
      const path = i === 0 ? target : variant(target, record.sha256, i - 1);
      if (await this.vault.exists(path)) {
        if (await sha256(await this.vault.readBytes(path)) === record.sha256) return { path, conflict: path !== target };
        continue;
      }
      try { await this.vault.createBytes(path, data); return { path, conflict: path !== target }; }
      catch (error) { if (!await this.vault.exists(path)) throw error; }
    }
    throw new Error('Не удалось сохранить вложение без замены существующего файла.');
  }
  private async remember(row: Note, path: string, text: string): Promise<void> {
    const parts = row.layout === 'folder' ? historyParts(text) : null;
    this.state[row.note_id] = { path, remoteHash: row.sha256, localHash: await sha256(utf8.encode(text)),
      ...(parts ? { historyHash: await sha256(utf8.encode(parts.history)) } : {}) };
    this.index.set(row.note_id, [path]);
    await this.save();
  }
  async accept(row: Note, source: Source): Promise<void> {
    validateRecord(row);
    const existing = await this.existing(row);
    const saved = this.state[row.note_id];
    const old = existing ? await this.vault.readText(existing) : null;
    // Local edits are deliberately left alone until a new server version arrives.
    if (existing && saved?.remoteHash === row.sha256 && row.attachments.length === 0) {
      this.report.unchanged++; return;
    }
    const assets = new Map<string, string>();
    let assetConflict = false;
    for (const attachment of row.attachments) {
      const result = await this.attachment(attachment, source);
      assets.set(attachment.path, result.path);
      assetConflict ||= result.conflict;
    }
    if (existing && saved?.remoteHash === row.sha256 && !assetConflict) {
      this.report.unchanged++; return;
    }
    const raw = decode(await this.download(row, source));
    let target = existing ?? this.root + '/' + row.path;
    const oldParts = row.layout === 'folder' && old !== null ? historyParts(old) : null;
    const render = (destination: string) => {
      let text = raw;
      for (const [original, local] of assets) {
        const before = relative(row.path, original);
        text = text.split('](' + before + ')').join('](' + relative(destination, local) + ')');
      }
      const parts = row.layout === 'folder' ? historyParts(text) : null;
      return parts && oldParts ? parts.history + oldParts.personal : text;
    };
    let text = render(target);
    if (old === text) {
      await this.remember(row, target, text); this.report.unchanged++; return;
    }
    const unchanged = old !== null && !!saved && (await sha256(utf8.encode(old)) === saved.localHash
      || (oldParts !== null && await sha256(utf8.encode(oldParts.history)) === saved.historyHash));
    let wrote = false;
    if (existing && unchanged && !source.preserveExisting && !assetConflict) {
      wrote = await this.vault.replaceText(existing, old!, text);
      if (wrote) this.report.updated++;
    }
    if (!wrote) {
      const collision = existing !== null || await this.vault.exists(target);
      const base = this.root + '/' + row.path;
      for (let i = 0; i < 20; i++) {
        target = collision || i ? variant(base, row.sha256, i) : base;
        text = render(target);
        if (await this.vault.exists(target)) {
          if (await this.vault.readText(target) === text) { wrote = true; break; }
          continue;
        }
        try { await this.vault.createText(target, text); wrote = true; break; }
        catch (error) { if (!await this.vault.exists(target)) throw error; }
      }
      if (!wrote) throw new Error('Не удалось сохранить заметку без замены твоих файлов.');
      if (collision || assetConflict) this.report.conflicts++; else this.report.added++;
    }
    await this.remember(row, target, text);
  }
}
