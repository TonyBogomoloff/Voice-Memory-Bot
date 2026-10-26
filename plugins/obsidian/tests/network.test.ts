import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { resolve } from 'node:path';
import { once } from 'node:events';
import { APIError, NetworkSource, serverUrl } from '../src/network';
import { Importer, State } from '../src/engine';
import { setOrigin } from './request-url';
import { MemoryVault } from './vault';

test('real API → production network client → vault, including pairing, pages, media and repeats', { timeout: 20000 }, async () => {
  const child = spawn(resolve('../../.venv/bin/python'), ['../../scripts/serve_obsidian_fixture.py'], { stdio: ['ignore', 'pipe', 'inherit'] });
  const closed = once(child, 'exit');
  const lines = createInterface({ input: child.stdout! });
  try {
    const boot = await Promise.race([once(lines, 'line').then(([line]) => JSON.parse(line)),
      closed.then(() => { throw new Error('Fixture exited early'); })]);
    setOrigin(boot.origin);
    assert.throws(() => serverUrl('http://fixture.invalid'));
    const anonymous = new NetworkSource('https://fixture.invalid', '');
    await assert.rejects(anonymous.list(''), (e: unknown) => e instanceof APIError && e.status === 401);
    const token = await anonymous.pair(boot.code, 'Synthetic iPhone');
    await assert.rejects(anonymous.pair(boot.code, 'Replay'), (e: unknown) => e instanceof APIError && e.status === 401);
    const source = new NetworkSource('https://fixture.invalid', token);
    const vault = new MemoryVault(), state: State = {};
    for (let round = 0; round < 2; round++) {
      const importer = new Importer(vault, 'Voice Memory', state, async () => {}); await importer.init();
      let cursor = '', pages = 0, pending = 0;
      do {
        const page = await source.list(cursor); pages++; pending += page.pending;
        for (const row of page.notes) await importer.accept(row, source);
        cursor = page.next ?? '';
      } while (cursor);
      assert.equal(pages, 2); assert.equal(pending, 1);
      assert.equal(round ? importer.report.unchanged : importer.report.added, 53);
      assert.equal(vault.files.size, 54);
    }
    for (const data of vault.files.values()) assert(!new TextDecoder().decode(data).includes('SYNTHETIC_PRIVATE_FILE_ID'));
  } finally {
    lines.close(); child.kill('SIGTERM'); await closed;
  }
});
