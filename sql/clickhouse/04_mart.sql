-- Витрина карточки маршрута в ClickHouse — повторяет пересчёты витрины PostgreSQL по месяцам.

INSERT INTO $stage_table
WITH agg AS
(
    SELECT month, loading_city_gcid, unloading_city_gcid, body_group, weight_category,
           any($as_ofs[indexOf($months, month)])                    AS cell_as_of,
           max(body_std_id)                                         AS body_std_id,
           quantileExactInclusive(0.25)(price_rub)                  AS p25_trip,
           quantileExactInclusive(0.50)(price_rub)                  AS median_trip,
           quantileExactInclusive(0.75)(price_rub)                  AS p75_trip,
           avg(price_rub)                                           AS avg_trip,
           quantileExactInclusive(0.25)(price_km)                   AS p25_km,
           quantileExactInclusive(0.50)(price_km)                   AS median_km,
           quantileExactInclusive(0.75)(price_km)                   AS p75_km,
           uniqExact(price_id)                                      AS trips_count,
           uniqExactIf(organization_id, price_type IN (3, 9))       AS carriers_count,
           if(count() >= 2 AND avg(price_rub) != 0, stddevSamp(price_rub) / avg(price_rub), NULL) AS cv_rate,
           max(price_date)                                          AS last_price_date,
           addMonths(month, -1)                                     AS prev_month,
           addMonths(month, -12)                                    AS prev_year_month
    FROM $db.market_base_current
    WHERE has($months, month)
      AND price_date <= $as_ofs[indexOf($months, month)]
    GROUP BY month, loading_city_gcid, unloading_city_gcid, body_group, weight_category
),
prev AS
(
    SELECT month, loading_city_gcid, unloading_city_gcid, body_group, weight_category, median_trip FROM agg
    UNION ALL
    SELECT month, loading_city_gcid, unloading_city_gcid, body_group, weight_category, median_trip
    FROM $db.mart_route_card
    WHERE has($keep_months, month)
)
SELECT c.month, c.loading_city_gcid, c.unloading_city_gcid, c.body_group, c.weight_category,
       dictGetOrNull('$db.dict_city', 'city_name', toUInt64(c.loading_city_gcid))     AS loading_city,
       dictGetOrNull('$db.dict_city', 'region_name', toUInt64(c.loading_city_gcid))   AS loading_region,
       dictGetOrNull('$db.dict_city', 'city_name', toUInt64(c.unloading_city_gcid))   AS unloading_city,
       dictGetOrNull('$db.dict_city', 'region_name', toUInt64(c.unloading_city_gcid)) AS unloading_region,
       c.body_std_id,
       c.p25_trip, c.median_trip, c.p75_trip, c.avg_trip, c.p25_km, c.median_km, c.p75_km,
       c.trips_count, c.carriers_count, c.cv_rate,
       if(pm.median_trip IS NULL OR pm.median_trip = 0, NULL, (c.median_trip / pm.median_trip - 1) * 100) AS mom_pct,
       if(py.median_trip IS NULL OR py.median_trip = 0, NULL, (c.median_trip / py.median_trip - 1) * 100) AS yoy_pct,
       c.last_price_date,
       dateDiff('day', c.last_price_date, c.cell_as_of)         AS freshness_days,
       multiIf(c.trips_count >= 30 AND c.carriers_count >= 5 AND freshness_days <= 30, 'HIGH',
               c.trips_count >= 10, 'MEDIUM', 'LOW_DATA')       AS reliability_level,
       c.cell_as_of                                             AS as_of,
       $refreshed[indexOf($months, c.month)]                    AS pg_updated_at
FROM agg AS c
LEFT JOIN prev AS pm
       ON pm.loading_city_gcid = c.loading_city_gcid AND pm.unloading_city_gcid = c.unloading_city_gcid
      AND pm.body_group = c.body_group AND pm.weight_category = c.weight_category AND pm.month = c.prev_month
LEFT JOIN prev AS py
       ON py.loading_city_gcid = c.loading_city_gcid AND py.unloading_city_gcid = c.unloading_city_gcid
      AND py.body_group = c.body_group AND py.weight_category = c.weight_category AND py.month = c.prev_year_month
SETTINGS join_use_nulls = 1;
