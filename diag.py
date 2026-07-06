from flask import Flask
from routes.user import bp
app = Flask(__name__)
app.register_blueprint(bp, url_prefix="/api")
print("Routes with 'create':")
for r in app.url_map.iter_rules():
    if "create" in r.rule:
        print(f"  {r.rule} {list(r.methods)}")
print("Total routes:", len(list(app.url_map.iter_rules())))

# Also check if admin route conflicts
from routes.admin_v2 import bp as admin_v2_bp
app.register_blueprint(admin_v2_bp, url_prefix="/api")
print("\nAfter registering admin_v2:")
for r in app.url_map.iter_rules():
    if "deposit" in r.rule:
        print(f"  {r.rule} {list(r.methods)}")