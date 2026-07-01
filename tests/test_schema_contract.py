"""Boot-contract guard — the regression test for the corrections-500 class.

What shipped: the API code began referencing `locations.origin_geom` (added in migration 0007),
but the running DB predated it and nothing asserted the code-vs-schema contract, so every pin
correction 500'd in production. The runtime guard `_assert_schema_at_head` (backend/app/main.py)
checks the DB is migrated to `settings.expected_schema_version` before serving — but that guard is
only as good as the version it points at. If a new migrations/000N.sql lands that the code relies
on and nobody bumps `expected_schema_version`, the guard happily passes against an older DB and we
re-ship the exact same drift.

These pure-logic tests (no DB) pin that contract: expected_schema_version MUST name the newest
migration file. They go RED the instant a migration is added without bumping the setting.
"""
from pathlib import Path

from app.config import settings

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def _migration_files() -> list[str]:
    return sorted(p.name for p in MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql"))


def test_migrations_dir_is_found():
    assert _migration_files(), f"no NNNN_*.sql migrations found under {MIGRATIONS}"


def test_expected_schema_version_is_the_latest_migration():
    files = _migration_files()
    latest = files[-1]
    assert settings.expected_schema_version == latest, (
        f"settings.expected_schema_version={settings.expected_schema_version!r} but the newest "
        f"migration is {latest!r}. Bump expected_schema_version whenever you add a migration the "
        f"code relies on — otherwise the boot-time schema-at-head guard passes against a DB missing "
        f"that migration and the API serves column-referencing code against an older schema (this is "
        f"exactly the corrections-500 production drift)."
    )


def test_expected_schema_version_points_at_a_real_file():
    target = MIGRATIONS / settings.expected_schema_version
    assert target.is_file(), (
        f"expected_schema_version={settings.expected_schema_version!r} is not a file under {MIGRATIONS}"
    )
