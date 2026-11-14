// Existing ignored configuration enters only this disposable test container.
import { existsSync } from 'node:fs';
import { loadEnvFile } from 'node:process';
import { spawn } from 'node:child_process';
for (const file of ['.env.local','.env']) if (existsSync(file)) loadEnvFile(file);
const names=['DATABASE_URL','DATABASE_URL_UNPOOLED','R2_ACCOUNT_ID','R2_BUCKET','R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY'];
if (names.some(name=>!process.env[name])) throw new Error('Live test configuration required');
const child=spawn('docker',['run','--rm','--memory','1g','--cpus','1','--pids-limit','64',
  '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
  '--tmpfs','/tmp:rw,noexec,nosuid,size=256m',...names.flatMap(name=>['-e',name]),
  'deadline-crush-phase4-tests',...process.argv.slice(2)],{stdio:'inherit',env:process.env});
child.on('error',()=>{process.exitCode=1;});
child.on('exit',code=>{process.exitCode=code??1;});
