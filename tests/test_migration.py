import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient
from main import app
from deadline_crush_spikes import db
from deadline_crush_spikes.settings import Settings


def test_lifespan_without_credentials(monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    monkeypatch.setenv('PROBE_TOKEN', 'test-token')
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get('/health').status_code == 200
        assert app.state.pool is None
        response = client.get('/health/dependencies', headers={'x-probe-token': 'test-token'})
        assert response.status_code == 503


def test_pool_lifecycle(monkeypatch):
    pool = SimpleNamespace(close=AsyncMock())
    create = AsyncMock(return_value=pool)
    monkeypatch.setattr('asyncpg.create_pool', create)
    async def check():
        result = await db.create_pool(Settings(DATABASE_URL='postgresql://example/db', DB_POOL_MAX=3))
        assert result is pool
        await db.close_pool(result)
    asyncio.run(check())
    assert create.call_args.kwargs['max_size'] == 3
    assert create.call_args.kwargs['ssl'].check_hostname
    assert create.call_args.kwargs['statement_cache_size'] == 0
    pool.close.assert_awaited_once()


def test_pool_bounds(monkeypatch):
    monkeypatch.setenv('DB_POOL_MAX', '0')
    with pytest.raises(ValueError):
        Settings.load()


def test_contract_changes_are_targeted():
    # Reconstruct the original contract using the reviewed replacement manifest.
    replacements = json.loads(Path('docs/contract-replacements.json').read_text(encoding='utf-8'))
    baselines = json.loads(Path('docs/contract-baseline-sha256.json').read_text())
    import hashlib
    import difflib
    patch = []
    for filename, pairs in replacements.items():
        after = Path(filename).read_bytes()
        captain = json.loads(Path('docs/captain-contract-audit.json').read_text())
        if filename in captain:
            assert hashlib.sha256(after).hexdigest() == captain[filename]['after_sha256']
            for old, new in reversed(captain[filename]['replacements']):
                if b'\r\n' in after:
                    old, new = old.replace('\n', '\r\n'), new.replace('\n', '\r\n')
                assert after.count(new.encode()) == 1
                after = after.replace(new.encode(), old.encode(), 1)
            assert hashlib.sha256(after).hexdigest() == captain[filename]['before_sha256']
        phase2 = json.loads(Path('docs/phase2-contract-audit.json').read_text())
        if filename in phase2:
            assert hashlib.sha256(after).hexdigest() == phase2[filename]['after_sha256']
            for old, new in reversed(phase2[filename]['replacements']):
                if b'\r\n' in after:
                    old, new = old.replace('\n', '\r\n'), new.replace('\n', '\r\n')
                assert after.count(new.encode()) == 1
                after = after.replace(new.encode(), old.encode(), 1)
            assert hashlib.sha256(after).hexdigest() == phase2[filename]['before_sha256']
        # Reverse the separately audited managed-identity amendment first;
        # retain the original architecture-baseline checks unchanged.
        identity = json.loads(Path('docs/phase1-contract-audit.json').read_text())
        assert hashlib.sha256(after).hexdigest() == identity[filename]['after_sha256']
        for old, new in reversed(identity[filename]['replacements']):
            if b'\r\n' in after:
                old, new = old.replace('\n', '\r\n'), new.replace('\n', '\r\n')
            assert after.count(new.encode()) == 1
            after = after.replace(new.encode(), old.encode(), 1)
        assert hashlib.sha256(after).hexdigest() == identity[filename]['before_sha256']
        before = after
        for old, new in reversed(pairs):
            assert before.count(new.encode()) == 1
            before = before.replace(new.encode(), old.encode(), 1)
        assert hashlib.sha256(before).hexdigest() == baselines[filename]
        patch.extend(difflib.unified_diff(before.decode().splitlines(True), after.decode().splitlines(True),
            fromfile='before/'+filename, tofile='after/'+filename))
    assert ''.join(patch) == Path('docs/ARCHITECTURE_MIGRATION.patch').read_text(encoding='utf-8')
