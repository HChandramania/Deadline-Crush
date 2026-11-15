// Public frontend configuration only. No session handoff endpoint or token capture.
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
const metadata = JSON.parse(await readFile('tmp/phase5-local.json', 'utf8'));
process.env.VITE_API_ORIGIN = metadata.api_origin;
const { createServer } = await import(pathToFileURL(resolve('apps/web/node_modules/vite/dist/node/index.js')));
const server = await createServer({ root: resolve('apps/web'), server: { host: 'localhost', port: 5173, strictPort: true } });
await server.listen();
server.printUrls();
