// Live SDK smoke test. Creates one clearly labeled synthetic test account.
// Keeps its random password and session cookies in memory; prints no credentials.
import { loadEnvFile } from 'node:process';
import { existsSync } from 'node:fs';
import { randomBytes, randomUUID } from 'node:crypto';
import assert from 'node:assert/strict';
import { createAuthClient } from '@neon/auth';
import { spawnSync } from 'node:child_process';

for (const file of ['.env.local', '.env']) if (existsSync(file)) loadEnvFile(file);
const url = process.env.NEON_AUTH_BASE_URL;
assert(url?.startsWith('https://'), 'NEON_AUTH_BASE_URL is required');
const originalFetch = globalThis.fetch;
const cookies = new Map();
globalThis.fetch = async (input, init = {}) => {
  const target = new URL(input instanceof Request ? input.url : String(input));
  assert.equal(target.origin, new URL(url).origin, 'Unexpected auth destination');
  const headers = new Headers(input instanceof Request ? input.headers : init.headers);
  headers.set('Origin', 'http://localhost:5173');
  if (cookies.size) headers.set('Cookie', [...cookies].map(([k, v]) => `${k}=${v}`).join('; '));
  const response = await originalFetch(input, { ...init, headers });
  for (const cookie of response.headers.getSetCookie()) {
    const pair = cookie.split(';')[0];
    const separator = pair.indexOf('=');
    const key = pair.slice(0, separator), value = pair.slice(separator + 1);
    if (value) cookies.set(key, value); else cookies.delete(key);
  }
  return response;
};

const client = createAuthClient(url);
const email = `deadline-crush-test-${randomUUID()}@example.com`;
const password = randomBytes(32).toString('base64url');
async function session() {
  const result = await client.getSession();
  assert(!result.error, 'Session request failed');
  return result.data;
}
let stage = 'initial_session';
try {
  assert(!(await session())?.user, 'Unexpected initial session');
  stage = 'signup';
  const signup = await client.signUp.email({ email, password, name: 'Deadline Crush SDK Test' });
  assert(!signup.error, `Signup failed (${signup.error?.code ?? signup.error?.status ?? 'unknown'})`);
  stage = 'current_user';
  const current = await session();
  assert.equal(current?.user.email, email, 'Current user did not match signup');
  const userId = current.user.id;
  if (process.argv.includes('--phase1')) {
    // 0.8.2 copies set-auth-jwt into session.token; do not pass a session cookie.
    const proof = current.session?.token;
    assert(proof?.split('.').length === 3, 'SDK did not provide an access JWT');
    const result = spawnSync('apps/api/.venv/Scripts/python.exe', ['-B', 'scripts/verify_identity.py'], {
      input: JSON.stringify({ token: proof }), encoding: 'utf8', env: process.env,
    });
    // The child deliberately emits only sanitized evidence, never token/claims.
    process.stdout.write(result.stdout || '');
    assert(result.status === 0, 'Live FastAPI identity verification failed; diagnostics suppressed');
  }
  assert(!(await client.signOut()).error, 'Signout failed');
  assert(!(await session())?.user, 'Session survived signout');
  stage = 'invalid_password';
  let rejected = false;
  try {
    const invalid = await client.signIn.email({ email, password: randomBytes(32).toString('hex') });
    rejected = Boolean(invalid.error);
  } catch (error) {
    if (error?.status === 401 || error?.message === 'Invalid email or password') rejected = true;
    else throw error;
  }
  assert(rejected, 'Wrong password accepted');
  stage = 'signin';
  assert(!(await client.signIn.email({ email, password })).error, 'Signin failed');
  assert.equal((await session())?.user.id, userId, 'Signin returned a different user');
  const restored = createAuthClient(url);
  assert.equal((await restored.getSession()).data?.user.id, userId, 'Fresh client did not restore session');
  assert(!(await client.signOut()).error, 'Final signout failed');
  console.log(JSON.stringify({ signup: 'PASS', current_user: 'PASS', signout: 'PASS', invalid_password: 'PASS', signin: 'PASS', session_restoration: 'PASS', synthetic_account_retained: true }));
} catch (error) {
  console.error(`Auth smoke test failed at ${stage}: ${error instanceof Error ? error.message : 'request failed'}`);
  process.exitCode = 1;
} finally {
  globalThis.fetch = originalFetch;
  cookies.clear();
}
