#!/bin/bash
set -e
cd /home/ubuntu/smart-locker
rm -rf /tmp/s229dry
mkdir -p /tmp/s229dry/routes /tmp/s229dry/static
cp -a routes/user.py /tmp/s229dry/routes/user.py
cp -a static/deposit.html /tmp/s229dry/static/deposit.html

echo "=== 空跑 1：后端 routes/user.py ==="
python3 .workbench/wxcfg/patch_s229_linkmp.py /tmp/s229dry/routes/user.py
echo "=== 空跑 2：H5 static/deposit.html ==="
python3 .workbench/wxcfg/patch_s229_h5.py /tmp/s229dry/static/deposit.html

echo
echo "=== 后端 diff ==="
diff -u routes/user.py /tmp/s229dry/routes/user.py || true
echo
echo "=== H5 diff ==="
diff -u static/deposit.html /tmp/s229dry/static/deposit.html || true
echo
echo "=== 后端语法编译检查 ==="
python3 -m py_compile /tmp/s229dry/routes/user.py && echo "py_compile OK"
echo "=== 标记核对 ==="
echo -n "  user.py 里 mp_page 次数(期望1): "; grep -c "'mp_page'" /tmp/s229dry/routes/user.py || true
echo -n "  H5 里 _askLoop 次数(期望3): "; grep -c '_askLoop' /tmp/s229dry/static/deposit.html || true
