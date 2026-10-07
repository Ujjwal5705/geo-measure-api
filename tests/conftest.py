import os
import tempfile

# Must be set before `app` is imported so tests never touch the real data directory.
_TMP = tempfile.mkdtemp(prefix="geo_test_")
os.environ["GEO_DATA_DIR"] = _TMP

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:  # runs lifespan -> creates tables
        yield c
