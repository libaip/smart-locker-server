import pytest
from unittest.mock import patch, MagicMock
from helpers import send_open_lock, send_pushplus

class TestSendOpenLock:
    def test_device_connected_sends_ws(self, mock_connected, mock_pending, mock_db):
        mock_connected["dev_001"] = MagicMock()
        result = send_open_lock("dev_001", 1, 1, "YBM", "order_001")
        assert mock_connected["dev_001"].send.called

    def test_device_offline_no_heartbeat_queues(self, mock_connected, mock_pending, mock_db):
        mock_db.fetchone.return_value = (None,)
        result = send_open_lock("dev_offline", 1, 1, "YBM", "order_002")
        assert result is not False
        assert "dev_offline" not in mock_connected or not mock_connected

    def test_device_offline_with_heartbeat_queues(self, mock_connected, mock_pending, mock_db):
        mock_db.fetchone.return_value = ("2026-07-06 10:00:00",)
        with patch("helpers.signal_pending_command"):
            result = send_open_lock("dev_offline_hb", 1, 1, "YBM", "order_003")
            assert result is not False