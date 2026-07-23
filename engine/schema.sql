-- Open Outcry arena engine schema. Append-only bias: fills, ticks, marks,
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

create table if not exists triggers_fired (
  id bigserial primary key,
  agent_id text not null references agents(id),
  kind text not null,  -- stop_filled | drawdown | catalyst | watchlist_granted
  details jsonb not null default '{}'::jsonb,
  ts timestamptz not null default now(),
  handled boolean not null default false
);
