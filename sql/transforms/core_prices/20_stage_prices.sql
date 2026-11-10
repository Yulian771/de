-- Staging сделок: нормализация (курс, руб., категории, группа кузова) и коды причин DQ.


CREATE TEMP TABLE stg_prices ON COMMIT DROP AS
WITH p AS (
    SELECT r.*
    FROM raw.prices r
    WHERE r.updated_at >= current_setting('etl.window_start')::timestamp
      AND r.updated_at <  current_setting('etl.window_end_excl')::timestamp
),
enriched AS (
    SELECT p.*,
           p.currency_code IN ('840', '978')        AS is_foreign,
           -- Курс строго на сохранённую nearest_rate_date
           CASE WHEN p.currency_code = '643' THEN 1.0::float8 ELSE er.value END AS fx_rate,
           er.value IS NOT NULL                    AS fx_found,
           lc.gc_id IS NOT NULL                    AS lc_known,
           uc.gc_id IS NOT NULL                    AS uc_known,
           CASE WHEN p.body_type IS NULL THEN 'Не указан'
                ELSE COALESCE(bm.body_group, 'Прочие') END AS primary_body_group
    FROM p
    LEFT JOIN core.exchange_rate er ON er.num_code = p.currency_code AND er.setting_date = p.nearest_rate_date
    LEFT JOIN core.dim_city lc      ON lc.gc_id = p.loading_city_gcid
    LEFT JOIN core.dim_city uc      ON uc.gc_id = p.unloading_city_gcid
    LEFT JOIN core.body_group_map bm ON bm.body_type_id = p.body_type
)
SELECT e.*,
       e.price * e.fx_rate                                         AS price_rub,
       COALESCE(e.weight_category, core.fn_weight_category(e.total_weight)) AS weight_category_final,
       COALESCE(e.volume_category, core.fn_volume_category(e.total_volume)) AS volume_category_final,
       e.weight_category IS NULL                                   AS weight_category_filled,
       e.volume_category IS NULL                                   AS volume_category_filled,
       array_remove(ARRAY[
           CASE WHEN e.price IS NULL OR e.distance IS NULL OR e.total_weight IS NULL OR e.total_volume IS NULL
                  OR e.loading_city_gcid IS NULL OR e.unloading_city_gcid IS NULL OR e.currency_code IS NULL
                  OR e.price_type IS NULL OR e.is_fact IS NULL OR e.complex IS NULL OR e.outlier_mark IS NULL
                THEN 'REQUIRED_FIELD_NULL' END,
           CASE WHEN e.price <= 0 THEN 'PRICE_NON_POSITIVE' END,
           CASE WHEN e.currency_code = '643' AND e.price > 0 AND e.price <= 1000 THEN 'PRICE_RUB_BELOW_MIN' END,
           CASE WHEN e.distance <= 0 THEN 'DISTANCE_NON_POSITIVE' END,
           CASE WHEN e.total_weight < 0 THEN 'WEIGHT_NEGATIVE' END,
           CASE WHEN e.total_volume < 0 THEN 'VOLUME_NEGATIVE' END,
           CASE WHEN e.loading_city_gcid IS NOT NULL AND NOT e.lc_known THEN 'LOADING_CITY_UNKNOWN' END,
           CASE WHEN e.unloading_city_gcid IS NOT NULL AND NOT e.uc_known THEN 'UNLOADING_CITY_UNKNOWN' END,
           CASE WHEN e.currency_code NOT IN ('643', '840', '978') THEN 'CURRENCY_UNKNOWN' END,
           CASE WHEN e.is_foreign AND e.nearest_rate_date IS NULL THEN 'FX_RATE_DATE_MISSING' END,
           CASE WHEN e.is_foreign AND e.nearest_rate_date IS NOT NULL AND NOT e.fx_found THEN 'FX_RATE_NOT_FOUND' END
       ], NULL)                                                    AS dq_reasons
FROM enriched e;

CREATE INDEX ON stg_prices (id);
ANALYZE stg_prices;
