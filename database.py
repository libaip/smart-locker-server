"""
智能寄存柜系统 - 数据库初始化与迁移
包含所有表的创建和兼容性迁移逻辑
"""
import sqlite3
import logging
from werkzeug.security import generate_password_hash
import re
from config import DATABASE, DATABASE_URL as CFG_DB_URL
from models import BRAND_DEFAULTS

logger = logging.getLogger(__name__)




class _Row(dict):
    """支持 row["col"] 和 row[0] 访问"""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)

class _PGCursor:
    """包装psycopg2 cursor, 兼容sqlite3接口"""
    def __init__(self, cur):
        self._cur = cur
        self.connection = cur.connection

    def _conv_sql(self, sql):
        s = sql.strip()
        if s.upper().startswith("PRAGMA "):
            return None
        s = sql.replace("?", "%s")
        s = s.replace("AUTOINCREMENT", "")
        if "INSERT OR IGNORE INTO devices" in s:
            s = s.replace("INSERT OR IGNORE INTO devices", "INSERT INTO devices")
            s = s.rstrip(";") + " ON CONFLICT (device_id) DO NOTHING"
        if "INSERT OR REPLACE INTO system_settings" in s:
            s = s.replace("INSERT OR REPLACE INTO system_settings", "INSERT INTO system_settings")
            s = s.rstrip(";") + " ON CONFLICT (setting_key) DO UPDATE SET setting_value=EXCLUDED.setting_value"
        if "INSERT OR IGNORE INTO system_settings" in s:
            s = s.replace("INSERT OR IGNORE INTO system_settings", "INSERT INTO system_settings")
            s = s.rstrip(";") + " ON CONFLICT DO NOTHING"
        if "INSERT OR REPLACE INTO user_profiles" in s:
            s = s.replace("INSERT OR REPLACE INTO user_profiles", "INSERT INTO user_profiles")
            s = s.rstrip(";") + " ON CONFLICT (openid) DO UPDATE SET wechat_name=EXCLUDED.wechat_name, updated_at=EXCLUDED.updated_at"
        s = re.sub(r"datetime\s*\(\s*'now'\s*\)", "NOW()", s)
        s = re.sub(r'datetime\s*\(\s*"now"\s*\)', "NOW()", s)
        # datetime("now", "-30 seconds") -> NOW() - INTERVAL "30 seconds"
        def _fix_dt(m):
            a = m.group(1)
            parts = re.findall(r"'([^']*)'" + '|' + r'"([^"]*)"', a)
            vals = [v[0] or v[1] for v in parts if any(v)]
            if not vals:
                return m.group(0)
            if vals[0] not in ("now",):
                return m.group(0)
            expr = "NOW()"
            for v in vals[1:]:
                if v == "localtime":
                    continue
                if v.startswith("+") or v.startswith("-"):
                    expr += " " + v[0] + " INTERVAL '" + v[1:].strip() + "'"
            return expr
        s = re.sub(r"datetime\s*\(([^()]*(?:\([^()]*\)[^()]*)*)\)", _fix_dt, s)
        
        s = re.sub(r'(\bstatus\b)\s*=\s*(\d+)', lambda m: m.group(1) + " = '" + m.group(2) + "'", s)
        s = re.sub(r'(\bstatus\b)\s*!=\s*(\d+)', lambda m: m.group(1) + " != '" + m.group(2) + "'", s)
        s = re.sub(r'(\bstatus\b)\s*<>\s*(\d+)', lambda m: m.group(1) + " <> '" + m.group(2) + "'", s)
        
        return s

    def execute(self, sql, params=None):
        pg_sql = self._conv_sql(sql)
        if pg_sql is None:
            return self
        if params:
            # Convert numeric params to str for text column compatibility
            converted = tuple(str(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else p for p in params)
            try:
                self._cur.execute(pg_sql, converted)
            except Exception as ee:
                # Auto-rollback on error to prevent transaction failure cascade
                try:
                    self._cur.connection.rollback()
                except:
                    pass
                import traceback
                with open("/tmp/psql_err.log","a") as f:
                    f.write("PSQL ERR: %s\nPARAMS: %s\n" % (pg_sql[:300], str(converted)))
                    traceback.print_exc(file=f)
                raise
        else:
            try:
                self._cur.execute(pg_sql)
            except Exception as ee:
                # Auto-rollback on error to prevent transaction failure cascade
                try:
                    self._cur.connection.rollback()
                except:
                    pass
                raise
        return self

    def fetchone(self):
        r = self._cur.fetchone()
        return _Row(r) if r else None
    def fetchall(self):
        return [_Row(r) for r in self._cur.fetchall()]
    @property
    def lastrowid(self):
        try:
            self._cur.execute("SELECT LASTVAL()")
            r = self._cur.fetchone()
            return r['lastval'] if r else None
        except Exception:
            return None
    @property
    def rowcount(self):
        return self._cur.rowcount
    def __iter__(self):
        for r in self._cur:
            yield _Row(r)

class _PGConn:
    """包装psycopg2连接, 兼容sqlite3接口（带连接池）"""
    _pool = None
    _pool_lock = None

    @classmethod
    def _init_pool(cls, dsn):
        if cls._pool is None:
            import threading
            from psycopg2 import pool
            cls._pool_lock = threading.Lock()
            cls._pool = pool.ThreadedConnectionPool(5, 50, dsn)

    def __init__(self, dsn):
        import psycopg2
        self.__class__._init_pool(dsn)
        self._conn = self.__class__._pool.getconn()
        self._returned = False
    def cursor(self):
        from psycopg2.extras import RealDictCursor
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        return _PGCursor(cur)
    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)
    def rollback(self):
        self._conn.rollback()
    def commit(self):
        self._conn.commit()
    def close(self):
        if getattr(self, '_returned', False):
            return
        self._returned = True
        try:
            self._conn.rollback()
        except:
            pass
        try:
            self.__class__._pool.putconn(self._conn)
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass

def close_all_request_conns():
    """teardown调用：关闭flask.g中的连接，归还连接池"""
    try:
        from flask import g
        conn = getattr(g, '_db_conn', None)
        if conn is not None:
            g._db_conn = None
            conn.close()
    except (ImportError, RuntimeError):
        pass


def get_db():
    """获取数据库连接（flask.g请求级复用+teardown自动回收）"""
    try:
        from flask import g
        if hasattr(g, '_db_conn') and g._db_conn is not None:
            return g._db_conn
    except (ImportError, RuntimeError):
        pass
    conn = sqlite3.connect(DATABASE, timeout=30)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
    except Exception:
        pass
    try:
        from flask import g
        g._db_conn = conn
    except (ImportError, RuntimeError):
        pass
    return conn


def init_db():
    if CFG_DB_URL:
        logger.info('[数据库] PostgreSQL模式，跳过创建表结构（迁移脚本已创建）')
        return
    """初始化数据库表结构 + 迁移"""
    conn = get_db()
    cursor = conn.cursor()

    # ========== 1. 商家表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS merchants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact_name TEXT,
            contact_phone TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            status INTEGER DEFAULT 1,
            agent_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ========== 2. 网点表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id INTEGER,
            name TEXT NOT NULL,
            address TEXT,
            longitude REAL,
            latitude REAL,
            status INTEGER DEFAULT 1,
            withdraw_enabled INTEGER DEFAULT 1,
            auto_approve_day INTEGER DEFAULT 1,
            auto_approve_time TEXT DEFAULT '12:00',
            auto_approve_rate REAL DEFAULT 80,
            click_free_count INTEGER DEFAULT 3,
            anti_test_minutes INTEGER DEFAULT 30,
            anti_test_auto_refund INTEGER DEFAULT 1,
            show_refunding_status INTEGER DEFAULT 1,
            hide_ratio INTEGER DEFAULT 0,
            whitelist_phones TEXT DEFAULT '',
            duplicate_filter_enabled INTEGER DEFAULT 0,
            duplicate_filter_days INTEGER DEFAULT 7,
            duplicate_filter_limit INTEGER DEFAULT 3,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (merchant_id) REFERENCES merchants(id)
        )
    ''')

    # ========== 3. 柜组表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cabinet_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER,
            group_code TEXT UNIQUE NOT NULL,
            name TEXT,
            screen_url TEXT,
            status INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (location_id) REFERENCES locations(id)
        )
    ''')

    # ========== 4. 柜体表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cabinets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            location_id INTEGER,
            group_id INTEGER,
            cabinet_code TEXT UNIQUE NOT NULL,
            name TEXT,
            total_slots INTEGER DEFAULT 12,
            deposit_amount REAL DEFAULT 20,
            mainboard_device_id TEXT,
            mainboard_source TEXT DEFAULT 'QM',
            charge_mode TEXT DEFAULT 'deposit',
            business_status TEXT DEFAULT 'inactive',
            business_hours TEXT DEFAULT '00:00-24:00',
            customer_phone TEXT DEFAULT '400-000-0000',
            app_version TEXT,
            app_version_code INTEGER DEFAULT 0,
            status INTEGER DEFAULT 1,
            last_heartbeat TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (location_id) REFERENCES locations(id),
            FOREIGN KEY (group_id) REFERENCES cabinet_groups(id)
        )
    ''')

    # ========== 4.1 主板表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS mainboards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cabinet_id INTEGER NOT NULL,
            board_index INTEGER NOT NULL,
            slot_count INTEGER DEFAULT 16,
            name TEXT,
            serial_port TEXT DEFAULT 'ttyS2',
            baud_rate INTEGER DEFAULT 9600,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (cabinet_id) REFERENCES cabinets(id),
            UNIQUE(cabinet_id, board_index)
        )
    ''')

    # ========== 4.2 柜格表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS cabinet_slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cabinet_id INTEGER NOT NULL,
            mainboard_id INTEGER,
            slot_number INTEGER NOT NULL,
            display_number INTEGER,
            slot_size TEXT DEFAULT 'M',
            status INTEGER DEFAULT 1,
            cabinet_code TEXT,
            board_no INTEGER DEFAULT 1,
            lock_no INTEGER DEFAULT 1,
            FOREIGN KEY (cabinet_id) REFERENCES cabinets(id),
            FOREIGN KEY (mainboard_id) REFERENCES mainboards(id),
            UNIQUE(cabinet_id, slot_number)
        )
    ''')

    # ========== 5. 订单表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_no TEXT UNIQUE NOT NULL,
            user_phone TEXT NOT NULL,
            slot_id INTEGER,
            cabinet_id INTEGER,
            compartment_number INTEGER,
            access_code TEXT,
            deposit_amount REAL DEFAULT 20,
            status INTEGER DEFAULT 1,
            store_time TIMESTAMP,
            retrieve_time TIMESTAMP,
            transaction_id TEXT,
            refund_id TEXT,
            pay_time TIMESTAMP,
            refund_time TIMESTAMP,
            group_id INTEGER,
            cabinet_code TEXT,
            cabinet_name TEXT,
            slot_size TEXT,
            payment_channel_id INTEGER,
            logical_mark TEXT DEFAULT 'N',
            logic_mark TEXT,
            logic_hide TEXT,
            note TEXT,
            admin_remark TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (slot_id) REFERENCES cabinet_slots(id),
            FOREIGN KEY (cabinet_id) REFERENCES cabinets(id)
        )
    ''')

    # ========== 6. 支付记录表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            type INTEGER NOT NULL,
            amount REAL NOT NULL,
            transaction_id TEXT,
            refund_transaction_id TEXT,
            status INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders(id)
        )
    ''')

    # ========== 7. 系统设置表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS system_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_key TEXT UNIQUE NOT NULL,
            setting_value TEXT,
            description TEXT
        )
    ''')

    # ========== 8. 管理员表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'admin',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ========== 9. 支付渠道表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payment_channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            channel_type TEXT NOT NULL DEFAULT 'wechat',
            mch_id TEXT,
            api_key TEXT,
            app_id TEXT,
            app_secret TEXT,
            cert_name TEXT,
            extra_config TEXT,
            is_active INTEGER DEFAULT 1,
            weight INTEGER DEFAULT 1,
            daily_limit REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            total_count INTEGER DEFAULT 0,
            last_used_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ========== 10. 短信验证码表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sms_codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            code TEXT NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ========== 11. 历史记录表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS storage_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cabinet_id INTEGER NOT NULL,
            compartment_number INTEGER NOT NULL,
            user_phone TEXT,
            access_code TEXT,
            status INTEGER DEFAULT 1,
            store_time TIMESTAMP,
            retrieve_time TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ========== 12. 远程开锁日志表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS remote_open_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id INTEGER NOT NULL,
            cabinet_id INTEGER NOT NULL,
            slot_id INTEGER,
            slot_number INTEGER,
            action_type TEXT DEFAULT 'emergency_open',
            result TEXT DEFAULT 'success',
            ip_address TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (merchant_id) REFERENCES merchants(id),
            FOREIGN KEY (cabinet_id) REFERENCES cabinets(id),
            FOREIGN KEY (slot_id) REFERENCES cabinet_slots(id)
        )
    ''')

    # ========== 13. 代理商表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS agents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact_name TEXT,
            contact_phone TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            status INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ========== 14. 员工表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS employees (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            merchant_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            phone TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'staff',
            status INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (merchant_id) REFERENCES merchants(id)
        )
    ''')

    # ========== 15. 用户余额表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_balances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            openid TEXT DEFAULT '',
            balance REAL DEFAULT 0,
            total_deposited REAL DEFAULT 0,
            total_withdrawn REAL DEFAULT 0,
            first_use_time TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(phone, openid)
        )
    ''')

    # ========== 15b. 用户画像表(微信昵称等) ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS user_profiles (
            openid TEXT PRIMARY KEY,
            wechat_name TEXT DEFAULT '',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ========== 16. 提现/退款记录表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS withdrawal_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            user_phone TEXT NOT NULL,
            amount REAL NOT NULL,
            status INTEGER DEFAULT 0,
            apply_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            approve_time TIMESTAMP,
            approver TEXT,
            click_count INTEGER DEFAULT 1,
            auto_approve_time TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders(id)
        )
    ''')

    # ========== 17. 投诉表 ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS complaints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_phone TEXT NOT NULL,
            type TEXT DEFAULT 'self',
            content TEXT NOT NULL,
            order_no TEXT,
            status INTEGER DEFAULT 0,
            reply TEXT,
            reply_time TIMESTAMP,
            wx_complaint_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # ========== 19. phone_openids ==========
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS phone_openids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            openid TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_phone_openids_phone ON phone_openids(phone)")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_phone_openids_unique ON phone_openids(phone)")


    # ========== 18. pending_lock_cmds ==========
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS pending_lock_cmds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            board_no INTEGER DEFAULT 1,
            lock_no INTEGER DEFAULT 1,
            protocol TEXT DEFAULT 'QM',
            order_id TEXT,
            delivered INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()

    # ========== 执行数据库迁移（兼容旧版） ==========
    _migrate_schema(cursor)

    # ========== 初始化默认数据 ==========
    _init_defaults(cursor)

    conn.commit()
    conn.close()
    logger.info('[数据库] 初始化完成')


def _migrate_schema(cursor):
    """数据库迁移：为新旧字段做兼容"""
    _add_column_if_not_exists(cursor, 'cabinets', 'group_id', 'INTEGER')
    _add_column_if_not_exists(cursor, 'cabinets', 'deposit_amount', 'REAL DEFAULT 20')
    _add_column_if_not_exists(cursor, 'cabinets', 'mainboard_device_id', 'TEXT')
    _add_column_if_not_exists(cursor, 'cabinets', 'mainboard_source', 'TEXT DEFAULT \'YBM\'')
    _add_column_if_not_exists(cursor, 'cabinets', 'charge_mode', 'TEXT DEFAULT \'deposit\'')
    _add_column_if_not_exists(cursor, 'cabinets', 'business_status', 'TEXT DEFAULT \'inactive\'')
    _add_column_if_not_exists(cursor, 'cabinets', 'business_hours', 'TEXT')
    _add_column_if_not_exists(cursor, 'cabinets', 'usage_rules', 'TEXT')
    _add_column_if_not_exists(cursor, 'cabinets', 'customer_phone', 'TEXT')
    _add_column_if_not_exists(cursor, 'cabinets', 'app_version', 'TEXT')
    _add_column_if_not_exists(cursor, 'cabinets', 'app_version_code', 'INTEGER DEFAULT 0')
    _add_column_if_not_exists(cursor, 'cabinets', 'status', 'INTEGER DEFAULT 1')

    _add_column_if_not_exists(cursor, 'mainboards', 'serial_port', 'TEXT DEFAULT \'ttyS2\'')
    _add_column_if_not_exists(cursor, 'mainboards', 'baud_rate', 'INTEGER DEFAULT 9600')

    _add_column_if_not_exists(cursor, 'cabinet_slots', 'cabinet_code', 'TEXT')
    _add_column_if_not_exists(cursor, 'cabinet_slots', 'mainboard_id', 'INTEGER')
    _add_column_if_not_exists(cursor, 'cabinet_slots', 'display_number', 'INTEGER')
    _add_column_if_not_exists(cursor, 'cabinet_slots', 'board_no', 'INTEGER DEFAULT 1')
    _add_column_if_not_exists(cursor, 'cabinet_slots', 'lock_no', 'INTEGER DEFAULT 1')

    _add_column_if_not_exists(cursor, 'orders', 'group_id', 'INTEGER')
    _add_column_if_not_exists(cursor, 'orders', 'cabinet_code', 'TEXT')
    _add_column_if_not_exists(cursor, 'orders', 'cabinet_name', 'TEXT')
    _add_column_if_not_exists(cursor, 'orders', 'slot_size', 'TEXT')
    _add_column_if_not_exists(cursor, 'orders', 'transaction_id', 'TEXT')
    _add_column_if_not_exists(cursor, 'orders', 'refund_id', 'TEXT')
    _add_column_if_not_exists(cursor, 'orders', 'pay_time', 'TIMESTAMP')
    _add_column_if_not_exists(cursor, 'orders', 'refund_time', 'TIMESTAMP')
    _add_column_if_not_exists(cursor, 'orders', 'payment_channel_id', 'INTEGER')
    _add_column_if_not_exists(cursor, 'orders', 'logical_mark', 'TEXT DEFAULT \'N\'')
    _add_column_if_not_exists(cursor, 'orders', 'logic_mark', 'TEXT')
    _add_column_if_not_exists(cursor, 'orders', 'logic_hide', 'TEXT')
    _add_column_if_not_exists(cursor, 'orders', 'note', 'TEXT')
    _add_column_if_not_exists(cursor, 'orders', 'admin_remark', 'TEXT')

    _add_column_if_not_exists(cursor, 'payments', 'refund_transaction_id', 'TEXT')

    _add_column_if_not_exists(cursor, 'merchants', 'agent_id', 'INTEGER')

    _add_column_if_not_exists(cursor, 'locations', 'withdraw_enabled', 'INTEGER DEFAULT 1')
    _add_column_if_not_exists(cursor, 'locations', 'auto_approve_day', 'INTEGER DEFAULT 1')
    _add_column_if_not_exists(cursor, 'locations', 'auto_approve_time', 'TEXT DEFAULT \'12:00\'')
    _add_column_if_not_exists(cursor, 'locations', 'auto_approve_rate', 'REAL DEFAULT 80')
    _add_column_if_not_exists(cursor, 'locations', 'click_free_count', 'INTEGER DEFAULT 3')
    _add_column_if_not_exists(cursor, 'locations', 'anti_test_minutes', 'INTEGER DEFAULT 30')
    _add_column_if_not_exists(cursor, 'locations', 'anti_test_auto_refund', 'INTEGER DEFAULT 1')
    _add_column_if_not_exists(cursor, 'locations', 'show_refunding_status', 'INTEGER DEFAULT 1')
    _add_column_if_not_exists(cursor, 'locations', 'hide_ratio', 'INTEGER DEFAULT 0')
    _add_column_if_not_exists(cursor, 'locations', 'whitelist_phones', 'TEXT DEFAULT \'\'')
    _add_column_if_not_exists(cursor, 'locations', 'duplicate_filter_enabled', 'INTEGER DEFAULT 0')
    _add_column_if_not_exists(cursor, 'locations', 'duplicate_filter_days', 'INTEGER DEFAULT 7')
    _add_column_if_not_exists(cursor, 'locations', 'duplicate_filter_limit', 'INTEGER DEFAULT 3')
    _add_column_if_not_exists(cursor, 'locations', 'usage_rules', 'TEXT DEFAULT ''')

    logger.info('[数据库迁移] 完成')


def _add_column_if_not_exists(cursor, table, column, col_type):
    """安全添加字段（如果不存在）"""
    try:
        cursor.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        if column not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            logger.info(f'[迁移] {table} 表添加 {column} 字段')
    except Exception as e:
        logger.warning(f'[迁移] {table}.{column} 添加失败: {e}')


def _init_defaults(cursor):
    """初始化默认数据"""
    # 默认管理员
    cursor.execute('SELECT COUNT(*) FROM admin_users WHERE username = %s', ('admin',))
    if cursor.fetchone()[0] == 0:
        password_hash = generate_password_hash('admin123')
        cursor.execute('INSERT INTO admin_users (username, password_hash, role) VALUES (%s, %s, %s)',
                       ('admin', password_hash, 'super_admin'))
        logger.info('[初始化] 创建默认管理员 admin/admin123')

    # 默认系统设置
    defaults = [
        ('deposit_amount', '20', '保证金金额（元）'),
        ('sms_enabled', 'false', '是否开启短信验证'),
        ('system_name', '智能寄存柜系统', '系统名称'),
        ('deposit_timeout', '24', '超时时间（小时）'),
        ('pay_mode', 'mock', '支付模式：mock=模拟支付，wechat=微信支付'),
        ('order_hide_rate', '0', '订单隐藏比例(%)'),
        ('order_hide_whitelist', '', '订单隐藏白名单手机号(逗号分隔)'),
        ('duplicate_days', '7', '重复用户过滤天数'),
        ('duplicate_limit', '5', '重复用户过滤单数'),
        ('duplicate_filter_enabled', '0', '是否开启重复用户过滤'),
    ]
    for key, value, desc in defaults:
        cursor.execute('INSERT OR IGNORE INTO system_settings (setting_key, setting_value, description) VALUES (%s, %s, %s)',
                       (key, value, desc))
class _SQLiteCursor:
    def __init__(self, cur):
        self._cur = cur
        self.connection = cur.connection
    def execute(self, sql, params=None):
        if params is not None and "%s" in str(sql):
            sql = sql.replace("%s", "?")
        if params is not None:
            return self._cur.execute(sql, params)
        return self._cur.execute(sql)
    def fetchone(self):
        return self._cur.fetchone()
    def fetchall(self):
        return self._cur.fetchall()
    def __iter__(self):
        return iter(self._cur)
    @property
    def lastrowid(self):
        return self._cur.lastrowid

class _SQLiteConn:
    def __init__(self, conn):
        self._conn = conn
    def __getattr__(self, name):
        return getattr(self._conn, name)
    def __setattr__(self, name, value):
        if name == "_conn":
            super().__setattr__(name, value)
        else:
            setattr(self._conn, name, value)
    def cursor(self):
        return _SQLiteCursor(self._conn.cursor())
    def execute(self, sql, params=None):
        return self.cursor().execute(sql, params)
    def rollback(self):
        self._conn.rollback()
    def commit(self):
        self._conn.commit()
    def close(self):
        self._conn.close()
_orig_connect = sqlite3.connect
def _patched_connect(*args, **kwargs):
    if CFG_DB_URL:
        return _PGConn(CFG_DB_URL)
    conn = _orig_connect(*args, **kwargs)
    return _SQLiteConn(conn)
sqlite3.connect = _patched_connect
