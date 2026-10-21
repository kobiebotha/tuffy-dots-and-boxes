-- Multiplayer dots and boxes.
-- A person may host or join one waiting/playing room. Finishing or cancelling
-- it frees them to open another. Pairing codes are five A/B/C letters (~8 bits)
-- and are cleared when the room ends so the short space can be reused.

create table public.rooms (
  id uuid primary key default gen_random_uuid(),
  host_user_id uuid not null,
  status text not null default 'waiting' check (status in ('waiting', 'playing', 'finished', 'cancelled')),
  cols integer not null default 5 check (cols = 5),
  rows integer not null default 4 check (rows = 4),
  current_seat integer not null default 1 check (current_seat in (1, 2)),
  score_p1 integer not null default 0 check (score_p1 >= 0),
  score_p2 integer not null default 0 check (score_p2 >= 0),
  winner integer check (winner in (0, 1, 2)),
  move_count integer not null default 0 check (move_count >= 0),
  player_count integer not null default 0 check (player_count >= 0),
  paired_count integer not null default 0 check (paired_count >= 0),
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);

create table public.room_players (
  id uuid primary key default gen_random_uuid(),
  room_id uuid not null references public.rooms (id) on delete cascade,
  user_id uuid not null,
  seat integer not null check (seat in (1, 2)),
  pairing_code text check (pairing_code ~ '^[ABC]{5}$'),
  device_id uuid,
  paired_at timestamptz,
  created_at timestamptz not null default now(),
  unique (room_id, seat),
  unique (room_id, user_id)
);

create unique index room_players_live_code
  on public.room_players (pairing_code)
  where pairing_code is not null;

create index room_players_user on public.room_players (user_id);

create table public.moves (
  id uuid primary key,
  room_id uuid not null references public.rooms (id) on delete cascade,
  seat integer not null check (seat in (1, 2)),
  orientation text not null check (orientation in ('h', 'v')),
  row integer not null check (row >= 0),
  col integer not null check (col >= 0),
  move_index integer not null check (move_index > 0),
  created_at timestamptz not null default now(),
  unique (room_id, move_index),
  unique (room_id, orientation, row, col)
);

create index moves_room on public.moves (room_id, move_index);

alter table public.rooms replica identity full;
alter table public.room_players replica identity full;
alter table public.moves replica identity full;

create or replace function public.make_pairing_code()
returns text
language plpgsql
volatile
set search_path = public
as $$
declare
  code text := '';
  index integer;
begin
  for index in 1..5 loop
    code := code || substr('ABC', 1 + (abs(hashtext(gen_random_uuid()::text || index::text)) % 3), 1);
  end loop;
  return code;
end;
$$;

create or replace function public.reserve_pairing_code()
returns text
language plpgsql
volatile
set search_path = public
as $$
declare
  code text;
  attempt integer;
begin
  for attempt in 1..12 loop
    code := public.make_pairing_code();
    if not exists (select 1 from public.room_players where pairing_code = code) then
      return code;
    end if;
  end loop;
  raise exception 'could not reserve a pairing code';
end;
$$;

create or replace function public.recompute_room(target uuid)
returns void
language plpgsql
security definer
set search_path = public
as $$
declare
  board_cols integer;
  board_rows integer;
  horizontal integer[];
  vertical integer[];
  boxes integer[];
  rec record;
  seat_to_play integer := 1;
  scores integer[] := array[0, 0];
  completed integer;
  total integer;
  played integer := 0;
  box_row integer;
  box_col integer;
  box_index integer;
  top_edge integer;
  bottom_edge integer;
  left_edge integer;
  right_edge integer;
begin
  select cols, rows into board_cols, board_rows
  from public.rooms
  where id = target
  for update;
  if board_cols is null then
    raise exception 'room missing';
  end if;

  horizontal := array_fill(0, array[(board_rows + 1) * board_cols]);
  vertical := array_fill(0, array[board_rows * (board_cols + 1)]);
  boxes := array_fill(0, array[board_rows * board_cols]);

  for rec in
    select seat, orientation, row, col
    from public.moves
    where room_id = target
    order by move_index
  loop
    if rec.seat <> seat_to_play then
      raise exception 'move out of turn';
    end if;
    if rec.orientation = 'h' then
      if rec.row < 0 or rec.row > board_rows or rec.col < 0 or rec.col >= board_cols then
        raise exception 'edge outside the board';
      end if;
      top_edge := rec.row * board_cols + rec.col;
      if horizontal[top_edge + 1] <> 0 then
        raise exception 'edge taken';
      end if;
      horizontal[top_edge + 1] := rec.seat;
    elsif rec.orientation = 'v' then
      if rec.row < 0 or rec.row >= board_rows or rec.col < 0 or rec.col > board_cols then
        raise exception 'edge outside the board';
      end if;
      left_edge := rec.row * (board_cols + 1) + rec.col;
      if vertical[left_edge + 1] <> 0 then
        raise exception 'edge taken';
      end if;
      vertical[left_edge + 1] := rec.seat;
    else
      raise exception 'bad orientation';
    end if;

    played := played + 1;
    completed := 0;

    -- Two candidate boxes per edge, matching the badge rules exactly.
    if rec.orientation = 'h' and rec.row > 0 then
      box_row := rec.row - 1;
      box_col := rec.col;
      box_index := box_row * board_cols + box_col;
      top_edge := box_row * board_cols + box_col;
      bottom_edge := (box_row + 1) * board_cols + box_col;
      left_edge := box_row * (board_cols + 1) + box_col;
      right_edge := left_edge + 1;
      if boxes[box_index + 1] = 0
         and horizontal[top_edge + 1] <> 0
         and horizontal[bottom_edge + 1] <> 0
         and vertical[left_edge + 1] <> 0
         and vertical[right_edge + 1] <> 0 then
        boxes[box_index + 1] := rec.seat;
        completed := completed + 1;
      end if;
    end if;
    if rec.orientation = 'h' and rec.row < board_rows then
      box_row := rec.row;
      box_col := rec.col;
      box_index := box_row * board_cols + box_col;
      top_edge := box_row * board_cols + box_col;
      bottom_edge := (box_row + 1) * board_cols + box_col;
      left_edge := box_row * (board_cols + 1) + box_col;
      right_edge := left_edge + 1;
      if boxes[box_index + 1] = 0
         and horizontal[top_edge + 1] <> 0
         and horizontal[bottom_edge + 1] <> 0
         and vertical[left_edge + 1] <> 0
         and vertical[right_edge + 1] <> 0 then
        boxes[box_index + 1] := rec.seat;
        completed := completed + 1;
      end if;
    end if;
    if rec.orientation = 'v' and rec.col > 0 then
      box_row := rec.row;
      box_col := rec.col - 1;
      box_index := box_row * board_cols + box_col;
      top_edge := box_row * board_cols + box_col;
      bottom_edge := (box_row + 1) * board_cols + box_col;
      left_edge := box_row * (board_cols + 1) + box_col;
      right_edge := left_edge + 1;
      if boxes[box_index + 1] = 0
         and horizontal[top_edge + 1] <> 0
         and horizontal[bottom_edge + 1] <> 0
         and vertical[left_edge + 1] <> 0
         and vertical[right_edge + 1] <> 0 then
        boxes[box_index + 1] := rec.seat;
        completed := completed + 1;
      end if;
    end if;
    if rec.orientation = 'v' and rec.col < board_cols then
      box_row := rec.row;
      box_col := rec.col;
      box_index := box_row * board_cols + box_col;
      top_edge := box_row * board_cols + box_col;
      bottom_edge := (box_row + 1) * board_cols + box_col;
      left_edge := box_row * (board_cols + 1) + box_col;
      right_edge := left_edge + 1;
      if boxes[box_index + 1] = 0
         and horizontal[top_edge + 1] <> 0
         and horizontal[bottom_edge + 1] <> 0
         and vertical[left_edge + 1] <> 0
         and vertical[right_edge + 1] <> 0 then
        boxes[box_index + 1] := rec.seat;
        completed := completed + 1;
      end if;
    end if;

    if completed > 0 then
      scores[rec.seat] := scores[rec.seat] + completed;
    else
      seat_to_play := 3 - seat_to_play;
    end if;
  end loop;

  total := (board_rows + 1) * board_cols + board_rows * (board_cols + 1);
  update public.rooms
  set current_seat = seat_to_play,
      score_p1 = scores[1],
      score_p2 = scores[2],
      move_count = played,
      winner = case
        when played = total and scores[1] = scores[2] then 0
        when played = total and scores[1] > scores[2] then 1
        when played = total then 2
        else null
      end,
      status = case when played = total then 'finished' else status end,
      finished_at = case when played = total then coalesce(finished_at, now()) else finished_at end
  where id = target;

  if played = total then
    update public.room_players
    set pairing_code = null
    where room_id = target and pairing_code is not null;
  end if;
end;
$$;

create or replace function public.moves_recompute()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  perform public.recompute_room(new.room_id);
  return null;
end;
$$;

create trigger moves_recompute
  after insert on public.moves
  for each row execute function public.moves_recompute();

create or replace function public.refresh_room_roster()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
declare
  target uuid := coalesce(new.room_id, old.room_id);
begin
  update public.rooms
  set player_count = (select count(*) from public.room_players where room_id = target),
      paired_count = (select count(*) from public.room_players where room_id = target and paired_at is not null)
  where id = target;

  update public.rooms
  set status = 'playing',
      started_at = coalesce(started_at, now())
  where id = target
    and status = 'waiting'
    and player_count = 2
    and paired_count = 2;
  return null;
end;
$$;

create trigger room_players_roster
  after insert or update or delete on public.room_players
  for each row execute function public.refresh_room_roster();

create or replace function public.assert_no_live_membership()
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
  if auth.uid() is null then
    raise exception 'sign in required';
  end if;
  if exists (
    select 1
    from public.room_players player
    join public.rooms room on room.id = player.room_id
    where player.user_id = auth.uid()
      and room.status in ('waiting', 'playing')
  ) then
    raise exception 'finish or cancel your current room first';
  end if;
end;
$$;

create or replace function public.create_room()
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  new_room uuid;
  new_player uuid;
  code text;
begin
  if auth.uid() is null then
    raise exception 'sign in required';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(auth.uid()::text, 0));
  perform public.assert_no_live_membership();
  code := public.reserve_pairing_code();
  insert into public.rooms (host_user_id)
  values (auth.uid())
  returning id into new_room;
  insert into public.room_players (room_id, user_id, seat, pairing_code)
  values (new_room, auth.uid(), 1, code)
  returning id into new_player;
  return jsonb_build_object(
    'room_id', new_room,
    'player_id', new_player,
    'seat', 1,
    'pairing_code', code
  );
end;
$$;

create or replace function public.join_room(target uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  host uuid;
  room_status text;
  new_player uuid;
  code text;
begin
  if auth.uid() is null then
    raise exception 'sign in required';
  end if;
  perform pg_advisory_xact_lock(hashtextextended(auth.uid()::text, 0));
  perform public.assert_no_live_membership();
  perform pg_advisory_xact_lock(hashtextextended(target::text, 0));
  select host_user_id, status into host, room_status
  from public.rooms
  where id = target
  for update;
  if host is null or room_status <> 'waiting' then
    raise exception 'that room is not open';
  end if;
  if host = auth.uid() then
    raise exception 'you already created this room';
  end if;
  if exists (select 1 from public.room_players where room_id = target and seat = 2) then
    raise exception 'that room already has two players';
  end if;
  code := public.reserve_pairing_code();
  insert into public.room_players (room_id, user_id, seat, pairing_code)
  values (target, auth.uid(), 2, code)
  returning id into new_player;
  return jsonb_build_object(
    'room_id', target,
    'player_id', new_player,
    'seat', 2,
    'pairing_code', code
  );
end;
$$;

create or replace function public.cancel_room(target uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
begin
  if auth.uid() is null then
    raise exception 'sign in required';
  end if;
  update public.rooms
  set status = 'cancelled',
      finished_at = coalesce(finished_at, now())
  where id = target
    and host_user_id = auth.uid()
    and status = 'waiting';
  if not found then
    raise exception 'only the host can cancel a room that has not started';
  end if;
  update public.room_players
  set pairing_code = null
  where room_id = target and pairing_code is not null;
  return jsonb_build_object('room_id', target, 'status', 'cancelled');
end;
$$;

create or replace function public.leave_room(target uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  removed integer;
begin
  if auth.uid() is null then
    raise exception 'sign in required';
  end if;
  delete from public.room_players player
  using public.rooms room
  where player.room_id = target
    and room.id = player.room_id
    and player.user_id = auth.uid()
    and player.seat = 2
    and room.status = 'waiting';
  get diagnostics removed = row_count;
  if removed <> 1 then
    raise exception 'you can leave only as the guest, before the game starts';
  end if;
  return jsonb_build_object('room_id', target, 'left', true);
end;
$$;

create or replace function public.submit_move(
  move_id uuid,
  target uuid,
  orientation text,
  row_i integer,
  col_i integer
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  room_status text;
  expected_seat integer;
  actor_seat integer;
  next_index integer;
begin
  if auth.uid() is null then
    raise exception 'sign in required';
  end if;
  if exists (select 1 from public.moves where id = move_id and room_id = target) then
    return jsonb_build_object('ok', true, 'duplicate', true);
  end if;
  select status, rooms.current_seat into room_status, expected_seat
  from public.rooms
  where id = target
  for update;
  if room_status is null or room_status <> 'playing' then
    raise exception 'the game has not started';
  end if;
  select seat into actor_seat
  from public.room_players
  where room_id = target and user_id = auth.uid();
  if actor_seat is null or actor_seat <> expected_seat then
    raise exception 'not your turn';
  end if;
  if orientation not in ('h', 'v') then
    raise exception 'bad orientation';
  end if;
  select coalesce(max(move_index), 0) + 1 into next_index
  from public.moves
  where room_id = target;
  insert into public.moves (id, room_id, seat, orientation, row, col, move_index)
  values (move_id, target, actor_seat, orientation, row_i, col_i, next_index);
  return jsonb_build_object('ok', true, 'move_index', next_index);
end;
$$;

revoke all on function public.make_pairing_code() from public, anon, authenticated;
revoke all on function public.reserve_pairing_code() from public, anon, authenticated;
revoke all on function public.recompute_room(uuid) from public, anon, authenticated;
revoke all on function public.moves_recompute() from public, anon, authenticated;
revoke all on function public.refresh_room_roster() from public, anon, authenticated;
revoke all on function public.assert_no_live_membership() from public, anon, authenticated;

revoke all on function public.create_room() from public, anon;
revoke all on function public.join_room(uuid) from public, anon;
revoke all on function public.cancel_room(uuid) from public, anon;
revoke all on function public.leave_room(uuid) from public, anon;
revoke all on function public.submit_move(uuid, uuid, text, integer, integer) from public, anon;

grant all on public.rooms, public.room_players, public.moves to service_role;
grant execute on function public.create_room() to service_role;
grant execute on function public.join_room(uuid) to service_role;
grant execute on function public.cancel_room(uuid) to service_role;
grant execute on function public.leave_room(uuid) to service_role;
grant execute on function public.submit_move(uuid, uuid, text, integer, integer) to service_role;

grant execute on function public.create_room() to authenticated;
grant execute on function public.join_room(uuid) to authenticated;
grant execute on function public.cancel_room(uuid) to authenticated;
grant execute on function public.leave_room(uuid) to authenticated;
grant execute on function public.submit_move(uuid, uuid, text, integer, integer) to authenticated;

alter table public.rooms enable row level security;
alter table public.room_players enable row level security;
alter table public.moves enable row level security;

revoke all on public.rooms, public.room_players, public.moves from anon, public, authenticated;

grant select on public.rooms to authenticated;
grant select (
  id, room_id, user_id, seat, paired_at, created_at
) on public.room_players to authenticated;
grant select on public.moves to authenticated;

create policy rooms_read on public.rooms
  for select to authenticated
  using (status <> 'cancelled');

create policy room_players_read on public.room_players
  for select to authenticated
  using (
    exists (
      select 1 from public.rooms
      where rooms.id = room_players.room_id
        and rooms.status <> 'cancelled'
    )
  );

create policy moves_read on public.moves
  for select to authenticated
  using (
    exists (
      select 1 from public.rooms
      where rooms.id = moves.room_id
        and rooms.status <> 'cancelled'
    )
  );

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'powersync_dbbb9f370607ca2fe2a8629b') then
    raise exception 'PowerSync replication role powersync_dbbb9f370607ca2fe2a8629b is missing';
  end if;
  grant select on public.rooms, public.room_players, public.moves
    to powersync_dbbb9f370607ca2fe2a8629b;

  if not exists (select 1 from pg_publication where pubname = 'powersync') then
    create publication powersync;
  end if;
  if (select puballtables from pg_publication where pubname = 'powersync') then
    raise exception 'powersync publication must list explicit tables';
  end if;
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'powersync' and schemaname = 'public' and tablename = 'rooms'
  ) then
    alter publication powersync add table public.rooms;
  end if;
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'powersync' and schemaname = 'public' and tablename = 'room_players'
  ) then
    alter publication powersync add table public.room_players;
  end if;
  if not exists (
    select 1 from pg_publication_tables
    where pubname = 'powersync' and schemaname = 'public' and tablename = 'moves'
  ) then
    alter publication powersync add table public.moves;
  end if;
end $$;
