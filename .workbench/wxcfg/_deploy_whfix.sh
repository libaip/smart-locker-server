#!/bin/bash
set -u
cd /home/ubuntu/smart-locker || exit 1
TS=$(date +%Y%m%d_%H%M%S)
BKNAME=whfix_$TS
S175() { ssh -o ConnectTimeout=10 -i ~/.ssh/prod_key ubuntu@172.16.0.2 "$@"; }

echo "===== 1) 106 自检 ====="
python3 -m py_compile routes/webhook.py && echo "  语法通过"
timeout 60 python3 -c "
import app
print('  总路由:', len(list(app.app.url_map.iter_rules())))
" 2>&1 | tail -2
grep -n '_is_user_msg' routes/webhook.py | cut -c1-120 | sed 's/^/  /'
echo
echo "===== 2) 备份 + 推 175 ====="
S175 "cd /home/ubuntu/smart-locker && mkdir -p backups/$BKNAME/routes && cp -a routes/webhook.py backups/$BKNAME/routes/"
scp -i ~/.ssh/prod_key routes/webhook.py ubuntu@172.16.0.2:/home/ubuntu/smart-locker/routes/ || { echo '[中止] scp 失败'; exit 1; }
want=$(md5sum routes/webhook.py | awk '{print $1}')
got=$(S175 "md5sum /home/ubuntu/smart-locker/routes/webhook.py" | awk '{print $1}')
[ "$want" = "$got" ] && echo "  ✅ md5 一致 $want" || { echo "  ❌ 不一致"; exit 1; }
S175 "cd /home/ubuntu/smart-locker && python3 -m py_compile routes/webhook.py && echo '  175 语法通过' && grep -c '_is_user_msg' routes/webhook.py"
echo
echo "===== 3) 重载 ====="
S175 "MPIDS=\$(ps -eo pid,ppid,args | awk '/[g]unicorn/ && \$2==1 {print \$1}'); for p in \$MPIDS; do sudo kill -HUP \$p; done; echo '  已发 HUP: '\$MPIDS"
sleep 15
S175 "for i in \$(seq 1 20); do a=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5001/api/health); b=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:5002/api/health); if [ \"\$a\" = \"200\" ] && [ \"\$b\" = \"200\" ]; then echo \"  就绪 5001=\$a 5002=\$b\"; break; fi; sleep 2; done"
sleep 8
echo "--- 重载后报错扫描（应无内容）---"
S175 "sudo journalctl -u smart-locker --since '3 min ago' --no-pager 2>/dev/null | grep -iE 'traceback|exception' | tail -4"
echo "--- 业务冒烟 ---"
S175 "for p in 5001 5002; do printf '  127.0.0.1:%s/api/health -> ' \$p; curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:\$p/api/health; done
for u in https://locker.cqdyxl.com/ https://locker.cqdyxl.com/store; do printf '  %s -> ' \$u; curl -sk -o /dev/null -w '%{http_code}\n' \$u; done"
echo
echo "===== 4) 功能验证：给 webhook 发一个"订阅事件"，看还会不会生成投诉 ====="
S175 "bash -s" <<'R175'
BEFORE=$(sudo -u postgres psql -d smart_locker -tAc "select count(*) from complaints")
echo "  当前投诉总数: $BEFORE"
XML='<xml><ToUserName><![CDATA[gh_12ece3f5c41f]]></ToUserName><FromUserName><![CDATA[oLhbm2CegcLUOudngMNwdzBhHA7U]]></FromUserName><CreateTime>1789258178</CreateTime><MsgType><![CDATA[event]]></MsgType><Event><![CDATA[subscribe_msg_popup_event]]></Event></xml>'
# 微信会带签名参数；这里直接 POST（如果服务端强制验签，会被拒，那就只能靠代码复核）
curl -s -X POST -H 'Content-Type: text/xml' --data "$XML" "http://127.0.0.1:5001/api/wx/webhook" -o /tmp/wh.out -w '  HTTP=%{http_code}\n'
sleep 2
AFTER=$(sudo -u postgres psql -d smart_locker -tAc "select count(*) from complaints")
echo "  发完事件后的投诉总数: $AFTER"
if [ "$BEFORE" = "$AFTER" ]; then echo "  ✅ 事件没有生成投诉（修复生效）"; else echo "  ❌ 还是生成了投诉，要多查"; fi
echo "  --- 微信原始消息表有没有记下这条事件（应该记，只是不建投诉）---"
sudo -u postgres psql -d smart_locker -tAc "select count(*) from wx_oa_messages where created_at > now() - interval '2 minutes'"
R175
echo
echo "备份: $BKNAME"
