-- Rules check against the linked database. Every change rolls back.
-- The board is fixed at 5x4. This plays the top-left box: h0,0 v0,0 h1,0 v0,1.
begin;

do $$
declare
  user_a uuid := '11111111-1111-4111-8111-111111111111';
  user_b uuid := '22222222-2222-4222-8222-222222222222';
  user_c uuid := '33333333-3333-4333-8333-333333333333';
  created jsonb;
  joined jsonb;
  target_room uuid;
  other_room uuid;
  replaced jsonb;
  move jsonb;
  edge record;
  next_user uuid;
begin
  perform set_config('request.jwt.claim.sub', user_a::text, true);
  perform set_config('request.jwt.claim.role', 'authenticated', true);
  perform set_config('request.jwt.claims', json_build_object('sub', user_a, 'role', 'authenticated')::text, true);

  created := public.create_room();
  target_room := (created->>'room_id')::uuid;
  if created->>'seat' <> '1' or created->>'pairing_code' !~ '^[ABC]{5}$' then
    raise exception 'create_room did not return a seat and code: %', created;
  end if;

  begin
    perform public.create_room();
    raise exception 'second room was allowed';
  exception
    when others then
      if sqlerrm <> 'finish or cancel your current room first' then
        raise;
      end if;
  end;

  perform set_config('request.jwt.claim.sub', user_b::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', user_b, 'role', 'authenticated')::text, true);
  joined := public.join_room(target_room);
  if joined->>'seat' <> '2' or joined->>'pairing_code' = created->>'pairing_code' then
    raise exception 'join did not issue a distinct code: %', joined;
  end if;

  begin
    perform public.join_room(target_room);
    raise exception 'second membership was allowed';
  exception
    when others then
      if sqlerrm <> 'finish or cancel your current room first' then
        raise;
      end if;
  end;

  if (select status from public.rooms where id = target_room) <> 'waiting' then
    raise exception 'game started before both badges paired';
  end if;

  update public.room_players
  set device_id = gen_random_uuid(), paired_at = now()
  where room_id = target_room;

  if (select status from public.rooms where id = target_room) <> 'playing' then
    raise exception 'game did not start when both badges paired';
  end if;

  perform set_config('request.jwt.claim.sub', user_a::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', user_a, 'role', 'authenticated')::text, true);
  move := public.submit_move('44444444-4444-4444-8444-444444444444', target_room, 'h', 0, 0);
  if move->>'ok' <> 'true' then
    raise exception 'first move failed: %', move;
  end if;

  perform set_config('request.jwt.claim.sub', user_b::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', user_b, 'role', 'authenticated')::text, true);
  perform public.submit_move('55555555-5555-4555-8555-555555555555', target_room, 'v', 0, 0);

  perform set_config('request.jwt.claim.sub', user_a::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', user_a, 'role', 'authenticated')::text, true);
  perform public.submit_move('66666666-6666-4666-8666-666666666666', target_room, 'h', 1, 0);

  perform set_config('request.jwt.claim.sub', user_b::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', user_b, 'role', 'authenticated')::text, true);
  perform public.submit_move('77777777-7777-4777-8777-777777777777', target_room, 'v', 0, 1);

  if (select score_p1 from public.rooms where id = target_room) <> 0
     or (select score_p2 from public.rooms where id = target_room) <> 1
     or (select current_seat from public.rooms where id = target_room) <> 2
     or (select move_count from public.rooms where id = target_room) <> 4
     or (select winner from public.rooms where id = target_room) is not null
     or (select status from public.rooms where id = target_room) <> 'playing' then
    raise exception 'top-left box was not awarded to player 2';
  end if;

  move := public.submit_move('77777777-7777-4777-8777-777777777777', target_room, 'v', 0, 1);
  if move->>'duplicate' <> 'true' or (select move_count from public.rooms where id = target_room) <> 4 then
    raise exception 'duplicate move was applied again: %', move;
  end if;

  perform set_config('request.jwt.claim.sub', user_a::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', user_a, 'role', 'authenticated')::text, true);
  begin
    perform public.submit_move('88888888-8888-4888-8888-888888888888', target_room, 'h', 0, 1);
    raise exception 'player 1 moved during player 2 turn';
  exception
    when others then
      if sqlerrm <> 'not your turn' then
        raise;
      end if;
  end;

  -- Finish all 49 edges using each authoritative turn. This exercises the
  -- second half of a game, scoring turns, completion, and freeing both seats.
  for edge in
    select 'h'::text as orientation, r as row_i, c as col_i
    from generate_series(0, 4) r cross join generate_series(0, 4) c
    union all
    select 'v'::text, r, c
    from generate_series(0, 3) r cross join generate_series(0, 5) c
  loop
    if exists (select 1 from public.moves m where m.room_id = target_room
               and m.orientation = edge.orientation and m.row = edge.row_i and m.col = edge.col_i) then
      continue;
    end if;
    select case when current_seat = 1 then user_a else user_b end into next_user
    from public.rooms where id = target_room;
    perform set_config('request.jwt.claim.sub', next_user::text, true);
    perform set_config('request.jwt.claims', json_build_object('sub', next_user, 'role', 'authenticated')::text, true);
    perform public.submit_move(gen_random_uuid(), target_room, edge.orientation, edge.row_i, edge.col_i);
  end loop;

  if not exists (select 1 from public.rooms where id = target_room and status = 'finished'
                 and move_count = 49 and score_p1 + score_p2 = 20 and winner is not null
                 and finished_at is not null) then
    raise exception 'full game did not finish with 49 edges and 20 scored boxes';
  end if;
  if exists (select 1 from public.room_players where room_id = target_room and pairing_code is not null) then
    raise exception 'finished game retained pairing codes';
  end if;
  replaced := public.create_room();
  if replaced->>'room_id' is null then
    raise exception 'finishing did not free the player to create another room';
  end if;

  perform set_config('request.jwt.claim.sub', user_c::text, true);
  perform set_config('request.jwt.claims', json_build_object('sub', user_c, 'role', 'authenticated')::text, true);
  other_room := (public.create_room()->>'room_id')::uuid;
  begin
    perform public.create_room();
    raise exception 'a second waiting room was allowed';
  exception
    when others then
      if sqlerrm <> 'finish or cancel your current room first' then
        raise;
      end if;
  end;
  perform public.cancel_room(other_room);
  if (select pairing_code from public.room_players where room_id = other_room) is not null then
    raise exception 'cancel left a pairing code behind';
  end if;
  replaced := public.create_room();
  if replaced->>'room_id' is null then
    raise exception 'cancel did not free the host to create another room';
  end if;
end $$;

rollback;
