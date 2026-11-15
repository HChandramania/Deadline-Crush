import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

async function moduleFor(api) {
  globalThis.__uploadApi = api;
  const source = (await readFile(new URL('./upload.ts', import.meta.url), 'utf8'))
    .replace("import { api } from './api';", 'const api = globalThis.__uploadApi;');
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(code + `\n// ${Math.random()}`).toString('base64')}`);
}

test('file identity covers different bytes with identical name and size', async () => {
  const { fileIdentity } = await moduleFor();
  assert.notEqual(await fileIdentity(new File(['abc'], 'p.zip')), await fileIdentity(new File(['xyz'], 'p.zip')));
  assert.equal(await fileIdentity(new File(['abc'], 'p.zip')), await fileIdentity(new File(['abc'], 'p.zip')));
});
test('file limits reject empty, unsupported, and oversized inputs', async () => {
  const { validateFile } = await moduleFor();
  assert.throws(() => validateFile({ name: 'p.zip', size: 0 }), /100 MiB/);
  assert.throws(() => validateFile({ name: 'p.zip', size: 104857601 }), /100 MiB/);
  assert.throws(() => validateFile({ name: 'p.html', size: 10 }), /ZIP/);
  validateFile({ name: 'p.PDF', size: 104857600 });
});
function draft() { return { body: { filename: 'p.zip', size_bytes: 10 }, fingerprint: 'original', admissionKey: 'stable-admission', session: 'session', keys: {} }; }
test('lost completion response retries the identical frozen server manifest and key after grace', async () => {
  const keys = [], bodies = [];
  const { runUpload } = await moduleFor(async (path, init) => {
    if (path.includes('/complete')) { keys.push(init.headers['Idempotency-Key']); bodies.push(init.body); if (keys.length === 1) throw new Error('response lost'); return { status: 'FINALIZED' }; }
    return { status: 'COMPLETING', completed_parts: [{ part_number: 1, etag: 'exact', size_bytes: 10 }], missing_parts: [], completion_pending: true, parts_source: 'FROZEN_MANIFEST', recoverable: true };
  });
  const value = draft(), args = ['team', value, null, new AbortController().signal, () => {}, () => {}];
  await assert.rejects(runUpload(...args), /response lost/); await runUpload(...args);
  assert.equal(keys[0], keys[1]); assert.equal(bodies[0], bodies[1]);
  assert.deepEqual(JSON.parse(bodies[0]), { parts: [{ part_number: 1, etag: 'exact' }] });
});
test('grace expiration prevents any new signing or completion mutation', async () => {
  let calls = 0;
  const { runUpload } = await moduleFor(async () => { calls++; return { status: 'ACTIVE', recoverable: false }; });
  await assert.rejects(runUpload('t', draft(), null, new AbortController().signal, () => {}, () => {}), /grace has expired/);
  assert.equal(calls, 1);
});
test('completed server state needs no file and never resends bytes', async () => {
  let calls = 0, result;
  const { runUpload } = await moduleFor(async () => { calls++; return { status: 'COMPLETED' }; });
  await runUpload('t', draft(), null, new AbortController().signal, () => {}, text => result = text);
  assert.equal(calls, 1); assert.match(result, /finalized/);
});
test('same admission key survives a lost admission response', async () => {
  const keys = [];
  const { runUpload } = await moduleFor(async (path, init) => { keys.push(init.headers['Idempotency-Key']); throw new Error('lost'); });
  const value = draft(); delete value.session;
  for (let i = 0; i < 2; i++) await assert.rejects(runUpload('t', value, null, new AbortController().signal, () => {}, () => {}), /lost/);
  assert.deepEqual(keys, ['stable-admission', 'stable-admission']);
});
