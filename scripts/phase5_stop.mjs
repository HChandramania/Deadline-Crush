// Request graceful cleanup of this specific disposable integration fixture.
import { readFile, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const fixture = JSON.parse(await readFile('tmp/phase5-local.json', 'utf8'));
if (!/^phase1_[a-f0-9]{32}$/.test(fixture.schema)) throw new Error('Not a disposable fixture schema');
await writeFile(resolve('tmp', `${fixture.schema}.stop`), 'stop\n');
console.log('Graceful fixture cleanup requested. Wait for the API harness cleanup confirmation.');
