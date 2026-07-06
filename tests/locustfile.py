from locust import HttpUser, task, between

class LockerUser(HttpUser):
    wait_time = between(1, 3)
    
    def on_start(self):
        pass

    @task(1)
    def health_check(self):
        self.client.get("/api/health")
    
    @task(3)
    def view_static(self):
        self.client.get("/static/admin-v2.html")
    
    @task(2)
    def view_deposit(self):
        self.client.get("/")
    
    @task(2)
    def admin_login(self):
        self.client.post("/api/admin/login", json={"username":"admin","password":"admin123"})
    
    @task(1)
    def test_store_init(self):
        self.client.post("/api/store/init", json={"phone":"13800138000","cabinet_id":1,"slot_size":"small"})