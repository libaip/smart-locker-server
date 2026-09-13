#!/bin/bash
# 跑配置中心回归测试（35 个用例）。测试是从"本目录"import wx_config 的，
# 所以每次先把线上正在跑的两个文件拷过来，保证测的就是现网代码。
#   bash run_tests.sh                     # 用 SQLite 跑（快，不需要数据库）
#   WXCFG_TEST_PG='postgresql://locker_admin:locker_pass_2024@127.0.0.1:5432/smart_locker_test' bash run_tests.sh
cd "$(dirname "$0")" || exit 1
cp -a /home/ubuntu/smart-locker/wx_config.py ./wx_config.py
cp -a /home/ubuntu/smart-locker/wx_config_api.py ./wx_config_api.py
exec python3 tests/test_wx_config.py "$@"
