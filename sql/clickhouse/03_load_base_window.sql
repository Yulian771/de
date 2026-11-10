-- Инкремент рыночной выборки за окно [start, end_excl) по updated_at

INSERT INTO $db.market_base
SELECT month, price_date, price_id, loading_city_gcid, unloading_city_gcid, body_group, body_std_id,
       weight_category, price_rub, price_km, distance, price_type, organization_id, updated_at,
       $version AS version, 1 AS is_deleted
FROM $db.market_base_current
WHERE price_id IN (
    SELECT id FROM postgresql(pg_dwh, schema = 'raw', table = 'prices')
    WHERE updated_at >= toDateTime('$start 00:00:00') AND updated_at < toDateTime('$end_excl 00:00:00')
);


INSERT INTO $db.market_base
SELECT month, price_date, price_id, loading_city_gcid, unloading_city_gcid, body_group, body_std_id,
       weight_category, price_rub, price_km, distance, price_type, organization_id, updated_at,
       $version + 1 AS version, 0 AS is_deleted
FROM postgresql(pg_dwh, table = 'v_market_base')
WHERE updated_at >= toDateTime('$start 00:00:00') AND updated_at < toDateTime('$end_excl 00:00:00');
