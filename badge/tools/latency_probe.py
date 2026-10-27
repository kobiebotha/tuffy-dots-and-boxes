"""Development-only, bounded RAM probe for the four end-to-end move latencies.

Never imported or packaged by the game. attach() wraps one existing client and
view; the launcher calls panel_submitted() after the real display.update().
Timestamps use one monotonic session clock. No tokens, row bodies, logging or
file writes are captured. Exact move IDs are retained privately for correlation.
"""
import time


def _uuid(value):
    return (isinstance(value, str) and len(value) == 36
            and [len(part) for part in value.split("-")] == [8, 4, 4, 4, 12]
            and all(c in "0123456789abcdef" for c in value.lower().replace("-", "")))


class Probe:
    def __init__(self, capacity=24, clock=None, diff=None):
        if type(capacity) is not int or not 1 <= capacity <= 64:
            raise ValueError("Probe capacity must be 1..64 per direction")
        if clock is None:
            if hasattr(time, "ticks_us"):
                clock, diff = time.ticks_us, time.ticks_diff
            else:
                clock = lambda: time.perf_counter_ns() // 1000
        self.clock = clock
        self.diff = diff or (lambda a, b: a - b)
        self.last = clock()
        self.elapsed = 0
        self.capacity = capacity
        # Allocate record dictionaries before any measurement starts.
        self.local = [{} for _ in range(capacity)]
        self.downloads = [{} for _ in range(capacity)]
        self.local_count = self.download_count = self.dropped = 0
        self.active = None
        self.frame_stages = ()

    def now(self):
        raw = self.clock()
        delta = self.diff(raw, self.last)
        if delta < 0:
            raise ValueError("Ambiguous or backwards probe clock")
        self.elapsed += delta
        self.last = raw
        return self.elapsed

    def begin_local(self):
        if self.active is not None:
            return False
        if self.local_count == self.capacity:
            self.dropped += 1
            return False
        row = self.local[self.local_count]
        row.update(sample_id=self.local_count, outcome="incomplete", _kind="local")
        self.active = row
        row["queued_us"] = self.now()
        return True

    def discard_local(self):
        self.active.clear()
        self.active = None

    def arm_download(self, client, sample_id, move_id, edge, move_index):
        if self.active is not None:
            raise ValueError("Previous sample is still active")
        if (type(sample_id) is not int or sample_id < 0 or not _uuid(move_id)
                or not isinstance(edge, (tuple, list)) or len(edge) != 3
                or type(move_index) is not int):
            raise ValueError("Invalid download sample")
        if self.download_count == self.capacity:
            self.dropped += 1
            raise ValueError("Download capacity reached")
        if any(row.get("sample_id") == sample_id for row in self.downloads[:self.download_count]):
            raise ValueError("Duplicate download sample_id")
        record = client.records.record
        game = client.controller.game
        if (client.sync is None or not client.sync.synced or client.sync.phase != "stream"
                or record["pending_move"] is not None or record["room_id"] is None
                or move_index != game.moves + 1 or game.edge_owner(*edge)
                or client.store.has_move(move_id, record["room_id"])):
            raise ValueError("Arm a new move on a fully synced idle board")
        row = self.downloads[self.download_count]
        row.update(sample_id=sample_id, armed_us=self.now(), outcome="incomplete",
                   _kind="download", _move_id=move_id, _room_id=record["room_id"],
                   _edge=tuple(edge), _move_index=move_index)
        self.download_count += 1
        self.active = row

    def committed(self, client, results, stamp):
        row = self.active
        if row is None or "sqlite_persisted_us" in row or "_move_id" not in row:
            return
        published = any(any(
            "DidCompleteSync" in instruction for instruction in result.get("instructions", ()))
            for result in results)
        # The timestamp is taken BEFORE this extra correlation query. Only a
        # publication containing this exact UUID/room can mark the sample.
        # Partial checkpoints can publish rows too. The app separately requires
        # a full checkpoint before clearing a locally pending move.
        if published and client.store.has_move(row["_move_id"], row["_room_id"]):
            row["sqlite_persisted_us"] = stamp

    def drawn(self, client):
        row = self.active
        if row is None or "_edge" not in row:
            return
        controller = client.controller
        edge = row["_edge"]
        stages = []
        if row["_kind"] == "local":
            if (controller.pending_edge == edge and controller.pending_player
                    and "framebuffer_us" not in row):
                stages.append("framebuffer_us")
            if ("sqlite_persisted_us" in row and client.records.record["pending_move"] is None
                    and controller.game.edge_owner(*edge)
                    and "confirmed_framebuffer_us" not in row):
                stages.append("confirmed_framebuffer_us")
        elif ("sqlite_persisted_us" in row and controller.game.moves >= row["_move_index"]
              and controller.game.edge_owner(*edge) and "framebuffer_us" not in row):
            stages.append("framebuffer_us")
        if stages:
            stamp = self.now()
            for stage in stages:
                row[stage] = stamp
            self.frame_stages = tuple(stages)

    def panel_submitted(self):
        row = self.active
        if row is None:
            self.frame_stages = ()
            return
        if self.frame_stages:
            stamp = self.now()
            for stage in self.frame_stages:
                row[stage.replace("framebuffer", "panel_submitted")] = stamp
            self.frame_stages = ()
        if row["_kind"] == "download" and "panel_submitted_us" in row:
            row["outcome"] = "accepted"
            self.active = None
        elif row["_kind"] == "local" and (
                row["outcome"] == "rejected" or
                (row["outcome"] == "accepted" and "confirmed_panel_submitted_us" in row)):
            self.active = None

    def export(self):
        def public(row):
            return {key: value for key, value in row.items() if not key.startswith("_")}
        return {"format_version": 1, "local": [public(row) for row in self.local[:self.local_count]],
                "downloads": [public(row) for row in self.downloads[:self.download_count]],
                "dropped": self.dropped, "active": self.active is not None,
                "durable_local_medium": "pending.json"}


def attach(client, view, probe=None):
    """Return (probe, detach); leave normal app files and global classes intact."""
    if client.sync is None or client.records.record["pending_move"] is not None:
        raise ValueError("Attach to a paired client with no existing pending move")
    probe = probe or Probe()
    restorations = []

    def wrap(owner, name, factory):
        original = getattr(owner, name)
        owned = name in getattr(owner, "__dict__", {})
        restorations.append((owner, name, original, owned))
        setattr(owner, name, factory(original))

    def queue_wrapper(original):
        def queue(edge):
            # Do not mix a physical local move into an armed remote trial.
            if probe.active is not None:
                return False
            started = probe.begin_local()
            try:
                result = original(edge)
            except Exception:
                if started:
                    probe.active["outcome"] = "failed"
                    probe.local_count += 1
                    probe.active = None
                raise
            if started:
                if result:
                    probe.local_count += 1
                else:
                    probe.discard_local()
            return result
        return queue

    def save_wrapper(original):
        def save(record):
            previous = client.records.record["pending_move"]
            result = original(record)
            row = probe.active
            move = record["pending_move"]
            if row is not None and row["_kind"] == "local" and previous is None and move is not None:
                row["pending_saved_us"] = probe.now()
                row.update(_move_id=move["id"], _room_id=record["room_id"],
                           _edge=(move["orientation"], move["row"], move["col"]))
            return result
        return save

    def upload_wrapper(original):
        def uploaded(accepted=False):
            row = probe.active
            if row is not None and row["_kind"] == "local":
                if accepted and "rpc_done_us" not in row:
                    row["rpc_done_us"] = probe.now()
                row["outcome"] = "accepted" if accepted else "rejected"
            return original(accepted=accepted)
        return uploaded

    def control_wrapper(original):
        def control(*args, **kwargs):
            result = original(*args, **kwargs)
            if probe.active is not None:
                probe.committed(client, result, probe.now())
            return result
        return control

    def draw_wrapper(original):
        def draw(*args, **kwargs):
            result = original(*args, **kwargs)
            probe.drawn(client)
            return result
        return draw

    wrap(client, "_queue_move", queue_wrapper)
    old_claim = client.controller.on_claim
    client.controller.on_claim = client._queue_move
    wrap(client.records, "save", save_wrapper)
    wrap(client.sync, "_after_upload", upload_wrapper)
    wrap(client.store, "control_lines", control_wrapper)
    wrap(view, "draw", draw_wrapper)
    detached = [False]

    def detach():
        if detached[0]:
            return
        detached[0] = True
        client.controller.on_claim = old_claim
        for owner, name, original, owned in reversed(restorations):
            if owned:
                setattr(owner, name, original)
            else:
                delattr(owner, name)
        restorations.clear()
    return probe, detach
