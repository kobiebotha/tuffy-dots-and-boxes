-- Room archival checks against the linked database. Every change rolls back.
begin;

do $$
declare
  host_user uuid := '81111111-1111-4111-8111-111111111111';
  guest_user uuid := '82222222-2222-4222-8222-222222222222';
  outsider uuid := '83333333-3333-4333-8333-333333333333';
  target_room uuid;
  next_room uuid;
  response jsonb;
  old_codes text[];
  changed integer;
  terminal_at timestamptz;
  first_move uuid := gen_random_uuid();
  edge record;
  next_user uuid;
  final_room public.rooms%rowtype;
begin
  if has_function_privilege('anon', 'public.archive_room(uuid)', 'execute') then
    raise exception 'anonymous callers can archive rooms';
  end if;
  if not has_function_privilege('authenticated', 'public.archive_room(uuid)', 'execute') then
    raise exception 'signed-in callers cannot archive rooms';
  end if;

  perform set_config('request.jwt.claim.sub', '', true);
  perform set_config('request.jwt.claims', '{}', true);
  begin
    perform public.archive_room(gen_random_uuid());
    raise exception 'unsigned caller archived a room';
  exception when others then
    if sqlerrm <> 'sign in required' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', host_user::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', host_user, 'role', 'authenticated')::text, true);
  target_room := (public.create_room()->>'room_id')::uuid;

  perform set_config('request.jwt.claim.sub', guest_user::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', guest_user, 'role', 'authenticated')::text, true);
  perform public.join_room(target_room);
  begin
    perform public.archive_room(target_room);
    raise exception 'guest archived the host room';
  exception when others then
    if sqlerrm <> 'only the host can archive this room' then raise; end if;
  end;

  perform set_config('request.jwt.claim.sub', outsider::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', outsider, 'role', 'authenticated')::text, true);
  begin
    perform public.archive_room(target_room);
    raise exception 'unrelated user archived a room';
  exception when others then
    if sqlerrm <> 'only the host can archive this room' then raise; end if;
  end;

  -- One paired badge is still a waiting room. Archiving must clear both codes
  -- and the paired badge without deleting either player's history.
  update public.room_players set device_id = gen_random_uuid(), paired_at = now()
  where room_id = target_room and seat = 1;
  select array_agg(pairing_code) into old_codes from public.room_players where room_id = target_room;
  perform set_config('request.jwt.claim.sub', host_user::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', host_user, 'role', 'authenticated')::text, true);
  response := public.archive_room(target_room);
  if response->>'status' <> 'cancelled' or (response->>'room_id')::uuid <> target_room then
    raise exception 'archive did not return its terminal room: %', response;
  end if;
  if not exists (select 1 from public.rooms where id = target_room and status = 'cancelled'
                 and paired_count = 0 and player_count = 2 and finished_at is not null) then
    raise exception 'waiting archive did not keep the roster and release the badges';
  end if;
  if exists (select 1 from public.room_players where room_id = target_room
             and (pairing_code is not null or device_id is not null or paired_at is not null)) then
    raise exception 'archived waiting room retained pairing credentials';
  end if;
  -- This is the same conditional write used by badge-pair. A stale code may
  -- not restore a device binding after an archive has cleared the code.
  update public.room_players set device_id = gen_random_uuid(), paired_at = now()
  where room_id = target_room and pairing_code = any(old_codes) and paired_at is null;
  get diagnostics changed = row_count;
  if changed <> 0 then raise exception 'an archived pairing code was reusable'; end if;

  select finished_at into terminal_at from public.rooms where id = target_room;
  perform public.archive_room(target_room);
  if (select finished_at from public.rooms where id = target_room) is distinct from terminal_at then
    raise exception 'repeated archive changed the terminal timestamp';
  end if;
  begin
    perform public.archive_room(gen_random_uuid());
    raise exception 'a missing room was archived';
  exception when others then
    if sqlerrm <> 'only the host can archive this room' then raise; end if;
  end;

  -- Both former participants can immediately start fresh rooms.
  next_room := (public.create_room()->>'room_id')::uuid;
  perform public.archive_room(next_room);
  perform set_config('request.jwt.claim.sub', guest_user::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', guest_user, 'role', 'authenticated')::text, true);
  next_room := (public.create_room()->>'room_id')::uuid;
  perform public.archive_room(next_room);

  -- Archive an active game after a real move. Preserve its board and scores,
  -- reject further moves, and release the badges from both seats.
  perform set_config('request.jwt.claim.sub', host_user::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', host_user, 'role', 'authenticated')::text, true);
  target_room := (public.create_room()->>'room_id')::uuid;
  perform set_config('request.jwt.claim.sub', guest_user::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', guest_user, 'role', 'authenticated')::text, true);
  perform public.join_room(target_room);
  update public.room_players set device_id = gen_random_uuid(), paired_at = now() where room_id = target_room;
  perform set_config('request.jwt.claim.sub', host_user::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', host_user, 'role', 'authenticated')::text, true);
  perform public.submit_move(first_move, target_room, 'h', 0, 0);
  perform public.archive_room(target_room);
  if not exists (select 1 from public.rooms where id = target_room and status = 'cancelled'
                 and move_count = 1 and score_p1 = 0 and score_p2 = 0 and paired_count = 0)
     or not exists (select 1 from public.moves where id = first_move) then
    raise exception 'active archive lost board history or kept a connected badge';
  end if;
  if exists (select 1 from public.room_players where room_id = target_room
             and (pairing_code is not null or device_id is not null or paired_at is not null)) then
    raise exception 'archived active room retained pairing credentials';
  end if;
  begin
    perform public.submit_move(gen_random_uuid(), target_room, 'h', 0, 1);
    raise exception 'a new move entered an archived room';
  exception when others then
    if sqlerrm <> 'the game has not started' then raise; end if;
  end;
  perform set_config('request.jwt.claim.sub', outsider::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', outsider, 'role', 'authenticated')::text, true);
  begin
    perform public.join_room(target_room);
    raise exception 'an archived room allowed a new player';
  exception when others then
    if sqlerrm <> 'that room is not open' then raise; end if;
  end;

  -- Finish a complete game, then archive the result. Its finish timestamp,
  -- winner, scores, and all 49 moves remain available as stored history.
  perform set_config('request.jwt.claim.sub', host_user::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', host_user, 'role', 'authenticated')::text, true);
  target_room := (public.create_room()->>'room_id')::uuid;
  perform set_config('request.jwt.claim.sub', guest_user::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', guest_user, 'role', 'authenticated')::text, true);
  perform public.join_room(target_room);
  update public.room_players set device_id = gen_random_uuid(), paired_at = now() where room_id = target_room;
  for edge in
    select 'h'::text as orientation, r as row_i, c as col_i
    from generate_series(0, 4) r cross join generate_series(0, 4) c
    union all
    select 'v'::text, r, c
    from generate_series(0, 3) r cross join generate_series(0, 5) c
  loop
    select case when current_seat = 1 then host_user else guest_user end into next_user
    from public.rooms where id = target_room;
    perform set_config('request.jwt.claim.sub', next_user::text, true);
    perform set_config('request.jwt.claims', json_build_object('sub', next_user, 'role', 'authenticated')::text, true);
    perform public.submit_move(gen_random_uuid(), target_room, edge.orientation, edge.row_i, edge.col_i);
  end loop;
  select * into final_room from public.rooms where id = target_room;
  if final_room.status <> 'finished' then raise exception 'fixture did not finish'; end if;
  perform set_config('request.jwt.claim.sub', host_user::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', host_user, 'role', 'authenticated')::text, true);
  perform public.archive_room(target_room);
  if not exists (select 1 from public.rooms where id = target_room and status = 'cancelled'
                 and finished_at = final_room.finished_at and winner = final_room.winner
                 and score_p1 = final_room.score_p1 and score_p2 = final_room.score_p2
                 and move_count = 49 and paired_count = 0)
     or (select count(*) from public.moves where room_id = target_room) <> 49 then
    raise exception 'finished archive lost the result or moves';
  end if;
  if exists (select 1 from public.room_players where room_id = target_room
             and (pairing_code is not null or device_id is not null or paired_at is not null)) then
    raise exception 'archived finished room retained badge bindings';
  end if;
end $$;

rollback;
