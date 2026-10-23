-- Tool System V2 crash-safe SQLite checkpoints in a dedicated private bucket.
-- Apply only to the separate Tool System V2 Supabase project.
begin;
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('money-ai-state', 'money-ai-state', false, 26214400,
        array['application/octet-stream']::text[])
on conflict (id) do nothing;
create table if not exists public.money_ai_checkpoints (
  name text primary key check (name = 'primary'),
  object_path text not null check (object_path like 'snapshots/%'),
  sha256 text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  byte_size bigint not null check (byte_size > 0 and byte_size <= 26214400),
  updated_at timestamptz not null default now()
);
alter table public.money_ai_checkpoints enable row level security;
revoke all on public.money_ai_checkpoints from anon, authenticated;
grant select, insert, update on public.money_ai_checkpoints to service_role;
commit;
