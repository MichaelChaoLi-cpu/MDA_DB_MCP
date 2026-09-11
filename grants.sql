-- 把蒙古 HSES / LFS 纳入只读角色的可见范围
--
-- 背景：只读角色 mda_readonly（成员 mda_viewer）默认对 mng_hses_* /
--       mng_lfs_* 这 8 个 schema 没有 USAGE 权限，问答系统看不到蒙古的数据。
--
-- 只授予 SELECT，不给任何写权限——mda_readonly 仍然是只读角色。
-- 脚本是幂等的，重复执行无害。
--
-- 执行：
--     psql -d mda -f grants.sql
--
-- 需要用有权限修改这些 schema 的账号运行（schema 属主或超级用户）。
-- 上面这条命令走 Unix socket + peer 认证，即以当前系统账号的身份连接；
-- 如果数据库属主和系统账号不同名，改成：
--     psql -h localhost -U <属主账号> -d mda -f grants.sql
--
-- 授权完成后记得重建元数据索引（网页设置页的「重建索引」按钮），
-- 否则搜索里还是找不到蒙古的变量。

\set ON_ERROR_STOP on

BEGIN;

-- 1) 允许进入这些 schema
GRANT USAGE ON SCHEMA
    mng_hses_data, mng_hses_meta, mng_hses_alignment, mng_hses_analysis,
    mng_lfs_data,  mng_lfs_meta,  mng_lfs_alignment,  mng_lfs_analysis
TO mda_readonly;

-- 2) 允许读现有的表和视图
GRANT SELECT ON ALL TABLES IN SCHEMA
    mng_hses_data, mng_hses_meta, mng_hses_alignment, mng_hses_analysis,
    mng_lfs_data,  mng_lfs_meta,  mng_lfs_alignment,  mng_lfs_analysis
TO mda_readonly;

-- 3) 以后新建的表也自动可读。
--    注意：ALTER DEFAULT PRIVILEGES 只对「执行本语句的角色所创建的对象」生效。
--    如果将来换别的角色往这些 schema 里建表，需要用那个角色再跑一次。
ALTER DEFAULT PRIVILEGES IN SCHEMA
    mng_hses_data, mng_hses_meta, mng_hses_alignment, mng_hses_analysis,
    mng_lfs_data,  mng_lfs_meta,  mng_lfs_alignment,  mng_lfs_analysis
GRANT SELECT ON TABLES TO mda_readonly;

COMMIT;

-- 4) 验证：下面每一行的 usage 都应该是 t
\echo ''
\echo '=== 授权结果 ==='
SELECT nspname AS schema,
       has_schema_privilege('mda_readonly', nspname, 'USAGE') AS usage
FROM pg_namespace
WHERE nspname LIKE 'mng\_%'
ORDER BY nspname;

\echo ''
\echo '=== mda_viewer 现在能看到多少张表 ==='
SELECT count(*) AS visible_tables
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r','p','v','m')
  AND has_schema_privilege('mda_readonly', n.nspname, 'USAGE')
  AND has_table_privilege('mda_readonly', c.oid, 'SELECT');

\echo ''
\echo '执行完成后，到网页「设置」页点「重建元数据索引」，蒙古数据就会纳入搜索。'
