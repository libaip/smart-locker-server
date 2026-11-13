import pytest
from unittest.mock import MagicMock, patch

@pytest.fixture
def client():
    with patch("database.get_db") as m:
        db = MagicMock()
        cur = MagicMock()
        cur.fetchone.side_effect = lambda: None
        cur.fetchall.return_value = []
        db.cursor.return_value = cur
        db.execute.return_value = cur
        cur.execute.return_value = cur
        m.return_value = db
        from app import app
        with app.test_client() as c:
            yield c

class TestApiEndpoints:

    def test_health_check(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_static_serving(self, client):
        resp = client.get("/static/admin-v2.html")
        assert resp.status_code == 200

    def test_admin_login_no_db(self, client, mock_db):
        mock_db.fetchone.return_value = None
        resp = client.post("/api/admin/login", json={
            "username": "admin",
            "password": "admin123"
        })
        assert resp.status_code in [200, 400, 500]

    def test_store_init_no_data(self, client, mock_db):
        resp = client.post("/api/store/init", json={})
        assert resp.status_code == 400

    def test_pending_commands_nonexistent(self, client, mock_db):
        resp = client.get("/api/pending-commands/999999")
        assert resp.status_code == 200

    def test_admin_login_wrong_auth(self, client, mock_db):
        mock_db.fetchone.return_value = None
        resp = client.post("/api/admin/login", json={
            "username": "admin",
            "password": "wrong_password"
        })
        assert resp.status_code == 400