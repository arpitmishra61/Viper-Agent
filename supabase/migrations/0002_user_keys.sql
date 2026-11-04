-- Per-user API keys (OpenRouter, Hugging Face, E2B, ...). Run once in the Supabase SQL editor.
--
-- Values are stored ENCRYPTED (AES-256-GCM, done by the `user-keys` edge function with the
-- KEYS_ENCRYPTION_SECRET function secret), so a database dump or a dashboard peek shows only ciphertext.
-- Clients never touch this table: RLS is on with no policies, and only the edge function (service role) uses it.

create table if not exists public.user_keys (
  user_id    uuid        not null references auth.users (id) on delete cascade,
  name       text        not null check (name ~ '^[A-Z][A-Z0-9_]{0,63}$'),   -- e.g. OPENROUTER_API_KEY
  value_enc  text        not null,                                           -- base64(iv || ciphertext+tag)
  updated_at timestamptz not null default now(),
  primary key (user_id, name)
);

alter table public.user_keys enable row level security;
revoke all on public.user_keys from anon, authenticated;
