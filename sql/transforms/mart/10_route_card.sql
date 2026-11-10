-- Витрина «Карточка маршрута и коридор рыночных ставок»: пересчёт месяцев (DELETE + INSERT в транзакции).
--
-- Инкремент: пересчитываются
--   * последние N месяцев до месяца as_of ;
--   * месяцы, посчитанные до своего окончания (as_of строки раньше последнего дня месяца), например если
--     ежедневные запуски стояли дольше N месяцев, и месяцы с рыночными сделками, которых в витрине ещё нет;
--   * следующий месяц и месяц через год после пересчитанных — от них зависят MoM и YoY.
-- 
-- Редкие глубокие правки старых месяцев подбирает периодический полный пересчёт (mart.full = 'true').

CREATE TEMP TABLE mart_months ON COMMIT DROP AS
WITH params AS (
    SELECT current_setting('mart.as_of')::date                AS as_of,
           current_setting('mart.full')::boolean              AS full_rebuild,
           current_setting('mart.lookback_months')::int       AS lookback
),
market_months AS (                                    -- месяцы, в которых есть рыночные сделки до as_of
    SELECT DISTINCT b.month
    FROM core.v_market_base b, params
    WHERE b.price_date <= params.as_of
),
mart_state AS (                                       -- что уже посчитано: месяц и самый ранний as_of его строк
    SELECT month, min(as_of) AS min_as_of
    FROM mart.mart_route_card
    GROUP BY month
),
base_months AS (
    SELECT m.month
    FROM market_months m
    CROSS JOIN params p
    LEFT JOIN mart_state s ON s.month = m.month
    WHERE p.full_rebuild
       OR m.month >= (date_trunc('month', p.as_of) - make_interval(months => p.lookback - 1))::date
       OR s.month IS NULL                                                        -- нового месяца нет в витрине
       OR s.min_as_of < (m.month + interval '1 month' - interval '1 day')::date  -- посчитан до окончания месяца
    UNION
    -- Месяцы витрины, где рыночных сделок больше нет
    SELECT s.month
    FROM mart_state s
    CROSS JOIN params p
    WHERE s.month NOT IN (SELECT month FROM market_months)
      AND (p.full_rebuild
           OR s.month >= (date_trunc('month', p.as_of) - make_interval(months => p.lookback - 1))::date
           OR s.month > date_trunc('month', p.as_of)::date)                      -- после сдвига as_of назад
)
SELECT month FROM base_months
UNION
SELECT d.month                                        -- соседи по MoM / YoY, если они есть
FROM (SELECT (month + interval '1 month')::date AS month FROM base_months
      UNION SELECT (month + interval '1 year')::date FROM base_months) d
WHERE d.month IN (SELECT month FROM market_months UNION SELECT month FROM mart_state);

DELETE FROM mart.mart_route_card WHERE month IN (SELECT month FROM mart_months);

INSERT INTO mart.mart_route_card (
    month, loading_city_gcid, unloading_city_gcid, body_group, weight_category,
    loading_city, loading_region, unloading_city, unloading_region, body_std_id,
    p25_trip, median_trip, p75_trip, avg_trip, p25_km, median_km, p75_km,
    trips_count, carriers_count, cv_rate, last_price_date, freshness_days, reliability_level,
    mom_pct, yoy_pct, as_of, updated_at)
WITH params AS (
    SELECT current_setting('mart.as_of')::date AS as_of
),
base AS (
    SELECT b.*
    FROM core.v_market_base b, params
    WHERE b.month IN (SELECT month FROM mart_months)
      AND b.price_date <= params.as_of
),
agg AS (
    SELECT month, loading_city_gcid, unloading_city_gcid, body_group, weight_category,
           max(body_std_id)                                                  AS body_std_id,
           percentile_cont(0.25) WITHIN GROUP (ORDER BY price_rub)           AS p25_trip,
           percentile_cont(0.50) WITHIN GROUP (ORDER BY price_rub)           AS median_trip,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY price_rub)           AS p75_trip,
           avg(price_rub)                                                    AS avg_trip,
           percentile_cont(0.25) WITHIN GROUP (ORDER BY price_km)            AS p25_km,
           percentile_cont(0.50) WITHIN GROUP (ORDER BY price_km)            AS median_km,
           percentile_cont(0.75) WITHIN GROUP (ORDER BY price_km)            AS p75_km,
           count(DISTINCT price_id)                                          AS trips_count,
           count(DISTINCT organization_id) FILTER (WHERE price_type IN (3, 9)) AS carriers_count,
           CASE WHEN count(*) >= 2 AND avg(price_rub) <> 0
                THEN stddev_samp(price_rub) / avg(price_rub) END             AS cv_rate,
           max(price_date)                                                   AS last_price_date
    FROM base
    GROUP BY month, loading_city_gcid, unloading_city_gcid, body_group, weight_category
),
cell AS (
    SELECT a.*, round(a.median_trip::numeric, 2) AS median_trip_r
    FROM agg a
),
prev AS (
    SELECT month, loading_city_gcid, unloading_city_gcid, body_group, weight_category, median_trip_r
    FROM cell
    UNION ALL
    SELECT month, loading_city_gcid, unloading_city_gcid, body_group, weight_category, median_trip
    FROM mart.mart_route_card
)
SELECT c.month, c.loading_city_gcid, c.unloading_city_gcid, c.body_group, c.weight_category,
       lc.city_name, lc.region_name, uc.city_name, uc.region_name, c.body_std_id,
       round(c.p25_trip::numeric, 2), c.median_trip_r, round(c.p75_trip::numeric, 2),
       round(c.avg_trip::numeric, 2),
       round(c.p25_km::numeric, 2), round(c.median_km::numeric, 2), round(c.p75_km::numeric, 2),
       c.trips_count, c.carriers_count, round(c.cv_rate::numeric, 4),
       c.last_price_date,
       params.as_of - c.last_price_date,
       CASE WHEN c.trips_count >= 30 AND c.carriers_count >= 5 AND params.as_of - c.last_price_date <= 30 THEN 'HIGH'
            WHEN c.trips_count >= 10 THEN 'MEDIUM'
            ELSE 'LOW_DATA' END,
       -- MoM / YoY
       round(((c.median_trip_r / NULLIF(m.median_trip_r, 0)) - 1) * 100, 2),
       round(((c.median_trip_r / NULLIF(y.median_trip_r, 0)) - 1) * 100, 2),
       params.as_of,
       now()
FROM cell c
CROSS JOIN params
LEFT JOIN prev m ON m.month = (c.month - interval '1 month')::date
                AND (m.loading_city_gcid, m.unloading_city_gcid, m.body_group, m.weight_category)
                  = (c.loading_city_gcid, c.unloading_city_gcid, c.body_group, c.weight_category)
LEFT JOIN prev y ON y.month = (c.month - interval '1 year')::date
                AND (y.loading_city_gcid, y.unloading_city_gcid, y.body_group, y.weight_category)
                  = (c.loading_city_gcid, c.unloading_city_gcid, c.body_group, c.weight_category)
LEFT JOIN core.dim_city lc ON lc.gc_id = c.loading_city_gcid
LEFT JOIN core.dim_city uc ON uc.gc_id = c.unloading_city_gcid;
