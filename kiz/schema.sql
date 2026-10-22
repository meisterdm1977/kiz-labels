CREATE TABLE IF NOT EXISTS users (
    id              serial PRIMARY KEY,
    name            text NOT NULL,
    role            text NOT NULL CHECK (role IN ('admin', 'operator', 'packer', 'station')),
    login           text UNIQUE,
    password_hash   text,
    code_hash       text UNIQUE,
    can_reprint     boolean NOT NULL DEFAULT true,
    active          boolean NOT NULL DEFAULT true,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS products (
    id          serial PRIMARY KEY,
    ean         text NOT NULL UNIQUE,
    article     text NOT NULL,
    size        text NOT NULL DEFAULT '',
    color       text NOT NULL DEFAULT '',
    search      text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS uploads (
    id          serial PRIMARY KEY,
    filename    text NOT NULL,
    user_id     integer REFERENCES users(id),
    status      text NOT NULL DEFAULT 'processing'
                CHECK (status IN ('processing', 'ready', 'failed', 'added', 'cancelled')),
    error       text,
    summary     jsonb,
    created_at  timestamptz NOT NULL DEFAULT now(),
    added_at    timestamptz
);

CREATE TABLE IF NOT EXISTS codes (
    id            bigserial PRIMARY KEY,
    product_id    integer NOT NULL REFERENCES products(id),
    upload_id     integer NOT NULL REFERENCES uploads(id),
    page_no       integer NOT NULL,
    code          text NOT NULL UNIQUE,
    code_prefix   text NOT NULL,
    status        text NOT NULL DEFAULT 'stock' CHECK (status IN ('stock', 'printed')),
    loaded_at     timestamptz NOT NULL DEFAULT now(),
    printed_at    timestamptz,
    printed_by    integer REFERENCES users(id),
    print_job_id  bigint,
    reprint_count integer NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS codes_stock_idx ON codes (product_id, loaded_at, id) WHERE status = 'stock';
CREATE INDEX IF NOT EXISTS codes_printed_idx ON codes (printed_at) WHERE status = 'printed';
CREATE INDEX IF NOT EXISTS codes_prefix_idx ON codes (code_prefix);

CREATE TABLE IF NOT EXISTS print_jobs (
    id          bigserial PRIMARY KEY,
    kind        text NOT NULL CHECK (kind IN ('print', 'reprint')),
    product_id  integer REFERENCES products(id),
    user_id     integer NOT NULL REFERENCES users(id),
    qty         integer NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS print_jobs_created_idx ON print_jobs (created_at);

CREATE TABLE IF NOT EXISTS print_job_codes (
    job_id      bigint NOT NULL REFERENCES print_jobs(id) ON DELETE CASCADE,
    code_id     bigint NOT NULL REFERENCES codes(id) ON DELETE CASCADE,
    position    integer NOT NULL,
    PRIMARY KEY (job_id, code_id)
);
CREATE INDEX IF NOT EXISTS print_job_codes_code_idx ON print_job_codes (code_id);

CREATE TABLE IF NOT EXISTS settings (
    key     text PRIMARY KEY,
    value   text NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id            serial PRIMARY KEY,
    name          text NOT NULL,
    token_hash    text NOT NULL UNIQUE,
    approved_by   integer REFERENCES users(id),
    active        boolean NOT NULL DEFAULT true,
    last_ip       text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_seen_at  timestamptz
);

-- Two roles remain: the packing computer became an approved device, operators became packers.
UPDATE users SET active = false WHERE role = 'station' AND active;
UPDATE users SET role = 'packer', login = NULL, password_hash = NULL WHERE role = 'operator';

-- Personal codes are not secret (they only identify who printed), so they are kept readable for the staff list.
ALTER TABLE users ADD COLUMN IF NOT EXISTS personal_code text;

-- A deleted upload stays in the list and the journal as a record of who deleted it; its codes are gone.
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
ALTER TABLE uploads ADD COLUMN IF NOT EXISTS deleted_by integer REFERENCES users(id);
ALTER TABLE uploads DROP CONSTRAINT IF EXISTS uploads_status_check;
ALTER TABLE uploads ADD CONSTRAINT uploads_status_check
    CHECK (status IN ('processing', 'ready', 'failed', 'added', 'cancelled', 'deleted'));
