-- 11-2：Grafana 只读账号
-- 目的：Grafana 直连业务库做"业务/质量/知识库"看板，但不给它任何写权限。
-- 说明：本文件只在 Postgres 数据卷**首次初始化**时自动执行（docker-entrypoint-initdb.d）。
--      已有数据卷的环境执行一次即可（命令见 docs/step-11-2-observability.md §5）。
SELECT 'CREATE ROLE grafana_ro LOGIN PASSWORD ''grafana_ro_2026'''
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'grafana_ro')\gexec

GRANT CONNECT ON DATABASE rag TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO grafana_ro;
