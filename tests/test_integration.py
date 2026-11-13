"""
API 集成测试：模拟核心用户流程
"""
import pytest, json
from unittest.mock import MagicMock, patch

def _fallback():
    """Side effect fallback: return MagicMock for __getitem__ safety"""
    return None

class TestCreateOrder:

    def test_store_init_creates_order(self, mock_db, mock_connected, mock_pending):
        from app import app
        with app.test_client() as c:
            # Provide enough None values for all unexpected fetchone calls
            calls = [
                None,  # 0: existing pending order
                {      # 1: available slot
                    "id": 1, "cabinet_id": 1, "slot_number": 1,
                    "slot_size": "M", "status": 1, "board_no": 1,
                    "lock_no": 1, "slot_label": None, "display_number": 1,
                    "last_used_at": None
                },
                {"deposit_amount": 20},  # 2: cabinet deposit
                {"id": 1, "mch_id": "test", "is_active": 1,
                 "api_key": "key", "app_id": "app", "name": "test",
                 "channel_type": "wechat", "weight": 1, "total_users": 0,
                 "total_amount": 0, "total_count": 0},  # 3: payment channel
                {"id": 100},  # 4: order INSERT RETURNING
                {"value": "20"},  # 5: get_setting fallback
            ] + [None] * 30  # lots of fallback Nones
            mock_db.fetchone.side_effect = calls
            mock_db.fetchall.return_value = []
            
            resp = c.post('/api/store/init', json={
                'phone': '13800138000', 'cabinet_id': 1, 'slot_size': 'M'
            })
            data = resp.get_json()
            assert resp.status_code == 200, f"FAIL: {data}"
            assert data['code'] == 200

    def test_store_init_no_slot(self, mock_db, mock_connected, mock_pending):
        from app import app
        with app.test_client() as c:
            mock_db.fetchone.side_effect = [None] * 50
            mock_db.fetchall.return_value = []
            resp = c.post('/api/store/init', json={
                'phone': '13800138000', 'cabinet_id': 1, 'slot_size': 'M'
            })
            assert resp.status_code == 400
            assert '暂无可用柜格' in resp.get_json().get('message', '')

    def test_store_init_duplicate_reuse(self, mock_db, mock_connected, mock_pending):
        from app import app
        with app.test_client() as c:
            mock_db.fetchone.side_effect = None
            mock_db.fetchone.return_value = {
                "id": 99, "order_no": "DUP001", "slot_id": 1,
                "compartment_number": "1", "access_code": "1234",
                "slot_size": "M", "deposit_amount": 20,
                "payment_channel_id": 1
            }
            mock_db.fetchall.return_value = []
            resp = c.post('/api/store/init', json={
                'phone': '13800138000', 'cabinet_id': 1, 'slot_size': 'M'
            })
            data = resp.get_json()
            assert resp.status_code == 200
            assert data['data']['order_id'] == 99

class TestRetrieve:
    def test_retrieve_wrong_code(self, mock_db):
        from app import app
        with app.test_client() as c:
            mock_db.fetchone.return_value = None
            resp = c.post('/api/deposit/retrieve', json={
                'phone': '13800138000', 'access_code': '0000',
                'device_id': 'TEST001'
            })
            assert resp.status_code == 400

class TestAdmin:
    def test_admin_login_empty(self, mock_db):
        from app import app
        with app.test_client() as c:
            resp = c.post('/api/admin/login', json={})
            assert resp.status_code == 400