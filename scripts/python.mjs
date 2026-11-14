// Neon env files are loaded only into server-side child processes.
import { existsSync } from 'node:fs';
import { loadEnvFile } from 'node:process';
import { spawn } from 'node:child_process';
import { resolve, delimiter } from 'node:path';

for (const file of ['.env.local', '.env']) {
  if (existsSync(file)) loadEnvFile(file); // Existing process variables win.
}
const python = resolve(process.platform === 'win32'
  ? 'apps/api/.venv/Scripts/python.exe' : 'apps/api/.venv/bin/python');
if (!existsSync(python)) {
  console.error('Run uv sync --directory apps/api --locked first.');
  process.exit(1);
}
const commands = {
  api: ['-m', 'uvicorn', 'main:app', '--app-dir', 'apps/api', '--host', '127.0.0.1', '--port', '8787', '--no-access-log'],
  worker: ['apps/consumer/main.py'],
  validation: ['apps/consumer/validation_main.py', ...process.argv.slice(3)],
  recovery: ['apps/api/storage_recovery.py', ...process.argv.slice(3)],
  cors: ['scripts/phase3_cors.py', ...process.argv.slice(3)],
  'cors-cli': ['scripts/phase3_cors_wrangler.py', ...process.argv.slice(3)],
  'fixture-cleanup': ['scripts/phase3_fixture_cleanup.py', ...process.argv.slice(3)],
  check: ['-c', "import main, storage, asyncpg, uvicorn; from deadline_crush_spikes import db, jobs; print('CPython backend imports: PASS')"],
  test: ['-m', 'pytest', '-q', ...process.argv.slice(3)],
  verify: ['scripts/verify_neon.py'],
  phase03: ['scripts/verify_phase03.py', ...process.argv.slice(3)],
  admin: ['scripts/domain_admin.py', ...process.argv.slice(3)],
};
const args = commands[process.argv[2]];
if (!args) throw new Error('Unknown Python command');
const child = spawn(python, args, {
  stdio: 'inherit',
  env: { ...process.env, PYTHONPATH: [resolve('apps/api'), resolve('packages/shared/src'), process.env.PYTHONPATH].filter(Boolean).join(delimiter) },
});
child.on('error', () => { console.error('Could not start Python'); process.exitCode = 1; });
child.on('exit', code => { process.exitCode = code ?? 1; });
