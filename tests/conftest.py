import pytest
from unittest.mock import MagicMock, patch, ANY

@pytest.fixture
def mock_db():
    with patch("database.get_db") as m:
        db = MagicMock()
        cur = MagicMock()
        db.cursor.return_value = cur
        cur.execute.return_value = cur
        cur.fetchone.return_value = None
        cur.fetchall.return_value = []
        m.return_value = db
        yield cur

@pytest.fixture
def mock_wxpay():
    with patch("helpers.get_wxpay") as m:
        wp = MagicMock()
        wp.refund.return_value = {"return_code": "SUCCESS", "result_code": "SUCCESS", "refund_id": "mock_refund_001"}
        m.return_value = wp
        yield wp

@pytest.fixture
def mock_connected():
    with patch("helpers.connected_devices", {}) as m:
        yield m

@pytest.fixture
def mock_pending():
    with patch("helpers.pending_lock_commands", {}) as m:
        yield m