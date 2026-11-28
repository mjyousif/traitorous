-- Migration: 20261128000000_press_read_cursors.sql
-- Issue #453: Persist per-seat, per-channel press read cursors across devices.
-- Service-role-only RLS and monotonic update trigger so out-of-order writes never regress cursors.

create table if not exists public.press_read_cursors (
  game_id uuid not null references public.multiplayer_games(id) on delete cascade,
  power text not null check (power ~ '^[A-Z][A-Z0-9_]{1,31}$'),
  channel_key text not null check (btrim(channel_key) <> '' and length(channel_key) <= 128),
  last_read_seq integer not null check (last_read_seq >= 0),
  updated_at timestamp with time zone not null default timezone('utc'::text, now()),
  primary key (game_id, power, channel_key)
);

alter table public.press_read_cursors enable row level security;

drop policy if exists "Only service role can select press_read_cursors" on public.press_read_cursors;
create policy "Only service role can select press_read_cursors"
  on public.press_read_cursors for select
  to service_role
  using (true);

drop policy if exists "Only service role can insert press_read_cursors" on public.press_read_cursors;
create policy "Only service role can insert press_read_cursors"
  on public.press_read_cursors for insert
  to service_role
  with check (true);

drop policy if exists "Only service role can update press_read_cursors" on public.press_read_cursors;
create policy "Only service role can update press_read_cursors"
  on public.press_read_cursors for update
  to service_role
  using (true)
  with check (true);

drop policy if exists "Only service role can delete press_read_cursors" on public.press_read_cursors;
create policy "Only service role can delete press_read_cursors"
  on public.press_read_cursors for delete
  to service_role
  using (true);

create or replace function public.enforce_press_read_cursor_monotonic()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.last_read_seq < old.last_read_seq then
    new.last_read_seq := old.last_read_seq;
    new.updated_at := old.updated_at;
  end if;
  return new;
end;
$$;

revoke all on function public.enforce_press_read_cursor_monotonic() from public, anon, authenticated;

drop trigger if exists trg_press_read_cursors_monotonic on public.press_read_cursors;
create trigger trg_press_read_cursors_monotonic
  before update on public.press_read_cursors
  for each row
  execute function public.enforce_press_read_cursor_monotonic();
