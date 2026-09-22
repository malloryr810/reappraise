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
