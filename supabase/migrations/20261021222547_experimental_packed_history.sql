-- Local experimental projection support. Existing rooms/moves and badge_game_v2
-- remain the canonical/default protocol; enabling a packed stream is separate.
-- APPLYING THIS MIGRATION CHANGES THE DEFAULT SERVER INSERT PATH IMMEDIATELY:
-- it replaces moves_recompute and submit_move for every normal game. The word
-- "experimental" in the filename does not opt it out of supabase db push.
-- Stream rollout remains separate; this file has only been qualified locally.
-- Filename generated locally after the CLI was unavailable; no remote operation.
alter table public.rooms add column edge_history text not null default '';

-- Keep the original full replay available as the reference/rebuild path.
alter function public.recompute_room(uuid) rename to recompute_room_replay;
create function public.recompute_room(target uuid)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  perform public.recompute_room_replay(target);
  update public.rooms
  set edge_history = coalesce((
    select string_agg(substr('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklm',
      1 + case when orientation = 'h' then row * 5 + col else 25 + row * 6 + col end, 1),
      '' order by move_index)
    from public.moves where room_id = target
  ), '')
  where id = target;
end;
$$;
revoke all on function public.recompute_room(uuid) from public, anon, authenticated;
revoke all on function public.recompute_room_replay(uuid) from public, anon, authenticated;

-- Refuse inconsistent legacy data instead of silently encoding another board.
do $$
begin
  if exists (
    select room_id from public.moves group by room_id
    having min(move_index) <> 1 or max(move_index) <> count(*) or count(*) > 49
  ) or exists (
    select 1 from public.moves where not (
      (orientation = 'h' and row between 0 and 4 and col between 0 and 4)
      or (orientation = 'v' and row between 0 and 3 and col between 0 and 5)
    )
  ) then
    raise exception 'invalid legacy move history; rebuild before enabling packed history';
  end if;
end;
$$;
update public.rooms room
set edge_history = coalesce((
  select string_agg(substr('0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklm',
    1 + case when orientation = 'h' then row * 5 + col else 25 + row * 6 + col end, 1),
    '' order by move_index)
  from public.moves where room_id = room.id
), '');
alter table public.rooms add constraint rooms_edge_history_bounds
  check (edge_history ~ '^[0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklm]{0,49}$');

-- Apply only the newly inserted edge. An edge can complete at most two boxes;
-- before this edge existed neither adjacent box could already be complete.
create or replace function public.moves_recompute()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
declare
  room public.rooms%rowtype;
  alphabet constant text := '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklm';
  history text;
  edge integer;
  symbol text;
  completed integer := 0;
  box_row integer;
  box_col integer;
  top_edge integer;
  bottom_edge integer;
  left_edge integer;
  right_edge integer;
  next_seat integer;
  score_a integer;
  score_b integer;
begin
  select * into room from public.rooms where id = new.room_id for update;
  if not found then raise exception 'room missing'; end if;
  if room.status <> 'playing' then raise exception 'the game has not started'; end if;
  if new.seat <> room.current_seat then raise exception 'move out of turn'; end if;
  if new.move_index <> room.move_count + 1
     or char_length(room.edge_history) <> room.move_count then
    raise exception 'move history is not consecutive';
  end if;
  if new.orientation = 'h' and new.row between 0 and 4 and new.col between 0 and 4 then
    edge := new.row * 5 + new.col;
  elsif new.orientation = 'v' and new.row between 0 and 3 and new.col between 0 and 5 then
    edge := 25 + new.row * 6 + new.col;
  else
    raise exception 'edge outside the board';
  end if;
  symbol := substr(alphabet, edge + 1, 1);
  if strpos(room.edge_history, symbol) <> 0 then raise exception 'edge taken'; end if;
  history := room.edge_history || symbol;

  for box_row, box_col in
    select r, c from (values
      (case when new.orientation = 'h' then new.row - 1 else new.row end,
       case when new.orientation = 'v' then new.col - 1 else new.col end),
      (new.row, new.col)
    ) adjacent(r, c) where r between 0 and 3 and c between 0 and 4
  loop
    top_edge := box_row * 5 + box_col;
    bottom_edge := top_edge + 5;
    left_edge := 25 + box_row * 6 + box_col;
    right_edge := left_edge + 1;
    if strpos(history, substr(alphabet, top_edge + 1, 1)) > 0
       and strpos(history, substr(alphabet, bottom_edge + 1, 1)) > 0
       and strpos(history, substr(alphabet, left_edge + 1, 1)) > 0
       and strpos(history, substr(alphabet, right_edge + 1, 1)) > 0 then
      completed := completed + 1;
    end if;
  end loop;
  next_seat := case when completed > 0 then new.seat else 3 - new.seat end;
  score_a := room.score_p1 + case when new.seat = 1 then completed else 0 end;
  score_b := room.score_p2 + case when new.seat = 2 then completed else 0 end;
  update public.rooms
  set edge_history = history, move_count = new.move_index,
      current_seat = next_seat, score_p1 = score_a, score_p2 = score_b,
      winner = case when new.move_index < 49 then null
                    when score_a = score_b then 0 when score_a > score_b then 1 else 2 end,
      status = case when new.move_index = 49 then 'finished' else room.status end,
      finished_at = case when new.move_index = 49 then coalesce(room.finished_at, now()) else room.finished_at end
  where id = new.room_id;
  if new.move_index = 49 then
    update public.room_players set pairing_code = null
    where room_id = new.room_id and pairing_code is not null;
  end if;
  return null;
end;
$$;
revoke all on function public.moves_recompute() from public, anon, authenticated;

-- The room row already serializes normal RPC inserts. Its authoritative count
-- supplies the next index, and the duplicate check runs after taking that lock.
create or replace function public.submit_move(
  move_id uuid, target uuid, orientation text, row_i integer, col_i integer
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $$
declare
  room public.rooms%rowtype;
  actor_seat integer;
  next_index integer;
begin
  if auth.uid() is null then raise exception 'sign in required'; end if;
  select * into room from public.rooms where id = target for update;
  select seat into actor_seat from public.room_players
  where room_id = target and user_id = auth.uid();
  if actor_seat is null then raise exception 'not your turn'; end if;
  if exists (select 1 from public.moves where id = move_id and room_id = target and seat = actor_seat) then
    return jsonb_build_object('ok', true, 'duplicate', true);
  end if;
  if room.id is null or room.status <> 'playing' then raise exception 'the game has not started'; end if;
  if actor_seat <> room.current_seat then raise exception 'not your turn'; end if;
  if orientation not in ('h', 'v') then raise exception 'bad orientation'; end if;
  next_index := room.move_count + 1;
  insert into public.moves (id, room_id, seat, orientation, row, col, move_index)
  values (move_id, target, actor_seat, orientation, row_i, col_i, next_index);
  return jsonb_build_object('ok', true, 'move_index', next_index);
end;
$$;
revoke all on function public.submit_move(uuid, uuid, text, integer, integer) from public, anon;
grant execute on function public.submit_move(uuid, uuid, text, integer, integer) to authenticated, service_role;

-- UNIQUE(room_id,move_index) already supplies the same ordered B-tree.
drop index public.moves_room;
