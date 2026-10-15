#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[S1113] 行为打分模块 behavior_scorer.py

功能：
  ① 从 user_behavior_log（或 nginx 日志）提取用户行为特征
  ② 按可配置的打分表算分
  ③ 得分 >= 阈值 → 判定为"高投诉风险"
  ④ 记录决策过程（可审计）

设计要点（借鉴开源方案）：
  · 规则可配置：打分表/阈值存数据库，不用改代码
  · 决策记录：每次判定都写日志（得分、命中信号）
  · 灰度开关：先只记录不退款
"""
import logging
import json
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════
# 默认打分表（可被数据库配置覆盖）
# ══════════════════════════════════════════════════════════
DEFAULT_SCORE_TABLE = [
    # (信号名, 类型, 阈值, 分值)
    ('requests_ge_40',    'req',  40,  3),
    ('requests_ge_20',    'req',  20,  1),
    ('balance_ge_10',     'bal',  10,  1),
    ('viewed_withdrawals','wd',   1,   1),
    ('viewed_transactions','tx',  1,   1),
    ('stay_ge_60s',       'dur',  60,  1),
    ('pages_ge_4',        'pages',4,   1),
    ('orders_ge_5',       'ord',  5,   1),
    ('stay_ge_120s',      'dur',  120, 1),
    ('stay_ge_30s',       'dur',  30,  1),
]
DEFAULT_THRESHOLD = 6          # 得分 >= 6 触发
DEFAULT_WINDOW_MIN = 360       # 判定窗口：取包后 6 小时


# ══════════════════════════════════════════════════════════
# 特征提取
# ══════════════════════════════════════════════════════════
def extract_features(events, gap_seconds=1800):
    """
    从行为事件列表提取特征

    events: [(datetime, path), ...]
    gap_seconds: 会话切分间隔（默认30分钟）

    返回 dict 或 None（无行为时）
    """
    if not events:
        return None
    evs = sorted(events, key=lambda x: x[0])
    # 会话切分
    sessions = []
    cur = [evs[0]]
    for e in evs[1:]:
        if (e[0] - cur[-1][0]).total_seconds() > gap_seconds:
            sessions.append(cur)
            cur = [e]
        else:
            cur.append(e)
    sessions.append(cur)

    durations = [(s[-1][0] - s[0][0]).total_seconds() for s in sessions]
    pages = set()
    bal = wd = tx = ord_ = 0
    for _, path in evs:
        if path.startswith('/api/user/balance'):
            pages.add('balance'); bal += 1
        elif path.startswith('/api/user/wallet/transactions'):
            pages.add('tx'); tx += 1
        elif path.startswith('/api/user/wallet/withdrawals'):
            pages.add('wd'); wd += 1
        elif path.startswith('/api/user/orders'):
            pages.add('orders'); ord_ += 1
        elif path.startswith('/api/user/order-detail'):
            pages.add('order_detail')
        elif path.startswith('/api/user/info'):
            pages.add('info')
        elif path.startswith('/api/user/withdraw'):
            pages.add('withdraw')
        elif path.startswith('/api/user/help-refund'):
            pages.add('help')
        elif path.startswith('/api/retrieve'):
            pages.add('retrieve')
        elif path.startswith('/api/deposit'):
            pages.add('deposit')
        else:
            pages.add('other')

    return {
        'req': len(evs),                          # 总请求数
        'sessions': len(sessions),                # 进入次数
        'avg_dur': sum(durations) / max(len(durations), 1),   # 平均停留(秒)
        'max_dur': max(durations) if durations else 0,
        'pages': len(pages),                      # 访问页面种类
        'bal': bal,                               # 查余额次数
        'wd': wd > 0,                             # 是否看过提现记录
        'tx': tx > 0,                             # 是否看过交易流水
        'ord': ord_,                              # 查订单次数
        'first_ts': evs[0][0],
        'last_ts': evs[-1][0],
    }


# ══════════════════════════════════════════════════════════
# 打分
# ══════════════════════════════════════════════════════════
# 打分表 signal_type → 特征字段名 的映射
# （打分表用简写，特征是完整名）
TYPE_MAP = {
    'dur': 'avg_dur',       # 停留时长
    'maxdur': 'max_dur',
    'sess': 'sessions',
    'req': 'req',
    'bal': 'bal',
    'pages': 'pages',
    'ord': 'ord',
    'wd': 'wd',
    'tx': 'tx',
}


def score_features(feat, score_table=None):
    """
    按打分表算分

    返回 (总分, 命中的信号列表)
    """
    if not feat:
        return 0, []
    table = score_table or DEFAULT_SCORE_TABLE
    total = 0
    hit = []
    for name, kind, th, pts in table:
        key = TYPE_MAP.get(kind, kind)
        v = feat.get(key)
        if v is None:
            continue
        if key in ('wd', 'tx'):
            ok = bool(v) if th >= 1 else True
        else:
            ok = (v or 0) >= th
        if ok:
            total += pts
            hit.append({'signal': name, 'value': v, 'points': pts})
    return total, hit


def judge(feat, score_table=None, threshold=None):
    """
    判定：得分 >= 阈值 → 高风险

    返回 dict:
      {score, hits, threshold, is_high_risk, reason}
    """
    th = threshold if threshold is not None else DEFAULT_THRESHOLD
    total, hits = score_features(feat, score_table)
    is_risk = total >= th
    reason = '得分%d>=%d：%s' % (total, th, '+'.join(h['signal'] for h in hits)) if is_risk else ''
    return {
        'score': total,
        'hits': hits,
        'threshold': th,
        'is_high_risk': is_risk,
        'reason': reason,
    }


# ══════════════════════════════════════════════════════════
# 数据库：配置读取 / 决策记录
# ══════════════════════════════════════════════════════════
def ensure_tables(db):
    """建配置表和决策记录表（幂等）"""
    cur = db.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS behavior_score_config (
            id SERIAL PRIMARY KEY,
            signal_name TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            threshold NUMERIC NOT NULL,
            points NUMERIC NOT NULL,
            enabled SMALLINT DEFAULT 1,
            remark TEXT,
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(signal_name)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS behavior_score_settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS behavior_decision_log (
            id SERIAL PRIMARY KEY,
            order_id INTEGER,
            order_no TEXT,
            openid TEXT,
            user_phone TEXT,
            score NUMERIC,
            threshold NUMERIC,
            hits JSONB,
            is_high_risk SMALLINT,
            action TEXT,               -- 'record_only' / 'auto_refund' / 'skipped'
            dry_run SMALLINT DEFAULT 1,-- 是否灰度（1=只记录）
            refund_id INTEGER,
            deposit_amount NUMERIC,
            window_min INTEGER,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bdl_order ON behavior_decision_log(order_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_bdl_created ON behavior_decision_log(created_at DESC)")
    db.commit()
    # 写入默认配置（幂等）
    for name, kind, th, pts in DEFAULT_SCORE_TABLE:
        cur.execute("""INSERT INTO behavior_score_config (signal_name, signal_type, threshold, points)
                       VALUES (%s,%s,%s,%s) ON CONFLICT (signal_name) DO NOTHING""",
                    (name, kind, th, pts))
    cur.execute("""INSERT INTO behavior_score_settings (key, value) VALUES ('threshold', %s)
                   ON CONFLICT (key) DO NOTHING""", (str(DEFAULT_THRESHOLD),))
    cur.execute("""INSERT INTO behavior_score_settings (key, value) VALUES ('window_min', %s)
                   ON CONFLICT (key) DO NOTHING""", (str(DEFAULT_WINDOW_MIN),))
    cur.execute("""INSERT INTO behavior_score_settings (key, value) VALUES ('dry_run', '1')
                   ON CONFLICT (key) DO NOTHING""")
    cur.execute("""INSERT INTO behavior_score_settings (key, value) VALUES ('enabled', '1')
                   ON CONFLICT (key) DO NOTHING""")
    db.commit()
    cur.close()


def load_config(db):
    """从数据库读配置（兼容字典游标和元组游标）"""
    cur = db.cursor()
    try:
        cur.execute("""SELECT signal_name AS a, signal_type AS b, threshold AS c, points AS d
                       FROM behavior_score_config WHERE enabled=1 ORDER BY id""")
        table = []
        for r in cur.fetchall():
            if isinstance(r, dict):
                table.append((r.get('a'), r.get('b'), float(r.get('c')), float(r.get('d'))))
            else:
                table.append((r[0], r[1], float(r[2]), float(r[3])))
        cur.execute("SELECT key AS k, value AS v FROM behavior_score_settings")
        st = {}
        for r in cur.fetchall():
            if isinstance(r, dict):
                st[r.get('k')] = r.get('v')
            else:
                st[r[0]] = r[1]
        return {
            'table': table or DEFAULT_SCORE_TABLE,
            'threshold': float(st.get('threshold') or DEFAULT_THRESHOLD),
            'window_min': int(float(st.get('window_min') or DEFAULT_WINDOW_MIN)),
            'dry_run': str(st.get('dry_run', '1')) == '1',
            'enabled': str(st.get('enabled', '1')) == '1',
        }
    finally:
        cur.close()


def log_decision(db, order, feat, result, action='record_only',
                 dry_run=True, refund_id=None, window_min=360):
    """记录决策（可审计）"""
    cur = db.cursor()
    try:
        cur.execute("""
            INSERT INTO behavior_decision_log
              (order_id, order_no, openid, user_phone, score, threshold, hits,
               is_high_risk, action, dry_run, refund_id, deposit_amount, window_min)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            order.get('id'), order.get('order_no'), order.get('mp_openid'),
            order.get('user_phone'), result['score'], result['threshold'],
            json.dumps(result['hits'], ensure_ascii=False),
            1 if result['is_high_risk'] else 0, action, 1 if dry_run else 0,
            refund_id, order.get('deposit_amount'), window_min,
        ))
        db.commit()
    except Exception as e:
        logger.warning('[behavior_scorer] 决策记录失败: %s' % e)
        try:
            db.rollback()
        except Exception:
            pass
    finally:
        cur.close()


# ══════════════════════════════════════════════════════════
# 特征来源：从 user_behavior_log 表读（或 nginx 日志回填）
# ══════════════════════════════════════════════════════════
def features_from_log(db, openid, t_from, t_to):
    """从 user_behavior_log 表读行为并提取特征"""
    cur = db.cursor()
    try:
        cur.execute("""
            SELECT created_at AS ts, path
            FROM user_behavior_log
            WHERE openid = %s AND created_at >= %s AND created_at <= %s
            ORDER BY created_at
        """, (openid, t_from, t_to))
        events = []
        for r in cur.fetchall():
            ts = r['ts'] if isinstance(r, dict) else r[0]
            path = r['path'] if isinstance(r, dict) else r[1]
            if isinstance(ts, str):
                s = ts[:19]
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
                    try:
                        ts = datetime.strptime(s, fmt)
                        break
                    except Exception:
                        continue
            if hasattr(ts, 'year') and path:
                events.append((ts, path))
        return extract_features(events)
    except Exception as e:
        logger.warning('[behavior_scorer] 读行为日志失败: %s' % e)
        return None
    finally:
        cur.close()
