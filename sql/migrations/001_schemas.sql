-- Слои хранилища: raw -> core (очищенные факты) -> mart (витрины для BI и ML).
-- dq — карантин отбракованных строк, 
-- etl — служебные таблицы пайплайна.
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS dq;
CREATE SCHEMA IF NOT EXISTS mart;
CREATE SCHEMA IF NOT EXISTS etl;

-- Журнал запусков: одна строка на транзакцию пайпа (окно, статус, статистика).
CREATE TABLE IF NOT EXISTS etl.run_log (
    run_id       bigserial PRIMARY KEY,
    step         text        NOT NULL,
    window_start date,
    window_end   date,
    status       text        NOT NULL DEFAULT 'running',
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    stats        jsonb
);
