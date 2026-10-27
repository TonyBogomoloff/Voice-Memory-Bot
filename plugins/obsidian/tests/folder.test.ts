import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile, readdir } from 'node:fs/promises';
import { checked, decode, HISTORY_END, historyParts, Importer, Note, sha256, Source, State, utf8 } from '../src/engine';
import { loadZips } from '../src/zip';
import { MemoryVault } from './vault';

async function fixture() {
  const paths = (await readdir('.test/fixture/folder-zip')).filter(p => p.endsWith('.zip')).sort();
  return loadZips(await Promise.all(paths.map(async p => new Uint8Array(await readFile('.test/fixture/folder-zip/' + p)))));
}
async function importer(vault = new MemoryVault(), state: State = {}) {
  const instance = new Importer(vault, 'Voice Memory', state, async () => {}); await instance.init();
  return { instance, vault, state };
}
async function update(row: Note, source: Source, transform: (text: string) => string) {
  const bytes = utf8.encode(transform(decode(await source.read(row))));
  return { row: { ...row, bytes: bytes.length, sha256: await sha256(bytes) },
    source: { read: async record => record.kind === 'note' ? bytes : source.read(record) } as Source };
}

test('v2 Python ZIP groups many photo/audio records into one folder note and keeps legacy files', async () => {
  const zip = await fixture(); assert.equal(zip.notes.length, 2); assert.equal(zip.pending, 0);
  const { instance, vault, state } = await importer();
  vault.files.set('Voice Memory/Legacy.md', utf8.encode('---\nvoice_memory_id: "0123456789abcdef"\n---\nOld export'));
  for (const row of zip.notes) { assert.equal(row.layout, 'folder'); await instance.accept(row, zip.source); }
  const row = zip.notes.find(n => n.attachments.length)!;
  assert.equal(row.attachments.length, 13);
  const text = await vault.readText(state[row.note_id].path);
  assert(text.indexOf('Исправленная **идея**') < text.indexOf('[[Ссылка]]'));
  assert(text.indexOf('[[Ссылка]]') < text.indexOf('Подпись 11'));
  for (const asset of row.attachments) {
    assert(text.includes('](' + asset.path + ')'));
    assert.equal(await sha256(await vault.readBytes('Voice Memory/' + asset.path)), asset.sha256);
  }
  assert.equal(vault.files.size, 16);
  for (const note of zip.notes) await instance.accept(note, zip.source);
  assert.equal(vault.files.size, 16); assert.equal(instance.report.unchanged, 2);
  assert.equal(await vault.readText('Voice Memory/Legacy.md'), '---\nvoice_memory_id: "0123456789abcdef"\n---\nOld export');
});

test('folder updates preserve personal text, renamed paths and attachments when audio is disabled', async () => {
  const zip = await fixture(); const row = zip.notes.find(n => n.attachments.length)!;
  const { instance, vault, state } = await importer(); await instance.accept(row, zip.source);
  const original = state[row.note_id].path, moved = 'Voice Memory/Мои проекты/Идеи.md';
  const personal = '\n## Мои дополнения\n\nМои мысли, [[ссылки]] и ![Фото](my-photo.jpg)\n';
  vault.files.set(moved, utf8.encode(historyParts(await vault.readText(original))!.history + personal));
  vault.files.delete(original);
  const changed = await update(row, zip.source, text => text.replace(HISTORY_END, 'Новая запись из бота\n\n' + HISTORY_END));
  const next = await importer(vault, state); await next.instance.accept(changed.row, changed.source);
  assert.equal(state[row.note_id].path, moved); assert.equal(next.instance.report.updated, 1);
  const received = await vault.readText(moved);
  assert(received.includes('Новая запись из бота'));
  assert.equal(historyParts(received)!.personal, personal);
  assert(received.includes('](../attachments/'));
  const silent = await update(changed.row, changed.source, text => text.replace(/^.*!\[.*\]\(attachments\/.*\.ogg\).*\n/gm, ''));
  silent.row.attachments = changed.row.attachments.filter(a => !a.path.endsWith('.ogg'));
  await next.instance.accept(silent.row, silent.source);
  assert(!historyParts(await vault.readText(moved))!.history.includes('.ogg'));
  assert.equal(historyParts(await vault.readText(moved))!.personal, personal);
  assert.equal(vault.files.size, 14); // Disabling audio does not delete previously downloaded files.
  await next.instance.accept(silent.row, silent.source); assert.equal(vault.files.size, 14);
});

test('edits inside folder history and edits during replacement are preserved as conflicts', async () => {
  const zip = await fixture(); const row = zip.notes.find(n => !n.attachments.length)!;
  for (const race of [false, true]) {
    const { instance, vault, state } = await importer(); await instance.accept(row, zip.source);
    const path = state[row.note_id].path;
    if (race) vault.race = true;
    else vault.files.set(path, utf8.encode((await vault.readText(path)).replace(HISTORY_END, 'MY HISTORY EDIT\n' + HISTORY_END)));
    const changed = await update(row, zip.source, text => text.replace(HISTORY_END, 'SERVER UPDATE\n' + HISTORY_END));
    await instance.accept(changed.row, changed.source);
    assert((await vault.readText(path)).includes(race ? 'EDIT WHILE DOWNLOADING' : 'MY HISTORY EDIT'));
    assert.notEqual(state[row.note_id].path, path); assert.equal(instance.report.conflicts, 1);
    await instance.accept(changed.row, changed.source); assert.equal(vault.files.size, 2);
  }
  const malformed = await update(row, zip.source, text => text.replace(HISTORY_END, ''));
  await assert.rejects(checked(malformed.row, malformed.source), /история/);
});
