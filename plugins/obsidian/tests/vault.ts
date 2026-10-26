import assert from 'node:assert/strict';
import { decode, noteId, utf8, VaultIO } from '../src/engine';

export class MemoryVault implements VaultIO {
  files = new Map<string, Uint8Array>();
  race = false;
  async exists(p: string) { return this.files.has(p); }
  async readText(p: string) { return decode(this.files.get(p)!); }
  async readBytes(p: string) { return this.files.get(p)!; }
  async createText(p: string, t: string) { await this.createBytes(p, utf8.encode(t)); }
  async createBytes(p: string, b: Uint8Array) { assert(!this.files.has(p), 'never overwrite'); this.files.set(p, new Uint8Array(b)); }
  async replaceText(p: string, expected: string, text: string) {
    if (this.race) { this.files.set(p, utf8.encode(expected + '\nEDIT WHILE DOWNLOADING')); this.race = false; }
    if (await this.readText(p) !== expected) return false;
    this.files.set(p, utf8.encode(text)); return true;
  }
  async findNotes(root: string) {
    const result = new Map<string, string[]>();
    for (const [path, bytes] of this.files) if (path.startsWith(root + '/') && path.endsWith('.md')) {
      const id = noteId(decode(bytes)); if (id) result.set(id, [...result.get(id) ?? [], path]);
    }
    return result;
  }
}
