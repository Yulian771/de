-- 1) техническая учётная запись пайплайна;
-- 2) база DWH, владелец — техническая учётная запись;
-- 3) источник: эталонная схема + детерминированный генератор данных из задания, права только на чтение.


\getenv etl_user     ETL_DB_USER
\getenv etl_password ETL_DB_PASSWORD
\getenv dwh_db       DWH_DB_NAME
\getenv n_prices     N_PRICES

\if :{?etl_user}
\else
    DO $$ BEGIN RAISE EXCEPTION 'ETL_DB_USER is not set'; END $$;
\endif
\if :{?etl_password}
\else
    DO $$ BEGIN RAISE EXCEPTION 'ETL_DB_PASSWORD is not set'; END $$;
\endif
\if :{?dwh_db}
\else
    DO $$ BEGIN RAISE EXCEPTION 'DWH_DB_NAME is not set'; END $$;
\endif

SELECT current_database() AS source_db \gset

-- Техническая учётная запись: без прав суперпользователя, создания баз и ролей.
CREATE ROLE :"etl_user" LOGIN PASSWORD :'etl_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;

-- DWH: владелец — техническая учётная запись (создаёт схемы raw/core/dq/mart/etl миграциями).
CREATE DATABASE :"dwh_db" OWNER :"etl_user";
REVOKE CONNECT ON DATABASE :"dwh_db" FROM PUBLIC;

-- Источник: схема и данные (под суперпользователем)
\echo '[init] loading source schema into' :source_db
\i /sql/source/schema.sql

\echo '[init] generating synthetic data'
\i /sql/source/synthetic.sql

REVOKE CONNECT ON DATABASE :"source_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"source_db" TO :"etl_user";
GRANT USAGE ON SCHEMA public TO :"etl_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"etl_user";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO :"etl_user";

\echo '[init] done: source' :source_db ', dwh' :dwh_db ', etl user' :etl_user
