-- Связующие таблицы core: группы кузова и месяцы сделок. Перезапись по price_id сделок окна.

DELETE FROM core.fact_price_body_group WHERE price_id IN (SELECT id FROM stg_prices);
DELETE FROM core.fact_price_month      WHERE price_id IN (SELECT id FROM stg_prices);

-- Группы кузова. Источник — price_body_types (может быть несколько дочерних кодов 1С);
-- если связей нет — prices.body_type; если пусто и там — «Не указан».
INSERT INTO core.fact_price_body_group (price_id, body_group, body_std_id)
WITH valid AS (
    SELECT s.id, s.body_type,
           EXISTS (SELECT 1 FROM raw.price_body_types b WHERE b.price_id = s.id) AS has_links
    FROM stg_prices s
    WHERE cardinality(s.dq_reasons) = 0
),
links AS (
    SELECT v.id AS price_id, b.body_type_id
    FROM valid v
    JOIN raw.price_body_types b ON b.price_id = v.id
    UNION ALL
    SELECT v.id, v.body_type
    FROM valid v
    WHERE NOT v.has_links AND v.body_type IS NOT NULL
)
SELECT DISTINCT l.price_id, COALESCE(m.body_group, 'Прочие'), m.std_id
FROM links l
LEFT JOIN core.body_group_map m ON m.body_type_id = l.body_type_id
WHERE l.body_type_id IS NOT NULL
UNION ALL
SELECT v.id, 'Не указан', NULL
FROM valid v
WHERE NOT v.has_links AND v.body_type IS NULL;

-- Месяцы из price_dates (зерно витрины), одна строка на (сделка, месяц).
INSERT INTO core.fact_price_month (price_id, month, price_date)
SELECT pd.price_id, date_trunc('month', pd.price_date)::date, min(pd.price_date)
FROM raw.price_dates pd
JOIN stg_prices s ON s.id = pd.price_id AND cardinality(s.dq_reasons) = 0
WHERE pd.price_date IS NOT NULL
GROUP BY pd.price_id, date_trunc('month', pd.price_date);
