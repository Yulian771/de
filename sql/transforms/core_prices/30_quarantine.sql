-- Карантин сделок: перезапись по price_id (DELETE + INSERT) — повторный запуск не создаёт дублей,
-- а сделка, исправленная в источнике, уходит из карантина.

DELETE FROM dq.dq_quarantine_prices
WHERE price_id IN (SELECT id FROM stg_prices);

INSERT INTO dq.dq_quarantine_prices (price_id, reason_code, reason_details, loading_date, currency_code, price, raw_record)
SELECT s.id,
       r.reason_code,
       CASE r.reason_code
           WHEN 'PRICE_NON_POSITIVE'     THEN format('price=%s', s.price)
           WHEN 'PRICE_RUB_BELOW_MIN'    THEN format('price=%s RUB <= 1000', s.price)
           WHEN 'DISTANCE_NON_POSITIVE'  THEN format('distance=%s', s.distance)
           WHEN 'WEIGHT_NEGATIVE'        THEN format('total_weight=%s', s.total_weight)
           WHEN 'VOLUME_NEGATIVE'        THEN format('total_volume=%s', s.total_volume)
           WHEN 'LOADING_CITY_UNKNOWN'   THEN format('loading_city_gcid=%s', s.loading_city_gcid)
           WHEN 'UNLOADING_CITY_UNKNOWN' THEN format('unloading_city_gcid=%s', s.unloading_city_gcid)
           WHEN 'CURRENCY_UNKNOWN'       THEN format('currency_code=%s', s.currency_code)
           WHEN 'FX_RATE_DATE_MISSING'   THEN format('currency_code=%s, nearest_rate_date IS NULL', s.currency_code)
           WHEN 'FX_RATE_NOT_FOUND'      THEN format('no rate %s on %s', s.currency_code, s.nearest_rate_date)
       END,
       s.loading_date,
       s.currency_code,
       s.price,
       -- Исходная запись — поиском по ключу только для отбракованных строк
       (SELECT to_jsonb(rp) - '_extracted_at' FROM raw.prices rp WHERE rp.id = s.id)
FROM stg_prices s
CROSS JOIN LATERAL unnest(s.dq_reasons) AS r(reason_code);
