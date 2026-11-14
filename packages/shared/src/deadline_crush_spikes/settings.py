"""Server-only configuration. Never print secret values."""
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    DATABASE_URL: str = field(default='', repr=False)
    PROBE_TOKEN: str = field(default='', repr=False)
    R2_ACCOUNT_ID: str = ''
    R2_BUCKET: str = ''
    R2_ACCESS_KEY_ID: str = field(default='', repr=False)
    R2_SECRET_ACCESS_KEY: str = field(default='', repr=False)
    DB_POOL_MIN: int = 0
    DB_POOL_MAX: int = 5
    WORKER_DB_POOL_MIN: int = 0
    WORKER_DB_POOL_MAX: int = 3
    APP_SCHEMA: str = 'deadline_crush'
    NEON_AUTH_BASE_URL: str = ''
    NEON_AUTH_JWKS_URL: str = ''
    CORS_ORIGINS: str = 'http://localhost:5173'
    R2_URL_TTL_SECONDS: int = 60

    @classmethod
    def load(cls):
        values = {key: os.environ[key] for key in cls.__dataclass_fields__ if key in os.environ}
        for key in ('DB_POOL_MIN', 'DB_POOL_MAX', 'WORKER_DB_POOL_MIN', 'WORKER_DB_POOL_MAX', 'R2_URL_TTL_SECONDS'):
            if key in values:
                values[key] = int(values[key])
        result = cls(**values)
        if not 1 <= result.R2_URL_TTL_SECONDS <= 120:
            raise ValueError('Invalid R2 URL lifetime')
        if not 0 <= result.DB_POOL_MIN <= result.DB_POOL_MAX <= 20 or result.DB_POOL_MAX < 1:
            raise ValueError('Invalid database pool bounds')
        if not 0 <= result.WORKER_DB_POOL_MIN <= result.WORKER_DB_POOL_MAX <= 20 or result.WORKER_DB_POOL_MAX < 1:
            raise ValueError('Invalid worker pool bounds')
        from .migrations import schema_name
        schema_name(result.APP_SCHEMA)
        from urllib.parse import urlsplit
        for origin in result.CORS_ORIGINS.split(','):
            url = urlsplit(origin)
            if url.scheme not in ('http', 'https') or not url.netloc or url.path or '*' in origin or url.username:
                raise ValueError('Invalid CORS origin')
        return result
