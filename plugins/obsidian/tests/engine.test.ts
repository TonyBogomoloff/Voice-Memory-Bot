import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFile, readdir } from 'node:fs/promises';
import { zipSync, strToU8 } from 'fflate';
import { checked, decode, Importer, Note, noteId, sha256, Source, State, utf8, validateRecord } from '../src/engine';
import { loadZips } from '../src/zip';
import { MemoryVault } from './vault';

async function fixture() {
  const paths = (await readdir('.test/fixture/zip')).filter(p => p.endsWith('.zip')).sort();
  const inputs = await Promise.all(paths.map(async p => new Uint8Array(await readFile('.test/fixture/zip/' + p))));
  return { inputs, ...await loadZips(inputs) };
}
async function importer(vault = new MemoryVault(), state: State = {}) {
  const instance = new Importer(vault, 'Voice Memory', state, async () => {}); await instance.init();
  return { instance, vault, state };
}
async function updated(row: Note, source: Source, suffix = '\nSERVER UPDATE') {
  const data = utf8.encode(decode(await source.read(row)) + suffix);
  return { row: { ...row, bytes: data.length, sha256: await sha256(data) },
    source: { read: async record => record.kind === 'note' ? data : source.read(record) } as Source };
}

test('real Python ZIP roundtrip preserves Markdown and media; repeats do not duplicate', async () => {
  const zip = await fixture(); assert.equal(zip.pending, 0); assert.equal(zip.notes.length, 2);
  const { instance, vault, state } = await importer();
  for (const note of zip.notes) await instance.accept(note, zip.source);
  assert.equal(instance.report.added, 2);
  assert.equal(vault.files.size, 3);
  for (const row of zip.notes) {
    const text = await vault.readText(state[row.note_id].path);
    assert.equal(noteId(text), row.note_id);
    for (const a of row.attachments) assert.equal(await sha256(await vault.readBytes('Voice Memory/' + a.path)), a.sha256);
    await instance.accept(row, zip.source);
  }
  assert.equal(vault.files.size, 3);
  assert.equal(instance.report.unchanged, 2);
});

test('server edits update untouched notes, preserve local edits and do not make repeated conflicts', async () => {
  const zip = await fixture(); const row = zip.notes.find(n => !n.attachments.length)!;
  const { instance, vault, state } = await importer(); await instance.accept(row, zip.source);
  const first = await updated(row, zip.source); await instance.accept(first.row, first.source);
  const path = state[row.note_id].path; assert.equal(instance.report.updated, 1);
  vault.files.set(path, utf8.encode(await vault.readText(path) + '\nMY EDIT'));
  const second = await updated(first.row, first.source, '\nNEXT UPDATE');
  await instance.accept(second.row, second.source);
  assert((await vault.readText(path)).endsWith('MY EDIT'));
  assert.notEqual(state[row.note_id].path, path);
  const count = vault.files.size; await instance.accept(second.row, second.source);
  assert.equal(vault.files.size, count); assert.equal(instance.report.conflicts, 1);
});

test('renamed local note is found by ID; interrupted state saving does not duplicate', async () => {
  const zip = await fixture(); const row = zip.notes.find(n => !n.attachments.length)!;
  const { instance, vault, state } = await importer(); await instance.accept(row, zip.source);
  const original = state[row.note_id].path; const moved = 'Voice Memory/Моя папка/Переименовано.md';
  vault.files.set(moved, vault.files.get(original)!); vault.files.delete(original);
  const next = await importer(vault, state); const change = await updated(row, zip.source);
  await next.instance.accept(change.row, change.source);
  assert.equal(state[row.note_id].path, moved); assert.equal(vault.files.size, 1);
  const recovered = await importer(vault, {}); await recovered.instance.accept(change.row, change.source);
  assert.equal(vault.files.size, 1);
});

test('an edit during atomic replacement remains intact', async () => {
  const zip = await fixture(); const row = zip.notes.find(n => !n.attachments.length)!;
  const { instance, vault, state } = await importer(); await instance.accept(row, zip.source);
  const original = state[row.note_id].path; const change = await updated(row, zip.source);
  vault.race = true; await instance.accept(change.row, change.source);
  assert((await vault.readText(original)).endsWith('EDIT WHILE DOWNLOADING'));
  assert.equal(instance.report.conflicts, 1);
});

test('offline ZIP never replaces an existing differing server version', async () => {
  const zip = await fixture(); const row = zip.notes.find(n => !n.attachments.length)!;
  const { instance, vault, state } = await importer(); const change = await updated(row, zip.source);
  await instance.accept(change.row, change.source); const original = state[row.note_id].path;
  await instance.accept(row, zip.source);
  assert((await vault.readText(original)).endsWith('SERVER UPDATE'));
  assert.equal(instance.report.conflicts, 1);
});

test('locally modified attachment is preserved alongside verified original', async () => {
  const zip = await fixture(); const row = zip.notes.find(n => n.attachments.length)!;
  const { instance, vault } = await importer(); await instance.accept(row, zip.source);
  const path = 'Voice Memory/' + row.attachments[0].path;
  vault.files.set(path, utf8.encode('MY IMAGE EDIT'));
  await instance.accept(row, { ...zip.source, preserveExisting: false });
  assert.equal(await vault.readText(path), 'MY IMAGE EDIT'); assert.equal(instance.report.conflicts, 1);
  const count = vault.files.size; await instance.accept(row, zip.source); assert.equal(vault.files.size, count);
});

test('missing ZIP parts are reported; invalid paths, extra files and corrupted hashes are rejected', async () => {
  const zip = await fixture(); assert(zip.inputs.length > 1);
  await assert.rejects(loadZips([zip.inputs[0].subarray(0, zip.inputs[0].length - 22)]), /повреждён/);
  const first = await loadZips([zip.inputs[0]]); assert(first.pending > 0);
  assert.throws(() => validateRecord({ ...zip.notes[0], path: '../outside.md' }));
  assert.throws(() => validateRecord({ ...zip.notes[0], path: '.obsidian/plugins/evil.md' }));
  const row = zip.notes.find(n => !n.attachments.length)!;
  await assert.rejects(checked(row, { read: async () => utf8.encode('corrupt') }), /сумма/);
  const manifest = { format: 'voice-memory-obsidian', schema_version: 1, files: [row] };
  const fake = zipSync({ 'manifest-001.json': strToU8(JSON.stringify(manifest)), [row.path]: await zip.source.read(row),
    'extra.txt': strToU8('undeclared') });
  await assert.rejects(loadZips([fake]), /незаявленные/);
  const traversal = zipSync({ '../escape.md': strToU8('bad') });
  await assert.rejects(loadZips([traversal]), /путь/);
});
