-- Applied to existing Tool System V2 Supabase project on 2026-10-18.
-- No new project/bucket. Profile data lives encrypted in PRIVATE money-ai-state.
begin;
create table if not exists public.money_ai_chrome_profiles (
  name text primary key check (name = 'owner'),
  manifest_path text not null
    check (manifest_path ~ '^chrome/manifests/[0-9a-f]{32}/manifest[.]json$'),
  manifest_sha256 text not null check (manifest_sha256 ~ '^[0-9a-f]{64}$'),
  byte_size bigint not null check (byte_size > 0 and byte_size <= 262144),
  updated_at timestamptz not null default now()
);
alter table public.money_ai_chrome_profiles enable row level security;
revoke all on public.money_ai_chrome_profiles from anon, authenticated;
grant select, insert, update on public.money_ai_chrome_profiles to service_role;
commit;
