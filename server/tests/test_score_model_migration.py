"""The score-model migration must be additive and preserve existing rows."""
from pathlib import Path


def test_score_model_version_migration_is_nullable_and_non_destructive():
    path = (
        Path(__file__).resolve().parents[2]
        / "db"
        / "migrations"
        / "005_add_state_logs_score_model_version.sql"
    )
    sql = path.read_text(encoding="utf-8").lower()

    assert "add column if not exists score_model_version text" in sql
    assert "not null" not in sql
    assert "default" not in sql
    assert "update " not in sql
    assert "delete " not in sql
    assert "create index" not in sql
