import { build } from 'esbuild';
import { mkdir, readFile, writeFile, copyFile, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import { zipSync, strToU8 } from 'fflate';

if (process.argv[2] === 'test') {
  await mkdir('.test', { recursive: true });
  await rm('.test/fixture', { recursive: true, force: true });
  const fixture = spawnSync(resolve('../../.venv/bin/python'), ['../../scripts/build_obsidian_fixture.py', '.test/fixture'], { stdio: 'inherit' });
  if (fixture.status !== 0) process.exit(fixture.status ?? 1);
  await build({ entryPoints: ['tests/engine.test.ts', 'tests/folder.test.ts', 'tests/network.test.ts'], outdir: '.test', outExtension: { '.js': '.mjs' }, bundle: true, platform: 'node', format: 'esm', alias: { obsidian: resolve('tests/request-url.ts') } });
  const result = spawnSync(process.execPath, ['--test', '.test/engine.test.mjs', '.test/folder.test.mjs', '.test/network.test.mjs'], { stdio: 'inherit' });
  process.exit(result.status ?? 1);
}
await mkdir('dist', { recursive: true });
await build({ entryPoints: ['src/main.ts'], outfile: 'dist/main.js', bundle: true, platform: 'browser', format: 'cjs', target: 'es2020', external: ['obsidian'], minify: true });
for (const name of ['manifest.json', 'styles.css']) await copyFile(name, `dist/${name}`);
const files = {};
for (const name of ['main.js', 'manifest.json', 'styles.css']) files[`voice-memory/${name}`] = new Uint8Array(await readFile(`dist/${name}`));
files['voice-memory/INSTALL.txt'] = strToU8('Install this folder into <vault>/.obsidian/plugins/ and enable Voice Memory in Community plugins. Open settings, copy a pairing code from Telegram Settings > Obsidian, and connect. See README.md in the source repository for Android/iOS instructions.');
await writeFile('dist/voice-memory.zip', zipSync(files, { level: 6, mtime: new Date('2026-10-26T00:00:00Z') }));
