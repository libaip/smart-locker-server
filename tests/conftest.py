import pytest
from unittest.mock import MagicMock, patch

@pytest.fixture
def mock_db():
    with patch("database.get_db") as m:
        db = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = lambda: None
        cur.fetchall.return_value = []
        db.cursor.return_value = cur
        db.execute.return_value = cur
        cur.execute.return_value = cur
        m.return_value = db
        yield cur

@pytest.fixture
def mock_wxpay():
    with patch("helpers.get_wxpay") as m:
        wp = MagicMock()
        wp.refund.return_value = {"return_code": "SUCCESS", "result_code": "SUCCESS", "refund_id": "mr1"}
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