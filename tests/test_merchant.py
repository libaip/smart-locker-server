import pytest
from helpers import assign_merchant, check_withdraw_auto_approve, get_withhold_hours

class TestAssignMerchant:
    def test_new_user_no_merchant(self, mock_db):
        mock_db.fetchone.return_value = None
        mock_db.fetchone.side_effect = [None, ("mch_001",)]
        result = assign_merchant(phone="13800138000")
        assert result == "mch_001"

    def test_existing_user_returns_merchant(self, mock_db):
        mock_db.fetchone.side_effect = None
        mock_db.fetchone.return_value = ("mch_old",)
        result = assign_merchant(phone="13800138000")
        assert result == "mch_old"

class TestCheckWithdrawAutoApprove:
    def test_new_user_auto_approve(self, mock_db):
        mock_db.fetchone.return_value = None
        result = check_withdraw_auto_approve(phone="13800138000")
        assert result == False

    def test_user_with_complaint_auto_approve(self, mock_db):
        mock_db.fetchone.side_effect = None
        mock_db.fetchone.return_value = (False, 2, "mch_001")
        result = check_withdraw_auto_approve(phone="13800138000")
        assert result == False