-- Run this once in the Supabase SQL editor (Project > SQL Editor > New query)

create table if not exists fund_snapshots (
  id uuid primary key default gen_random_uuid(),
  fund_name text not null,
  person text,
  cik text not null,
  period_end date not null,
  filed_date date,
  portfolio_value numeric,
  num_holdings int,
  top_holdings jsonb,
  source_accession text,
  updated_at timestamptz default now(),
  unique (cik, period_end)
);

-- Let the public frontend read data with the anon key, but never write.
alter table fund_snapshots enable row level security;

create policy "Public read access"
  on fund_snapshots
  for select
  using (true);

-- No insert/update/delete policy is created for anon/authenticated roles.
-- Only the service_role key (used by the GitHub Action) can write, because
-- service_role bypasses RLS entirely.
