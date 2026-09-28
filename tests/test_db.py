import pytest

import db


@pytest.fixture
def migrations_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "_MIGRATIONS_DIR", tmp_path)
    return tmp_path


def test_migration_files_are_ordered_by_number(migrations_dir):
    for name in ("002_b.sql", "001_a.sql", "010_c.sql"):
        (migrations_dir / name).write_text("SELECT 1;")

    assert [version for version, _ in db.migration_files()] == ["001_a", "002_b", "010_c"]


@pytest.mark.parametrize("bad_name", ["3_add_column.sql", "0003_add_column.sql", "add_column.sql"])
def test_misnamed_migration_file_is_an_error_not_skipped(migrations_dir, bad_name):
    (migrations_dir / "001_ok.sql").write_text("SELECT 1;")
    (migrations_dir / bad_name).write_text("SELECT 1;")

    with pytest.raises(ValueError, match=bad_name):
        db.migration_files()


def test_rollback_statements_are_read_from_comments(migrations_dir):
    path = migrations_dir / "003_x.sql"
    path.write_text(
        "-- Adds a table.\n"
        "-- rollback: DROP TABLE IF EXISTS x;\n"
        "-- rollback: DELETE FROM y WHERE z = 1;\n"
        "CREATE TABLE x (id INT);\n"
    )

    assert db.rollback_statements(path) == ["DROP TABLE IF EXISTS x", "DELETE FROM y WHERE z = 1"]
    assert db._statements(path) == ["CREATE TABLE x (id INT)"]  # the note is never executed


# Migrations 001 and 002 predate rollback notes and must not be edited
_FIRST_MIGRATION_WITH_ROLLBACK = 3


def test_every_new_migration_has_a_rollback_note():
    missing = [
        version for version, path in db.migration_files()
        if int(version[:3]) >= _FIRST_MIGRATION_WITH_ROLLBACK and not db.rollback_statements(path)
    ]

    assert missing == []
