create extension if not exists pgcrypto;

create table if not exists public.calls (
  id uuid primary key default gen_random_uuid(),
  call_sid text unique not null,
  phone_number text,
  caller_name text,
  pg_for text,
  sharing_preference text,
  budget text,
  move_in_date text,
  occupation text,
  whatsapp_confirmation boolean,
  visit_interest text,
  lead_status text default 'needs_follow_up',
  sentiment text,
  outcome text,
  objections text,
  summary text,
  full_transcript text,
  lead_score integer,
  language text,
  intent_type text,
  conversation_stage text,
  customer_profile text,
  visit_day text,
  visit_time text,
  decision_maker text,
  enriched_memory jsonb,
  created_at timestamptz not null default now()
);

create index if not exists calls_created_at_idx on public.calls (created_at desc);
create index if not exists calls_call_sid_idx on public.calls (call_sid);
create index if not exists calls_lead_status_idx on public.calls (lead_status);

alter table public.calls add column if not exists lead_score integer;
alter table public.calls add column if not exists language text;
alter table public.calls add column if not exists intent_type text;
alter table public.calls add column if not exists conversation_stage text;
alter table public.calls add column if not exists customer_profile text;
alter table public.calls add column if not exists visit_day text;
alter table public.calls add column if not exists visit_time text;
alter table public.calls add column if not exists decision_maker text;
alter table public.calls add column if not exists enriched_memory jsonb;

create index if not exists calls_lead_score_idx on public.calls (lead_score desc);
create index if not exists calls_conversation_stage_idx on public.calls (conversation_stage);

-- Demo-friendly option for a backend using a publishable Supabase key.
-- For production, use a service-role key on the backend instead and keep RLS enabled.
alter table public.calls disable row level security;
