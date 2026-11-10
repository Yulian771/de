-- RAW: копия источника deals_bigdata. Без ограничений целостности —
-- raw должен принять любые данные, проверки выполняются при переходе в core.
-- Сделки загружаются окнами по метке изменения (updated_at) и перезаписываются по id вместе
-- с дочерними строками: raw — зеркало текущего состояния источника. Технический столбец _extracted_at —
-- когда строка выгружена. Transform читает сделки окна по тому же updated_at

CREATE TABLE IF NOT EXISTS raw.prices (
    id                  bigint PRIMARY KEY,
    created_at          timestamp(0),
    updated_at          timestamp(0),
    subscriber_id       integer,
    organization_id     integer,
    loading_date        date,
    loading_city_gcid   integer,
    unloading_city_gcid integer,
    distance            double precision,
    total_weight        double precision,
    total_volume        double precision,
    body_type           integer,
    price               double precision,
    currency_code       varchar(3),
    complex             boolean,
    price_type          smallint,
    item_id             integer,
    is_fact             boolean,
    nearest_rate_date   date,
    meta_item_id        bigint,
    end_loading_date    date,
    source              varchar(255),
    external_item_uuid  uuid,
    outlier_mark        boolean,
    weight_category     varchar(4),
    volume_category     varchar(4),
    price_group_id      bigint,
    _extracted_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS raw_prices_updated_at_idx ON raw.prices (updated_at);

CREATE TABLE IF NOT EXISTS raw.price_body_types (
    id            bigint PRIMARY KEY,
    price_id      bigint,
    body_type_id  integer,
    _extracted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS raw_price_body_types_price_idx ON raw.price_body_types (price_id);

CREATE TABLE IF NOT EXISTS raw.price_dates (
    id            bigint PRIMARY KEY,
    price_id      bigint,
    price_date    date,
    _extracted_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS raw_price_dates_price_idx ON raw.price_dates (price_id);

CREATE TABLE IF NOT EXISTS raw.cities (
    id              bigint PRIMARY KEY,
    full_name_local varchar(255),
    full_name_gc    varchar(255),
    gc_id           integer,
    name            varchar(255),
    longitude       double precision,
    latitude        double precision,
    created_at      timestamp(0),
    updated_at      timestamp(0),
    _extracted_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.body_types (
    id            bigint PRIMARY KEY,
    name          varchar(255),
    "id_from_1C"  integer,
    std_id        integer,
    _extracted_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS raw.exchange_rates (
    id            bigint PRIMARY KEY,
    num_code      varchar(255),
    value         double precision,
    setting_date  date,
    _extracted_at timestamptz NOT NULL DEFAULT now()
);
