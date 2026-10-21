-- Archiving is a terminal room transition. Keep the board and roster for
-- history, but release every pairing code and badge binding. PowerSync sends
-- the existing cancelled status to badges before they return to pairing.
create or replace function public.archive_room(target uuid)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  actor uuid := auth.uid();
  host uuid;
begin
  if actor is null then
    raise exception 'sign in required';
  end if;

  -- Match join_room's room lock and submit_move's row lock so an archive
  -- cannot race a join or let a move commit after the terminal transition.
  perform pg_advisory_xact_lock(hashtextextended(target::text, 0));
  select host_user_id into host
  from public.rooms
  where id = target
  for update;
  if host is null or host <> actor then
    raise exception 'only the host can archive this room';
  end if;

  -- Change status first: clearing paired_at fires the roster trigger, which
  -- must never restart a waiting room while its badges are being removed.
  update public.rooms
  set status = 'cancelled',
      finished_at = coalesce(finished_at, now())
  where id = target;

  update public.room_players
  set pairing_code = null,
      device_id = null,
      paired_at = null
  where room_id = target
    and (pairing_code is not null or device_id is not null or paired_at is not null);

  return jsonb_build_object('room_id', target, 'status', 'cancelled');
end;
$$;

revoke all on function public.archive_room(uuid) from public, anon, authenticated;
grant execute on function public.archive_room(uuid) to authenticated, service_role;
