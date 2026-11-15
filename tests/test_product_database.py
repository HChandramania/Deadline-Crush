"""Product queries against disposable Neon schema; identity/storage controlled."""
import asyncio
import csv
import io
import os
from uuid import UUID

import pytest
from deadline_crush_spikes import db
from test_phase2_database import environment, admit, activate, complete

pytestmark = pytest.mark.skipif(not os.environ.get('DATABASE_URL_UNPOOLED'), reason='isolated Neon fixture requires explicit database configuration')


def test_product_ledger_export_and_private_details():
    async def check():
        async with environment() as (client, pool, schema, team, actor, storage, subject):
            events = await client.get('/api/v1/hackathons')
            assert events.status_code == 200 and events.json()['server_time']
            first = (await admit(client, team)).json()
            await activate(pool, schema, first)
            assert (await complete(client, team, first)).status_code == 200
            latest = (await admit(client, team)).json()
            assert latest['version_no'] == 2
            detail = await client.get(f"/api/v1/submissions/{first['version_id']}/details")
            assert detail.status_code == 200
            assert detail.json()['job_status'] == 'PENDING'
            assert detail.json()['upload_session_id'] == first['upload_session_id']
            assert (await client.get('/api/v1/organizer/submissions')).status_code == 403
            async with db.transaction(pool, schema) as c:
                await c.execute("UPDATE users SET role='ORGANIZER' WHERE id=$1", actor)
                await c.execute('UPDATE teams SET name=$2 WHERE id=$1', team, '=SUM(1,2)\n"Crew"')
            metrics = (await client.get('/api/v1/organizer/metrics')).json()
            assert metrics['teams'] == 1 and metrics['total_versions'] == 2
            assert metrics['finalized_versions'] == 1 and metrics['checking_versions'] == 1
            assert sum(item['finalized'] for item in metrics['recent_activity']) == 1
            ledger = await client.get('/api/v1/organizer/submissions?limit=1')
            assert ledger.status_code == 200
            row = ledger.json()['rows'][0]
            assert row['version_no'] == 1 and row['latest_version_no'] == 2
            assert row['version_id'] == first['version_id']
            assert (await client.get('/api/v1/organizer/submissions?status=ACCEPTED')).json()['rows'] == []
            assert (await client.get('/api/v1/organizer/submissions?search=missing')).json()['rows'] == []
            assert (await client.get('/api/v1/organizer/submissions?limit=101')).status_code == 422
            drilldown = (await client.get(f'/api/v1/organizer/teams/{team}/versions')).json()
            assert [v['version_no'] for v in drilldown['versions']] == [2, 1]
            export = await client.get('/api/v1/organizer/export.csv')
            assert export.status_code == 200
            rows = list(csv.DictReader(io.StringIO(export.text)))
            assert len(rows) == 1 and rows[0]['version_no'] == '1'
            assert rows[0]['team_name'] == "'=SUM(1,2)\n\"Crew\""
            assert rows[0]['validation_status'] == row['validation_status']
            subject[0] = 'outsider'
            assert (await client.get(f"/api/v1/submissions/{first['version_id']}/details")).status_code == 404
            assert (await client.get(f"/api/v1/submissions/{first['version_id']}/preview/ZIP_PREVIEW")).status_code == 404
            assert (await client.get(f"/api/v1/submissions/{first['version_id']}/download-url")).status_code == 404
            assert (await client.get('/api/v1/organizer/export.csv')).status_code == 403
    asyncio.run(check())
