import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import ts from 'typescript';

async function client(auth) {
  globalThis.__testAuth = auth;
  const source = (await readFile(new URL('./api.ts', import.meta.url), 'utf8'))
    .replace("import { authClient } from './auth';", 'const authClient = globalThis.__testAuth;')
    .replace('import.meta.env.VITE_API_ORIGIN', "'http://api.test'");
  const code = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext } }).outputText;
  return import(`data:text/javascript;base64,${Buffer.from(code + `\n// ${Math.random()}`).toString('base64')}`);
}

test('signed-out requests never reach the backend', async () => {
  const original = globalThis.fetch;
  try {
    let calls = 0;
    globalThis.fetch = async () => { calls++; throw new Error('Unexpected network'); };
    const { api } = await client({ getSession: async () => ({ data: null }) });
    await assert.rejects(api('/teams'), /sign in again/);
    assert.equal(calls, 0);
  } finally { globalThis.fetch = original; }
});

test('authenticated request sends session JWT and omits API cookies', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async (url, init) => {
      assert.equal(url, 'http://api.test/api/v1/teams');
      assert.equal(init.headers.Authorization, 'Bearer test-access-proof');
      assert.equal(init.credentials, 'omit');
      return new Response(JSON.stringify({ teams: [] }), { status: 200 });
    };
    const { api } = await client({ getSession: async () => ({ data: { user: { id: 'opaque' }, session: { token: 'test-access-proof' } } }) });
    assert.deepEqual(await api('/teams'), { teams: [] });
  } finally { globalThis.fetch = original; }
});

test('expired or invalid backend identity prompts signin', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response('', { status: 401 });
    const { api } = await client({ getSession: async () => ({ data: { user: {}, session: { token: 'expired-proof' } } }) });
    await assert.rejects(api('/teams'), /sign in again/);
  } finally { globalThis.fetch = original; }
});

test('logout invalidates a pending session lookup before API transmission', async () => {
  const original = globalThis.fetch;
  try {
    let release, calls = 0;
    const pending = new Promise(resolve => { release = resolve; });
    globalThis.fetch = async () => { calls++; throw new Error('Unexpected network'); };
    const { api, clearApiSession } = await client({ getSession: () => pending });
    const request = api('/teams');
    clearApiSession();
    release({ data: { user: {}, session: { token: 'old-proof' } } });
    await assert.rejects(request, /sign in again/);
    assert.equal(calls, 0);
  } finally { globalThis.fetch = original; }
});

test('logout aborts requests already in flight', async () => {
  const original = globalThis.fetch;
  try {
    let transmitted;
    const ready = new Promise(resolve => { transmitted = resolve; });
    globalThis.fetch = async (url, init) => {
      transmitted();
      return new Promise((resolve, reject) => init.signal.addEventListener('abort', () => reject(new Error('Aborted'))));
    };
    const { api, clearApiSession } = await client({ getSession: async () => ({ data: { user: {}, session: { token: 'proof' } } }) });
    const request = api('/teams');
    await ready; clearApiSession();
    await assert.rejects(request, /Aborted/);
  } finally { globalThis.fetch = original; }
});

test('deadline and idempotency errors preserve distinct actionable codes', async () => {
  const original = globalThis.fetch;
  try {
    const { api, ApiError } = await client({ getSession: async () => ({ data: { user: {}, session: { token: 'test-proof' } } }) });
    for (const code of ['DEADLINE_CLOSED', 'GRACE_EXPIRED', 'IDEMPOTENCY_CONFLICT', 'PARTS_MISMATCH', 'CAPTAIN_REQUIRED']) {
      globalThis.fetch = async () => new Response(JSON.stringify({ error: { code, message: 'safe server error' } }), { status: 409 });
      await assert.rejects(api('/test'), error => error instanceof ApiError && error.code === code && error.status === 409 && error.message !== 'safe server error');
    }
  } finally { globalThis.fetch = original; }
});

test('a rejected cached access token refreshes through the managed SDK once, preserving the mutation key', async () => {
  const original = globalThis.fetch;
  try {
    let calls = 0, refreshes = 0;
    const { api } = await client({ getSession: async options => {
      if (options) { refreshes++; assert.equal(options.query.disableCookieCache, true); assert.equal(options.fetchOptions.headers['X-Force-Fetch'], 'true'); }
      return { data: { user: {}, session: { token: options ? 'fresh-proof' : 'cached-proof' } } };
    } });
    globalThis.fetch = async (url, init) => {
      assert.equal(init.headers['Idempotency-Key'], 'same-operation');
      assert.equal(init.body, '{"frozen":true}');
      assert.equal(init.headers.Authorization, calls++ === 0 ? 'Bearer cached-proof' : 'Bearer fresh-proof');
      return calls === 1 ? new Response('', { status: 401 }) : new Response('{"ok":true}');
    };
    assert.deepEqual(await api('/test', { method: 'POST', headers: { 'Idempotency-Key': 'same-operation' }, body: '{"frozen":true}' }), { ok: true });
    assert.equal(refreshes, 1); assert.equal(calls, 2);
  } finally { globalThis.fetch = original; }
});

test('managed auth infrastructure errors never masquerade as expired identity', async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => { throw new Error('API must not be called'); };
    const { api, ApiError } = await client({ getSession: async () => ({ error: { status: 503 } }) });
    await assert.rejects(api('/test'), error => error instanceof ApiError && error.code === 'AUTH_UNAVAILABLE' && error.status === 503);
  } finally { globalThis.fetch = original; }
});
