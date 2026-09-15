import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB_NAME = "slicer_test_db"
os.environ["MONGO_DB_NAME"] = TEST_DB_NAME

from mongo_service import mongo_service
mongo_service.db_name = TEST_DB_NAME


@pytest.fixture(scope="session", autouse=True)
def isolate_and_cleanup_test_db():
    yield
    # Clean up test database after all tests finish
    if mongo_service.ensure_connection() and mongo_service.client is not None:
        try:
            mongo_service.client.drop_database(TEST_DB_NAME)
        except Exception:
            pass
