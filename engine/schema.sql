-- Conviction League arena engine schema. Append-only bias: fills, ticks, marks,
-- runs, operations, triggers are never updated after insert (orders/positions
-- and agent_state carry current state).

create table if not exists agents (
  id text primary key,
  name text not null,
  archetype text not null,
  brain text not null default 'routine-claude',  -- routine-claude | antigravity-gemini
  config jsonb not null default '{}'::jsonb,     -- constitution caps etc.
  status text not null default 'active',
  created_at timestamptz not null default now()
);

-- seat-application ingestion (2026-07-23): interview-born agents carry an
-- owning principal and a cadence tier ('house' 2x/day · 'seated' 1x/day).
alter table agents add column if not exists owner_uid text;
alter table agents add column if not exists tier text not null default 'house';

create table if not exists agent_state (
  agent_id text primary key references agents(id),
  cash numeric not null,
  peak_equity numeric not null,
  launched date,
  bench jsonb not null  -- {symbols:[], weights:[], launch_prices:[]}
);

create table if not exists watchlist (
  symbol text primary key,          -- internal symbol (BTC-USD)
  source_symbol text not null,      -- data-source symbol (BINANCE:BTCUSDT)
  status text not null default 'active',
  requested_by text,
  added_at timestamptz not null default now()
);

-- open universe (2026-07-27): symbols are resolved against the data source
-- before they are granted, and carry the asset class that per-agent class caps
-- are enforced against. equity | etf | inverse_levered | crypto.
alter table watchlist add column if not exists asset_class text not null default 'equity';
alter table watchlist add column if not exists description text;

create table if not exists ticks (
  id bigserial primary key,
  symbol text not null,
  ts timestamptz not null,
  price numeric not null,
  prev_close numeric,
  source text not null default 'finnhub',
  unique (symbol, ts)
);
create index if not exists ticks_symbol_ts on ticks(symbol, ts desc);

-- range-aware fills (2026-07-29): the session extremes the data source
-- reported at each sample, recorded so every touched-since-last-look fire is
-- auditable against exactly the data it used.
alter table ticks add column if not exists high numeric;
alter table ticks add column if not exists low numeric;
alter table ticks add column if not exists day_open numeric;

create table if not exists positions (
  agent_id text references agents(id),
  symbol text,
  qty numeric not null,
  avg_fill numeric not null,
  opened_at date,
  thesis text,
  invalidation text,
  review_by date,
  primary key (agent_id, symbol)
);

create table if not exists orders (
  id bigserial primary key,
  agent_id text not null references agents(id),
  kind text not null check (kind in ('market','stop','limit','trailing_stop')),
  side text not null check (side in ('buy','sell')),
  symbol text not null,
  qty numeric,
  params jsonb not null default '{}'::jsonb,
  -- stop: {trigger_price} · trailing_stop: {trail_pct, high_water} · limit: {limit_price}
  status text not null default 'open' check (status in ('open','filled','canceled','rejected')),
  reason text,
  run_id bigint,
  created_at timestamptz not null default now(),
  closed_at timestamptz
);
create index if not exists orders_open on orders(agent_id) where status = 'open';

create table if not exists fills (
  id bigserial primary key,
  order_id bigint references orders(id),
  agent_id text not null,
  symbol text not null,
  side text not null,
  qty numeric not null,
  price numeric not null,       -- raw market price at execution
  fill_price numeric not null,  -- price after 0.15% cost, against the agent
  ts timestamptz not null default now()
);

create table if not exists equity_marks (
  agent_id text not null references agents(id),
  ts timestamptz not null,
  equity numeric not null,
  cash numeric not null,
  positions_value numeric not null,
  bench_index numeric,
  primary key (agent_id, ts)
);

create table if not exists runs (
  id bigserial primary key,
  agent_id text not null references agents(id),
  trigger text not null,  -- scheduled | stop_filled | drawdown | manual
  status text not null default 'started',
  started timestamptz not null default now(),
  finished timestamptz,
  cost_usd numeric,
  tokens_in bigint,
  tokens_out bigint,
  meta jsonb not null default '{}'::jsonb
);

create table if not exists operations (
  id bigserial primary key,
  run_id bigint references runs(id),
  seq int not null,
  type text not null,
  payload jsonb not null,
  verdict text not null check (verdict in ('accepted','rejected')),
  reason text,
  created_at timestamptz not null default now()
);

-- the desk (2026-07-28): what a principal files from their private page. A
-- note is a deliberation input with standing and no authority — the agent must
-- answer it at its next session, and may decline or refuse it with reasons.
create table if not exists guidance (
  id bigserial primary key,
  agent_id text not null references agents(id),
  cid text not null,                  -- C1, C2 … per agent
  uid text not null,                  -- the principal who filed it
  doc_id text not null unique,        -- the Firestore doc it arrived in
  text text not null,
  filed_at timestamptz not null default now(),
  disposition text check (disposition in ('adopted','converted','declined','refused')),
  answer text,
  answered_run bigint references runs(id),
  answered_at timestamptz,
  pushed_at timestamptz,              -- the answer is back on the principal's desk
  unique (agent_id, cid)
);
-- who wrote the note: the principal's own words, or the trader's summary of a
-- conversation it decided to carry. The record must never attribute one to the
-- other.
alter table guidance add column if not exists author text not null default 'principal';
create index if not exists guidance_pending on guidance(agent_id) where disposition is null;

create table if not exists triggers_fired (
  id bigserial primary key,
  agent_id text not null references agents(id),
  kind text not null,  -- stop_filled | drawdown | catalyst | watchlist_granted
  details jsonb not null default '{}'::jsonb,
  ts timestamptz not null default now(),
  handled boolean not null default false
);

-- letters (2026-07-30): the record of what left the building. A letter is the
-- one artifact of this arena that goes out to a person, and nothing here
-- recorded that it had — the only traces were a line in a CI log and a row in
-- a provider's dashboard that ages out. One row per trader per run, the quiet
-- ones included: silence is a fact about the strategy, and a missing row
-- cannot be told apart from a job that never ran. The principal's ADDRESS is
-- deliberately absent — it belongs to them and lives in Firestore; the uid
-- answers "who was written to" without republishing it. Append-only.
create table if not exists letters (
  id bigserial primary key,
  agent_id text not null references agents(id),
  day text not null,       -- the tape's own label, "Jul 28"
  occasion text not null,  -- close | reflection | welcome
  decision text not null check (decision in ('sent','quiet','refused','failed','dry')),
  reason text,             -- why it went out, or why the trader stayed quiet
  subject text,
  owner_uid text,
  provider_id text,        -- resend's message id: the receipt
  html text,               -- exactly what was rendered, so it can be re-read
  plain text,
  bytes int,
  error text,
  created_at timestamptz not null default now()
);
create index if not exists letters_agent on letters(agent_id, created_at desc);
create index if not exists letters_sent on letters(created_at desc) where decision = 'sent';
