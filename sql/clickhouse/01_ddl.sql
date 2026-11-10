-- ClickHouse: аналитический слой поверх очищенных фактов core.
-- Проверки качества, курсы и дедупликация остаются в PostgreSQL; здесь только агрегация.

CREATE DATABASE IF NOT EXISTS $db;

CREATE DATABASE IF NOT EXISTS $stage_db;

-- Рыночная выборка (копия core.v_market_base), партиция — месяц. ReplacingMergeTree(version, is_deleted)
CREATE TABLE IF NOT EXISTS $db.market_base
(
    month               Date,
    price_date          Date,
    price_id            UInt64,
    loading_city_gcid   Int32,
    unloading_city_gcid Int32,
    body_group          LowCardinality(String),
    body_std_id         Nullable(Int32),
    weight_category     LowCardinality(String),
    price_rub           Float64,
    price_km            Float64,
    distance            Float64,
    price_type          Int16,
    organization_id     Nullable(Int32),
    updated_at          DateTime,                 -- версия сделки в источнике
    version             UInt64,                   -- версия строки: микросекунды запуска синхронизации
    is_deleted          UInt8
)
ENGINE = ReplacingMergeTree(version, is_deleted)
PARTITION BY toYYYYMM(month)
ORDER BY (loading_city_gcid, unloading_city_gcid, body_group, weight_category, month, price_id);

-- Актуальное состояние рыночной выборки
CREATE VIEW IF NOT EXISTS $db.market_base_current AS
SELECT month, price_date, price_id, loading_city_gcid, unloading_city_gcid, body_group, body_std_id,
       weight_category, price_rub, price_km, distance, price_type, organization_id, updated_at
FROM $db.market_base FINAL
SETTINGS do_not_merge_across_partitions_select_final = 1;

-- Справочник городов — словарь с источником core.dim_city
CREATE DICTIONARY IF NOT EXISTS $db.dict_city
(
    gc_id       UInt64,
    city_name   String,
    region_name Nullable(String)
)
PRIMARY KEY gc_id
SOURCE(POSTGRESQL(NAME pg_dwh TABLE 'dim_city'))
LAYOUT(HASHED())
LIFETIME(0);

-- Витрина: та же схема и зерно, что mart.mart_route_card в PostgreSQL, партиция — месяц.
CREATE TABLE IF NOT EXISTS $db.mart_route_card
(
    month               Date,
    loading_city_gcid   Int32,
    unloading_city_gcid Int32,
    body_group          LowCardinality(String),
    weight_category     LowCardinality(String),
    loading_city        Nullable(String),
    loading_region      Nullable(String),
    unloading_city      Nullable(String),
    unloading_region    Nullable(String),
    body_std_id         Nullable(Int32),
    p25_trip            Float64,
    median_trip         Float64,
    p75_trip            Float64,
    avg_trip            Float64,
    p25_km              Float64,
    median_km           Float64,
    p75_km              Float64,
    trips_count         UInt32,
    carriers_count      UInt32,
    cv_rate             Nullable(Float64),
    mom_pct             Nullable(Float64),
    yoy_pct             Nullable(Float64),
    last_price_date     Date,
    freshness_days      Int32,
    reliability_level   LowCardinality(String),
    as_of               Date,
    pg_updated_at       DateTime64(6, 'UTC')
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(month)
ORDER BY (loading_city_gcid, unloading_city_gcid, body_group, weight_category, month);
