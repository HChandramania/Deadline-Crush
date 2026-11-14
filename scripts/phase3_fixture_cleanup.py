"""Audit/drop only explicitly named disposable fixture schemas after interrupted tests."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import ssl
import asyncpg


async def run(targets):
    direct=os.environ['DATABASE_URL_UNPOOLED']
    connection=await asyncpg.connect(direct,ssl=ssl.create_default_context(),timeout=15,command_timeout=15)
    report={'disposable_schemas':[],'dropped':[]}
    try:
        names=await connection.fetch("SELECT nspname FROM pg_namespace WHERE nspname ~ '^(phase1|phase03)_[a-f0-9]{32}$' ORDER BY nspname")
        for row in names:
            name=row['nspname']
            tables=await connection.fetch('SELECT table_name FROM information_schema.tables WHERE table_schema=$1',name)
            item={'schema':name,'tables':[r['table_name'] for r in tables]}
            if 'schema_migrations' in item['tables']:
                stamp=await connection.fetchval(f'SELECT min(applied_at) FROM "{name}".schema_migrations')
                item['first_migration_utc']=stamp.isoformat() if stamp else None
            report['disposable_schemas'].append(item)
        for name in targets:
            if not re.fullmatch(r'(phase1|phase03)_[a-f0-9]{32}',name):
                raise RuntimeError('Refusing non-fixture schema')
            if name not in {r['schema'] for r in report['disposable_schemas']}:
                raise RuntimeError('Explicit fixture schema not in inventory')
            await connection.execute(f'DROP SCHEMA "{name}" CASCADE')
            report['dropped'].append(name)
        Path('docs/phase3-fixture-cleanup.json').write_text(json.dumps(report,indent=2)+'\n')
        print(json.dumps(report,indent=2))
    finally: await connection.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--drop',action='append',default=[]); args=parser.parse_args()
    try: asyncio.run(run(args.drop))
    except Exception: raise SystemExit('Fixture cleanup failed; private connection details omitted') from None
