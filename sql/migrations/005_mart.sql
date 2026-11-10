-- MART: витрины для BI и результаты ML-скоринга.

-- Карточка маршрута и рыночный коридор.
-- Зерно: месяц х город отправления х город назначения х группа кузова х весовая категория.
CREATE TABLE IF NOT EXISTS mart.mart_route_card (
    month               date        NOT NULL,
    loading_city_gcid   integer     NOT NULL,
    unloading_city_gcid integer     NOT NULL,
    body_group          text        NOT NULL,
    weight_category     varchar(4)  NOT NULL,
    loading_city        text,
    loading_region      text,
    unloading_city      text,
    unloading_region    text,
    body_std_id         integer,
    -- коридор за рейс, руб. без НДС
    p25_trip            numeric(14, 2),
    median_trip         numeric(14, 2),
    p75_trip            numeric(14, 2),
    avg_trip            numeric(14, 2),
    -- коридор за километр, руб./км
    p25_km              numeric(12, 2),
    median_km           numeric(12, 2),
    p75_km              numeric(12, 2),
    -- доказательная база (Data Trust)
    trips_count         integer     NOT NULL,
    carriers_count      integer     NOT NULL,
    cv_rate             numeric(10, 4),
    mom_pct             numeric(10, 2),
    yoy_pct             numeric(10, 2),
    last_price_date     date        NOT NULL,
    freshness_days      integer     NOT NULL,
    reliability_level   text        NOT NULL CHECK (reliability_level IN ('HIGH', 'MEDIUM', 'LOW_DATA')),
    as_of               date        NOT NULL,
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (month, loading_city_gcid, unloading_city_gcid, body_group, weight_category)
);
-- Сценарий «карточка одного маршрута»: история по ячейке.
CREATE INDEX IF NOT EXISTS mart_route_card_route_idx
    ON mart.mart_route_card (loading_city_gcid, unloading_city_gcid, body_group, weight_category, month);

-- Сценарий «сводная таблица сети»: последний месяц по каждой ячейке.
CREATE OR REPLACE VIEW mart.v_route_card_latest AS
SELECT DISTINCT ON (loading_city_gcid, unloading_city_gcid, body_group, weight_category) *
FROM mart.mart_route_card
ORDER BY loading_city_gcid, unloading_city_gcid, body_group, weight_category, month DESC;

-- Пакетный скоринг основной модели: факт vs прогноз (baselines — только метрики в ml_model_metrics).
-- split: test — отложенный период модели; future — плановые заявки с loading_date > as_of, факт не известен.
CREATE TABLE IF NOT EXISTS mart.ml_price_predictions (
    price_id            bigint      NOT NULL,
    model_name          text        NOT NULL,
    model_version       text        NOT NULL,
    split               text        NOT NULL CHECK (split IN ('test', 'future')),
    loading_date        date        NOT NULL,
    loading_city_gcid   integer     NOT NULL,
    unloading_city_gcid integer     NOT NULL,
    body_group          text        NOT NULL,
    weight_category     varchar(4)  NOT NULL,
    distance            double precision NOT NULL,
    outlier_mark        boolean     NOT NULL,
    actual_price_rub    numeric(14, 2),          -- NULL для будущих заявок
    predicted_price_rub numeric(14, 2) NOT NULL,
    abs_error_rub       numeric(14, 2),
    ape_pct             numeric(10, 2),
    scored_at           timestamptz NOT NULL,
    PRIMARY KEY (price_id, model_name),
    CHECK (split = 'test' OR (actual_price_rub IS NULL AND abs_error_rub IS NULL AND ape_pct IS NULL))
);
CREATE INDEX IF NOT EXISTS ml_price_predictions_date_idx ON mart.ml_price_predictions (loading_date);

CREATE TABLE IF NOT EXISTS mart.ml_model_metrics (
    model_name    text        NOT NULL,
    as_of         date        NOT NULL,          -- дата обучения (разбиение train / test)
    split         text        NOT NULL,
    model_version text        NOT NULL,          -- та же версия, что в артефакте и в ml_price_predictions
    n_rows        integer     NOT NULL,
    r2            double precision,
    mape_pct      double precision,
    rmse_rub      double precision,
    mae_rub       double precision,
    train_rows    integer,
    params        jsonb,
    trained_at    timestamptz NOT NULL,
    PRIMARY KEY (model_name, as_of, split)
);

-- Поиск ячеек по маршруту (карточка маршрута, последний доступный коридор).
CREATE INDEX IF NOT EXISTS mart_route_card_route_idx
    ON mart.mart_route_card (loading_city_gcid, unloading_city_gcid, body_group, weight_category, month);

-- Факт vs прогноз vs рыночный коридор: подсветка рейсов, закрытых заметно выше рынка.
CREATE OR REPLACE VIEW mart.v_fact_vs_forecast AS
SELECT p.price_id,
       p.model_name,
       p.split,
       p.loading_date,
       p.loading_city_gcid,
       p.unloading_city_gcid,
       p.body_group,
       p.weight_category,
       p.actual_price_rub,
       p.predicted_price_rub,
       p.abs_error_rub,
       p.ape_pct,
       c.p25_trip,
       c.median_trip,
       c.p75_trip,
       c.reliability_level,
       CASE WHEN p.actual_price_rub IS NULL THEN NULL
            ELSE p.actual_price_rub > p.predicted_price_rub * 1.15 END AS above_forecast_15pct,
       CASE WHEN p.actual_price_rub IS NULL OR c.p75_trip IS NULL THEN NULL
            ELSE p.actual_price_rub > c.p75_trip END                AS above_market_p75,
       c.month                                                      AS corridor_month
FROM mart.ml_price_predictions p
LEFT JOIN LATERAL (
    SELECT r.month, r.p25_trip, r.median_trip, r.p75_trip, r.reliability_level
    FROM mart.mart_route_card r
    WHERE r.loading_city_gcid = p.loading_city_gcid
      AND r.unloading_city_gcid = p.unloading_city_gcid
      AND r.body_group = p.body_group
      AND r.weight_category = p.weight_category
      AND r.month <= date_trunc('month', p.loading_date)::date
    ORDER BY r.month DESC
    LIMIT 1
) c ON true;

-- Качество модели по сегментам (на отложенном test без размеченных выбросов).
-- Иллюстрирует вывод R&D-отчёта: на узком сегменте R^2 падает при норм MAPE.
CREATE OR REPLACE VIEW mart.v_model_error_by_segment AS
WITH t AS (
    SELECT p.*,
           CASE WHEN p.distance < 300 THEN '1. до 300 км'
                WHEN p.distance < 1000 THEN '2. 300–1000 км'
                ELSE '3. 1000+ км' END AS distance_band,
           avg(p.actual_price_rub) OVER (PARTITION BY p.model_name, p.body_group,
               CASE WHEN p.distance < 300 THEN 1 WHEN p.distance < 1000 THEN 2 ELSE 3 END) AS seg_mean
    FROM mart.ml_price_predictions p
    WHERE p.split = 'test' AND NOT p.outlier_mark
)
SELECT model_name,
       body_group,
       distance_band,
       count(*)                                         AS n_rows,
       round(avg(ape_pct), 2)                           AS mape_pct,
       round(avg(abs_error_rub), 0)                     AS mae_rub,
       round((1 - sum((actual_price_rub - predicted_price_rub) ^ 2)
                  / NULLIF(sum((actual_price_rub - seg_mean) ^ 2), 0))::numeric, 3) AS r2
FROM t
GROUP BY model_name, body_group, distance_band;
