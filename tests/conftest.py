import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql://kiz:kiz@localhost:5433/kiz_test")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="kiz-test-")
os.environ["ADMIN_PASSWORD"] = "admin-password"
os.environ["SECRET_KEY"] = "test-secret"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from kiz import db
    from kiz.main import app

    with TestClient(app) as c:
        with db.tx() as conn:
            conn.execute(
                "TRUNCATE print_job_codes, print_jobs, codes, uploads, products, settings RESTART IDENTITY CASCADE"
            )
            conn.execute("DELETE FROM users WHERE role <> 'admin'")
        yield c
