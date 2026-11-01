-- ============================================================
-- 白名单按 unionid 认人：历史数据回填 + 去重 + 唯一索引
-- 执行前先跑下面的统计（dry-run），确认影响行数后再执行 UPDATE/DELETE
-- ============================================================

-- [DRY-RUN] 现状统计
SELECT
  COUNT(*) AS total_rows,
  COUNT(*) FILTER (WHERE unionid IS NULL OR unionid = '') AS missing_unionid,
  COUNT(*) FILTER (WHERE unionid IS NOT NULL AND unionid <> '') AS has_unionid
FROM withdrawal_whitelist;

-- [DRY-RUN] 同 unionid 多行（回填后会需要去重）
SELECT unionid, COUNT(*) AS cnt
FROM withdrawal_whitelist
WHERE unionid IS NOT NULL AND unionid <> ''
GROUP BY unionid
HAVING COUNT(*) > 1
ORDER BY cnt DESC;

-- [APPLY 1] 按 openid 回填 unionid（优先级：phone_openids -> user_balances -> orders）
UPDATE withdrawal_whitelist ww
SET unionid = COALESCE(
  (SELECT p.unionid FROM phone_openids p
    WHERE p.openid = ww.openid AND p.unionid IS NOT NULL AND p.unionid <> ''
    ORDER BY p.id DESC LIMIT 1),
  (SELECT u.unionid FROM user_balances u
    WHERE u.openid = ww.openid AND u.unionid IS NOT NULL AND u.unionid <> ''
    ORDER BY u.id DESC LIMIT 1),
  (SELECT o.unionid FROM orders o
    WHERE o.openid = ww.openid AND o.unionid IS NOT NULL AND o.unionid <> ''
    ORDER BY o.id DESC LIMIT 1)
)
WHERE (ww.unionid IS NULL OR ww.unionid = '');

-- [DRY-RUN] 回填后仍有 0/空 unionid 的行（这些老数据只能用 openid 兜底）
SELECT openid, source, remain_count, created_at
FROM withdrawal_whitelist
WHERE unionid IS NULL OR unionid = ''
ORDER BY created_at DESC;

-- [APPLY 2] 同 unionid 多行去重（保留无限次 -1，其次保留最新一条；连跑两次兜底）
DELETE FROM withdrawal_whitelist a
USING withdrawal_whitelist b
WHERE a.unionid IS NOT NULL AND a.unionid <> ''
  AND a.unionid = b.unionid
  AND a.openid <> b.openid
  AND (b.remain_count = -1 OR (a.remain_count <> -1 AND b.created_at > a.created_at));

DELETE FROM withdrawal_whitelist a
USING withdrawal_whitelist b
WHERE a.unionid IS NOT NULL AND a.unionid <> ''
  AND a.unionid = b.unionid
  AND a.openid <> b.openid
  AND (b.remain_count = -1 OR (a.remain_count <> -1 AND b.created_at > a.created_at));

-- [DRY-RUN] 去重后复查：应该没有同 unionid 多行
SELECT unionid, COUNT(*) AS cnt
FROM withdrawal_whitelist
WHERE unionid IS NOT NULL AND unionid <> ''
GROUP BY unionid
HAVING COUNT(*) > 1;

-- [APPLY 3] unionid 非空唯一索引（新代码按 unionid 认人，防重复）
CREATE UNIQUE INDEX IF NOT EXISTS uq_withdrawal_whitelist_unionid
  ON withdrawal_whitelist (unionid)
  WHERE unionid IS NOT NULL AND unionid <> '';

-- [DRY-RUN] 最终统计
SELECT
  COUNT(*) AS total_rows,
  COUNT(*) FILTER (WHERE unionid IS NULL OR unionid = '') AS missing_unionid,
  COUNT(*) FILTER (WHERE unionid IS NOT NULL AND unionid <> '') AS has_unionid
FROM withdrawal_whitelist;
