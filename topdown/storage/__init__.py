"""Storage backends for topdown-profiler."""

from topdown.storage.base import StorageBackend
from topdown.storage.sqlite_backend import SQLiteBackend


def get_backend(config) -> StorageBackend:
    """Factory: create and initialize a storage backend from config."""
    if config.backend == "postgresql":
        from topdown.storage.postgresql_backend import PostgreSQLBackend

        if not config.dsn:
            raise ValueError("TOPDOWN_DSN is required for PostgreSQL backend")
        backend = PostgreSQLBackend(dsn=config.dsn)
    else:
        backend = SQLiteBackend(db_path=config.db_path)

    backend.initialize()
    return backend
