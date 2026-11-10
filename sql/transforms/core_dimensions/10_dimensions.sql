-- Справочники: raw -> core (upsert).

INSERT INTO core.dim_city (gc_id, city_id, full_name_local, city_name, region_name, latitude, longitude, duplicates_cnt, updated_at)
SELECT d.gc_id,
       d.id,
       d.full_name_local,
       btrim(split_part(d.full_name_local, ',', 1)),
       NULLIF(btrim(CASE WHEN strpos(d.full_name_local, ',') > 0
                         THEN substr(d.full_name_local, strpos(d.full_name_local, ',') + 1) END), ''),
       d.latitude,
       d.longitude,
       d.duplicates_cnt,
       now()
FROM (
    SELECT DISTINCT ON (gc_id) c.*, count(*) OVER (PARTITION BY gc_id) AS duplicates_cnt
    FROM raw.cities c
    WHERE c.gc_id IS NOT NULL
    ORDER BY gc_id, id
) d
ON CONFLICT (gc_id) DO UPDATE SET
    city_id         = EXCLUDED.city_id,
    full_name_local = EXCLUDED.full_name_local,
    city_name       = EXCLUDED.city_name,
    region_name     = EXCLUDED.region_name,
    latitude        = EXCLUDED.latitude,
    longitude       = EXCLUDED.longitude,
    duplicates_cnt  = EXCLUDED.duplicates_cnt,
    updated_at      = now();

-- Типы кузова с бизнес-группой; неизвестные коды -> «Прочие».
INSERT INTO core.dim_body_type (body_type_id, name, std_id, body_group, updated_at)
SELECT b.id, b.name, b.std_id, COALESCE(m.body_group, 'Прочие'), now()
FROM raw.body_types b
LEFT JOIN core.body_group_map m ON m.body_type_id = b.id
ON CONFLICT (body_type_id) DO UPDATE SET
    name       = EXCLUDED.name,
    std_id     = EXCLUDED.std_id,
    body_group = EXCLUDED.body_group,
    updated_at = now();

-- Курсы ЦБ: при дублях (num_code, setting_date) берём последнюю загруженную запись.
INSERT INTO core.exchange_rate (num_code, setting_date, value)
SELECT DISTINCT ON (num_code, setting_date) num_code, setting_date, value
FROM raw.exchange_rates
WHERE num_code IS NOT NULL AND setting_date IS NOT NULL AND value IS NOT NULL
ORDER BY num_code, setting_date, id DESC
ON CONFLICT (num_code, setting_date) DO UPDATE SET value = EXCLUDED.value;
