-- Полная синхронизация рыночной выборки

INSERT INTO $stage_table
SELECT month, price_date, price_id, loading_city_gcid, unloading_city_gcid, body_group, body_std_id,
       weight_category, price_rub, price_km, distance, price_type, organization_id, updated_at,
       $version AS version, 0 AS is_deleted
FROM postgresql(pg_dwh, table = 'v_market_base')
