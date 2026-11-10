-- CORE: очищенные, нормализованные факты и дедуплицированные справочники.

-- Весовая категория: по весу, округлённому до 0.1 т (как в источнике).
CREATE OR REPLACE FUNCTION core.fn_weight_category(w double precision)
RETURNS varchar(4) LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
             WHEN w IS NULL                     THEN NULL
             WHEN round(w::numeric, 1) >= 30.0  THEN 'G20T'
             WHEN round(w::numeric, 1) >= 10.0  THEN 'FTL'
             WHEN round(w::numeric, 1) >= 5.0   THEN '10T'
             WHEN round(w::numeric, 1) >= 2.5   THEN '5T'
             WHEN round(w::numeric, 1) >= 0.5   THEN 'L5T'
             ELSE 'OTH'
           END
$$;

-- Объёмная категория: FTL при 0 < V <= 120 м куб, иначе OTH.
CREATE OR REPLACE FUNCTION core.fn_volume_category(v double precision)
RETURNS varchar(4) LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT CASE
             WHEN v IS NULL            THEN NULL
             WHEN v > 0 AND v <= 120   THEN 'FTL'
             ELSE 'OTH'
           END
$$;

-- Бизнес-группы кузова (из требований). Коды, которых здесь нет, попадают в «Прочие».
CREATE TABLE IF NOT EXISTS core.body_group_map (
    body_type_id integer PRIMARY KEY,
    std_id       integer NOT NULL,
    body_group   text    NOT NULL
);
INSERT INTO core.body_group_map (body_type_id, std_id, body_group) VALUES
    (56, 56, 'Тент'), (57, 56, 'Тент'), (58, 56, 'Тент'), (59, 56, 'Тент'), (60, 56, 'Тент'), (61, 56, 'Тент'),
    (4, 4, 'Рефрижератор'), (46, 4, 'Рефрижератор'), (47, 4, 'Рефрижератор'), (48, 4, 'Рефрижератор'),
    (2, 2, 'Изотерм'),
    (1, 15, 'Борт'), (15, 15, 'Борт')
ON CONFLICT (body_type_id) DO UPDATE SET std_id = EXCLUDED.std_id, body_group = EXCLUDED.body_group;

-- Города: одна строка на gc_id (минимальный cities.id), город и регион разобраны из full_name_local.
CREATE TABLE IF NOT EXISTS core.dim_city (
    gc_id           integer PRIMARY KEY,
    city_id         bigint  NOT NULL,
    full_name_local text    NOT NULL,
    city_name       text    NOT NULL,
    region_name     text,
    latitude        double precision,
    longitude       double precision,
    duplicates_cnt  integer NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.dim_body_type (
    body_type_id integer PRIMARY KEY,
    name         text    NOT NULL,
    std_id       integer,
    body_group   text    NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS core.exchange_rate (
    num_code     varchar(3) NOT NULL,
    setting_date date       NOT NULL,
    value        double precision NOT NULL,
    PRIMARY KEY (num_code, setting_date)
);

-- Факт сделки: одна строка на price_id, только записи, прошедшие DQ.
CREATE TABLE IF NOT EXISTS core.fact_price (
    price_id               bigint PRIMARY KEY,
    created_at             timestamp(0),
    updated_at             timestamp(0)     NOT NULL,  -- версия сделки в источнике
    loading_date           date             NOT NULL,
    end_loading_date       date,
    loading_city_gcid      integer          NOT NULL,
    unloading_city_gcid    integer          NOT NULL,
    distance               double precision NOT NULL,
    total_weight           double precision NOT NULL,
    total_volume           double precision NOT NULL,
    body_type              integer,
    body_group             text             NOT NULL,  -- группа основного кузова (prices.body_type) — для ML
    price                  double precision NOT NULL,  -- исходная ставка в валюте сделки
    currency_code          varchar(3)       NOT NULL,
    rate_date              date,
    fx_rate                double precision NOT NULL,
    price_rub              double precision NOT NULL,  -- ставка в RUB без НДС
    price_type             smallint         NOT NULL,
    is_fact                boolean          NOT NULL,
    complex                boolean          NOT NULL,
    organization_id        integer,
    outlier_mark           boolean          NOT NULL,
    weight_category        varchar(4)       NOT NULL,
    volume_category        varchar(4)       NOT NULL,
    weight_category_filled boolean          NOT NULL,  -- категория вычислена ETL (в источнике была NULL)
    volume_category_filled boolean          NOT NULL,
    loaded_at              timestamptz      NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS fact_price_loading_date_idx ON core.fact_price (loading_date);
CREATE INDEX IF NOT EXISTS fact_price_route_idx ON core.fact_price (loading_city_gcid, unloading_city_gcid);

-- Сделка х стандартная группа кузова (без дублей внутри группы).
CREATE TABLE IF NOT EXISTS core.fact_price_body_group (
    price_id    bigint NOT NULL REFERENCES core.fact_price (price_id) ON DELETE CASCADE,
    body_group  text   NOT NULL,
    body_std_id integer,
    PRIMARY KEY (price_id, body_group)
);

-- Сделка х месяц (развёртка price_dates: долгосрочная заявка живёт в нескольких месяцах).
CREATE TABLE IF NOT EXISTS core.fact_price_month (
    price_id   bigint NOT NULL REFERENCES core.fact_price (price_id) ON DELETE CASCADE,
    month      date   NOT NULL,
    price_date date   NOT NULL,
    PRIMARY KEY (price_id, month)
);
CREATE INDEX IF NOT EXISTS fact_price_month_month_idx ON core.fact_price_month (month);

-- Единое определение «рыночной выборки»
CREATE OR REPLACE VIEW core.v_market_base AS
SELECT m.month,
       m.price_date,
       f.price_id,
       f.loading_city_gcid,
       f.unloading_city_gcid,
       bg.body_group,
       bg.body_std_id,
       f.weight_category,
       f.price_rub,
       f.price_rub / NULLIF(f.distance, 0) AS price_km,
       f.distance,
       f.price_type,
       f.organization_id,
       f.updated_at  -- версия сделки: инкрементальная синхронизация ClickHouse по окну
FROM core.fact_price f
JOIN core.fact_price_month m       ON m.price_id = f.price_id
JOIN core.fact_price_body_group bg ON bg.price_id = f.price_id
WHERE f.is_fact
  AND NOT f.complex
  AND f.price > 0
  AND f.distance > 0
  AND NOT f.outlier_mark;
