with open('/etc/nginx/sites-enabled/locker-cqdyxl', 'r') as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if 'return 301 https://kelaiwei.top' in line:
        lines[i] = '        rewrite ^/(MP_verify_.*\.txt|fFBR3J2qOh\.txt)$ /static/$1 last;\n'
        lines.insert(i+1, '        proxy_pass http://127.0.0.1:5001;\n')
        lines.insert(i+2, '        proxy_set_header Host $host;\n')
        lines.insert(i+3, '        proxy_set_header X-Real-IP $remote_addr;\n')
        lines.insert(i+4, '        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n')
        lines.insert(i+5, '        proxy_set_header X-Forwarded-Proto $scheme;\n')
        break
with open('/etc/nginx/sites-enabled/locker-cqdyxl', 'w') as f:
    f.writelines(lines)
print('OK')