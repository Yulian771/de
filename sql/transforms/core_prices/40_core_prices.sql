-- Загрузка core.fact_price: UPSERT валидных сделок окна + удаление сделок, ставших невалидными

DELETE FROM core.fact_price f
WHERE f.price_id IN (SELECT id FROM stg_prices WHERE cardinality(dq_reasons) > 0);

INSERT INTO core.fact_price (
    price_id, created_at, updated_at, loading_date, end_loading_date, loading_city_gcid, unloading_city_gcid,
    distance, total_weight, total_volume, body_type, body_group, price, currency_code, rate_date, fx_rate,
    price_rub, price_type, is_fact, complex, organization_id, outlier_mark, weight_category, volume_category,
    weight_category_filled, volume_category_filled, loaded_at)
SELECT s.id, s.created_at, s.updated_at, s.loading_date, s.end_loading_date, s.loading_city_gcid, s.unloading_city_gcid,
       s.distance, s.total_weight, s.total_volume, s.body_type, s.primary_body_group, s.price, s.currency_code,
       s.nearest_rate_date, s.fx_rate, s.price_rub, s.price_type, s.is_fact, s.complex, s.organization_id,
       s.outlier_mark, s.weight_category_final, s.volume_category_final,
       s.weight_category_filled, s.volume_category_filled, now()
FROM stg_prices s
WHERE cardinality(s.dq_reasons) = 0
ON CONFLICT (price_id) DO UPDATE SET
    created_at             = EXCLUDED.created_at,
    updated_at             = EXCLUDED.updated_at,
    loading_date           = EXCLUDED.loading_date,
    end_loading_date       = EXCLUDED.end_loading_date,
    loading_city_gcid      = EXCLUDED.loading_city_gcid,
    unloading_city_gcid    = EXCLUDED.unloading_city_gcid,
    distance               = EXCLUDED.distance,
    total_weight           = EXCLUDED.total_weight,
    total_volume           = EXCLUDED.total_volume,
    body_type              = EXCLUDED.body_type,
    body_group             = EXCLUDED.body_group,
    price                  = EXCLUDED.price,
    currency_code          = EXCLUDED.currency_code,
    rate_date              = EXCLUDED.rate_date,
    fx_rate                = EXCLUDED.fx_rate,
    price_rub              = EXCLUDED.price_rub,
    price_type             = EXCLUDED.price_type,
    is_fact                = EXCLUDED.is_fact,
    complex                = EXCLUDED.complex,
    organization_id        = EXCLUDED.organization_id,
    outlier_mark           = EXCLUDED.outlier_mark,
    weight_category        = EXCLUDED.weight_category,
    volume_category        = EXCLUDED.volume_category,
    weight_category_filled = EXCLUDED.weight_category_filled,
    volume_category_filled = EXCLUDED.volume_category_filled,
    loaded_at              = EXCLUDED.loaded_at;
