-- Viper backend schema. Run once in the Supabase SQL editor (or `supabase db push`).

-- Free-tier counters, one row per signed-in user.
create table if not exists public.usage (
  user_id        uuid primary key references auth.users (id) on delete cascade,
  sandbox_grants int  not null default 0,
  updated_at     timestamptz not null default now()
);

-- Server-side secrets (the shared E2B key). Clients can never read this table.
create table if not exists public.app_secrets (
  name  text primary key,
  value text not null
);

alter table public.usage       enable row level security;
alter table public.app_secrets enable row level security;
-- No policies on purpose: with RLS on and no policy, anon and signed-in users get nothing.
-- Only the edge function (service role) reads or writes these tables.
revoke all on public.usage, public.app_secrets from anon, authenticated;

-- Atomically claim one free sandbox session. Returns granted=false once the limit is hit.
create or replace function public.claim_sandbox_grant(uid uuid, max_grants int)
returns table (granted boolean, used int)
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into usage (user_id) values (uid) on conflict (user_id) do nothing;

  update usage
     set sandbox_grants = sandbox_grants + 1, updated_at = now()
   where user_id = uid and sandbox_grants < max_grants
  returning sandbox_grants into used;

  if found then
    granted := true;
  else
    granted := false;
    select sandbox_grants into used from usage where user_id = uid;
  end if;
  return next;
end;
$$;

revoke all on function public.claim_sandbox_grant(uuid, int) from public, anon, authenticated;

-- After running this file, store the key (do not commit it anywhere):
--   insert into app_secrets (name, value) values ('E2B_API_KEY', 'e2b_...');
