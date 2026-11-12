import os
import tempfile

import pytest

from app import create_app
from app.db import init_db
from app.services.mock_catalog import reset_catalogs


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.environ["DATABASE_PATH"] = path
    application = create_app()
    application.config["DATABASE"] = path
    application.config["TESTING"] = True
    with application.app_context():
        init_db(application)
    yield application
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture(autouse=True)
def _reset_mock_site():
    """每个测试前恢复 mock 源站初始数据与修订序号。"""
    reset_catalogs()
    yield


@pytest.fixture()
def client(app):
    return app.test_client()
