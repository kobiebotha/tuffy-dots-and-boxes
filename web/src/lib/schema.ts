import { column, Schema, Table } from "@powersync/web";

export const AppSchema = new Schema({
  rooms: new Table({
    host_user_id: column.text,
    status: column.text,
    cols: column.integer,
    rows: column.integer,
    current_seat: column.integer,
    score_p1: column.integer,
    score_p2: column.integer,
    winner: column.integer,
    move_count: column.integer,
    player_count: column.integer,
    paired_count: column.integer,
    created_at: column.text,
    started_at: column.text,
    finished_at: column.text,
  }),
  room_players: new Table(
    {
      room_id: column.text,
      user_id: column.text,
      seat: column.integer,
      pairing_code: column.text,
      device_id: column.text,
      paired_at: column.text,
      created_at: column.text,
    },
    { indexes: { room: ["room_id"], user: ["user_id"] } },
  ),
  moves: new Table(
    {
      room_id: column.text,
      seat: column.integer,
      orientation: column.text,
      row: column.integer,
      col: column.integer,
      move_index: column.integer,
    },
    { indexes: { room: ["room_id", "move_index"] } },
  ),
});

export type Database = (typeof AppSchema)["types"];
export type RoomRow = Database["rooms"];
export type PlayerRow = Database["room_players"];
export type MoveRow = Database["moves"];
