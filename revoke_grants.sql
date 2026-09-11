-- 撤销 grants.sql 授予的权限，恢复到看不见 mng_* 的状态
--
-- 执行：psql -d mda -f revoke_grants.sql
--       （需要属主或超级用户账号；账号与系统账号不同名时加 -h localhost -U <属主>）

\set ON_ERROR_STOP on

BEGIN;

ALTER DEFAULT PRIVILEGES IN SCHEMA
    mng_hses_data, mng_hses_meta, mng_hses_alignment, mng_hses_analysis,
    mng_lfs_data,  mng_lfs_meta,  mng_lfs_alignment,  mng_lfs_analysis
REVOKE SELECT ON TABLES FROM mda_readonly;

REVOKE SELECT ON ALL TABLES IN SCHEMA
    mng_hses_data, mng_hses_meta, mng_hses_alignment, mng_hses_analysis,
    mng_lfs_data,  mng_lfs_meta,  mng_lfs_alignment,  mng_lfs_analysis
FROM mda_readonly;

REVOKE USAGE ON SCHEMA
    mng_hses_data, mng_hses_meta, mng_hses_alignment, mng_hses_analysis,
    mng_lfs_data,  mng_lfs_meta,  mng_lfs_alignment,  mng_lfs_analysis
FROM mda_readonly;

COMMIT;

SELECT nspname AS schema,
       has_schema_privilege('mda_readonly', nspname, 'USAGE') AS usage
FROM pg_namespace WHERE nspname LIKE 'mng\_%' ORDER BY nspname;
