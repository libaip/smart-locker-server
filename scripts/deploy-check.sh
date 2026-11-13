#!/bin/bash
set -e
echo "=== 1. ???? ==="
python3 -m py_compile app.py config.py 2>/dev/null
for f in routes/*.py; do python3 -m py_compile "$f" 2>/dev/null || exit 1; done
echo "OK"

echo "=== 2. ????? ==="
pip3 install pylint -q 2>/dev/null
pylint --disable=all --enable=E0602 routes/ 2>&1 | grep -v "^Your code\|^------\|^:" || echo "OK"

echo "=== 3. ???? ==="
for f in cert/*_cert.pem; do
  mch=$(basename "$f" _cert.pem)
  cn=$(openssl x509 -in "$f" -noout -subject 2>/dev/null | grep -o "CN = [0-9]*" | awk "{"print $3"}")
  [ "$mch" = "apiclient" ] && echo "  apiclient -> CN=$cn" && continue
  kf="cert/${mch}_key.pem"
  [ ! -f "$kf" ] && echo "  ERROR: $mch key not found" && exit 1
  cm=$(openssl x509 -noout -modulus -in "$f" 2>/dev/null | openssl md5 | awk "{"print $2"}")
  km=$(openssl rsa -noout -modulus -in "$kf" 2>/dev/null | openssl md5 | awk "{"print $2"}")
  [ "$cm" = "$km" ] && echo "  $mch: MATCH" || echo "  ERROR: $mch MISMATCH" && exit 1
done
echo "OK"

echo "=== 4. ????? ==="
cc=$(grep LATEST_VERSION_CODE config.py | grep -o "[0-9]*")
echo "  config.py: $cc"
echo "OK"
