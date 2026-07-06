"""Test script to check if the app can start properly"""
import sys
sys.path.insert(0, '/app/data/所有对话/主对话/smart-locker-v2')

try:
    from app import app
    print("Flask app imported successfully")
    print("Registered routes:")
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        methods = ','.join(sorted(rule.methods - {'OPTIONS', 'HEAD'}))
        if methods:
            print(f"  {methods:8s} {rule.rule}")
except Exception as e:
    import traceback
    print(f"ERROR importing app: {e}")
    traceback.print_exc()
    sys.exit(1)