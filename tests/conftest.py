from __future__ import annotations

import pytest

from nl2sql import sample
from nl2sql.db.sqlite import SQLiteBackend
from nl2sql.engine import Engine
from nl2sql.providers.stub import TemplateProvider
from nl2sql.semantic import load_sample


@pytest.fixture(scope="session")
def sample_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("db") / "sample.db"
    sample.build_sqlite(path)
    return path


@pytest.fixture(scope="session")
def layer():
    return load_sample()


@pytest.fixture
def backend(sample_db):
    b = SQLiteBackend(sample_db)
    yield b
    b.close()


@pytest.fixture(scope="session")
def schema(sample_db):
    b = SQLiteBackend(sample_db)
    try:
        return b.introspect()
    finally:
        b.close()


@pytest.fixture
def engine(backend, layer):
    return Engine(backend, TemplateProvider(), layer)
