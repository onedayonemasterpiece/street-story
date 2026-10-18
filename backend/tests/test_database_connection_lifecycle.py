import gc
from pathlib import Path
import sqlite3
import pytest

from street_story.db import Store


def test_scoped_read_connection_is_closed_even_without_gc(tmp_path):
    store = Store(tmp_path / 'state.sqlite3')
    with store.connection() as connection:
        assert connection.execute('SELECT 1').fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')


def test_identity_read_loops_do_not_exhaust_file_descriptors(tmp_path):
    store = Store(tmp_path / 'state.sqlite3')
    descriptors = Path('/proc/self/fd')
    before = len(list(descriptors.iterdir())) if descriptors.is_dir() else 0
    enabled = gc.isenabled()
    gc.disable()
    try:
        for _ in range(600):
            with store.connection() as db:
                db.execute('SELECT count(*) FROM stories').fetchone()
        if descriptors.is_dir():
            assert len(list(descriptors.iterdir())) <= before + 3
    finally:
        if enabled:
            gc.enable()


def test_scoped_connection_rolls_back_exception_and_closes(tmp_path):
    store = Store(tmp_path / 'state.sqlite3')
    with pytest.raises(RuntimeError):
        with store.connection() as db:
            db.execute('BEGIN')
            db.execute("INSERT INTO cache(key,value_json,expires_at,created_at) VALUES('x','{}',100,0)")
            raise RuntimeError('fixture')
    with store.connection() as db:
        assert db.execute("SELECT 1 FROM cache WHERE key='x'").fetchone() is None
