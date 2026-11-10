-- Карантин. Одна строка на (сделка, причина).
CREATE TABLE IF NOT EXISTS dq.dq_quarantine_prices (
    price_id       bigint      NOT NULL,
    reason_code    text        NOT NULL,
    reason_details text,
    loading_date   date,
    currency_code  varchar(3),
    price          double precision,
    raw_record     jsonb       NOT NULL,
    quarantined_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (price_id, reason_code)
);
CREATE INDEX IF NOT EXISTS dq_quarantine_loading_date_idx ON dq.dq_quarantine_prices (loading_date);

-- Сводка карантина по причинам.
CREATE OR REPLACE VIEW dq.v_quarantine_summary AS
SELECT reason_code,
       count(*)                 AS rows_cnt,
       min(loading_date)        AS min_loading_date,
       max(loading_date)        AS max_loading_date
FROM dq.dq_quarantine_prices
GROUP BY reason_code;
