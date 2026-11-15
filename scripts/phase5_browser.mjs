// User enters credentials directly into managed Auth; this runner reads no secrets.
import { readFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
const fixture = JSON.parse(await readFile('tmp/phase5-local.json', 'utf8'));
const health = await fetch(fixture.api_origin + '/health');
const contract = await (await fetch(fixture.api_origin + '/openapi.json')).json();
if (!health.ok || !contract.paths['/api/v1/hackathons']) throw new Error('The isolated Phase 5 API is not ready; browser not opened.');
const child = spawn(process.execPath, ['apps/web/node_modules/playwright/cli.js', 'test',
  '--config', 'apps/web/playwright.config.ts', '--grep', 'LIVE isolated participant'], {
  stdio: 'inherit', env: { ...process.env, PW_LIVE_URL: fixture.frontend_origin,
    PW_LIVE_EVENT_ID: fixture.event_id, PW_LIVE_MUTATIONS: 'isolated-only', PW_INTERACTIVE: '1' },
});
child.on('exit', code => { process.exitCode = code ?? 1; });
