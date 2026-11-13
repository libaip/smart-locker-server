import pytest
from helpers import get_channel_wxpay

class TestGetChannelWxpay:
    def test_active_channel_returns_wxpay(self):
        channel = {"is_active": 1, "channel_type": "wechat", "mch_id": "mch_test", "api_key": "key", "app_id": "app"}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("helpers.get_wxpay", lambda **kw: "mock_wxpay")
            result, ctype = get_channel_wxpay(channel)
            assert result is not None

    def test_inactive_channel_fallsback(self):
        channel = {"is_active": 0, "channel_type": "wechat", "mch_id": "mch_test", "api_key": "key"}
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("helpers.get_wxpay", lambda **kw: ("mock_default", None))
            result, ctype = get_channel_wxpay(channel)
            assert result is not None