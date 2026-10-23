-- Tool System V2 media bucket for generated reels and source samples.
-- Apply only to the separate Tool System V2 Supabase project.
-- Private storage; no authenticated/anonymous object policies.
-- 50 MiB per object for free tier, no files uploaded by migration.
insert into storage.buckets (
    id, name, public, file_size_limit, allowed_mime_types
)
values (
    'money-ai-media', 'money-ai-media', false, 52428800,
    array['video/mp4','image/png','image/jpeg','image/webp']::text[]
)
on conflict (id) do nothing;
