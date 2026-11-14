"""Checksummed transactional SQL migrations scoped to one application schema."""
import hashlib
import re
from pathlib import Path


def schema_name(value: str) -> str:
    if not re.fullmatch(r'(deadline_crush|phase1_[a-f0-9]{32})', value):
        raise ValueError('Use deadline_crush or an isolated phase1_<uuid> schema')
    return value


async def apply(connection, schema: str, directory: Path | None = None):
    schema = schema_name(schema)
    directory = directory or Path(__file__).resolve().parents[4] / 'migrations'
    async with connection.transaction():
        await connection.execute('SELECT pg_advisory_xact_lock(hashtext($1))', 'deadline-crush-migrations:' + schema)
        await connection.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        await connection.execute(f'SET LOCAL search_path TO "{schema}", pg_catalog')
        await connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz NOT NULL DEFAULT clock_timestamp())')
        for path in sorted(directory.glob('[0-9]*.sql')):
            sql = path.read_bytes()
            digest = hashlib.sha256(sql).hexdigest()
            old = await connection.fetchval('SELECT sha256 FROM schema_migrations WHERE name=$1', path.name)
            if old is not None:
                if old != digest:
                    raise ValueError('Applied migration checksum mismatch')
                continue
            await connection.execute(sql.decode('utf-8'))
            await connection.execute('INSERT INTO schema_migrations(name,sha256) VALUES($1,$2)', path.name, digest)
