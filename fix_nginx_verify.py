import re

with open("/etc/nginx/sites-enabled/locker-cqdyxl", "r") as f:
    content = f.read()

# Remove the broken insertion (lines with fFBR3J2qOh that were inserted inside TEg3WDrG51)
content = re.sub(
    r'(location = /TEg3WDrG51\.txt \{\s*root .+\n\s+default_type text/plain;\n\s+\}\n)\n    location = /fFBR3J2qOh\.txt \{\n        root .+\n        default_type text/plain;\n    \}\n',
    r'\1',
    content
)

# Insert correctly after the TEg3WDrG51 block
insertion = """    location = /fFBR3J2qOh.txt {
        root /home/ubuntu/smart-locker/static;
        default_type text/plain;
    }
"""
content = content.replace(
    "location = /TEg3WDrG51.txt {\n        root /home/ubuntu/smart-locker/static;\n        default_type text/plain;\n    }\n    location /admin-v2",
    "location = /TEg3WDrG51.txt {\n        root /home/ubuntu/smart-locker/static;\n        default_type text/plain;\n    }\n" + insertion + "    location /admin-v2"
)

with open("/etc/nginx/sites-enabled/locker-cqdyxl", "w") as f:
    f.write(content)

print("Config fixed")