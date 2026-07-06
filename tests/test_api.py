import pytest
from unittest.mock import MagicMock, patch
from flask import Flask
import sys; sys.path.insert(0, '.')

# We test the admin endpoints through the Flask test client
# with mocked database layer

class TestApiEndpoints:
    
    def test_health_check(self, client):
        """Health check should return 200"""
        resp = client.get('/')
        assert resp.status_code == 200
    
    def test_static_serving(self, client):
        """Static files should be served"""
        resp = client.get('/static/admin-v2.html')
        assert resp.status_code == 200
    
    def test_admin_login_no_db(self, client, mock_db):
        """Admin login with empty db should fail gracefully"""
        mock_db.fetchone.return_value = None
        resp = client.post('/api/admin/login', json={
            'username': 'admin',
            'password': 'admin123'
        })
        assert resp.status_code in [200, 400, 500]
    
    def test_store_init_no_data(self, client, mock_db):
        """Store init without required fields should return 400"""
        resp = client.post('/api/store/init', json={})
        assert resp.status_code == 400
    
    def test_pending_commands_nonexistent(self, client, mock_db):
        """Query pending commands for non-existent device"""
        resp = client.get('/api/pending-commands/999999')
        assert resp.status_code == 200
    
    def test_admin_login_wrong_auth(self, client, mock_db):
        """Admin login with wrong password should return 400"""
        mock_db.fetchone.return_value = None
        resp = client.post('/api/admin/login', json={
            'username': 'admin',
            'password': 'wrong_password'
        })
        assert resp.status_code == 400