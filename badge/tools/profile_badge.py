"""Optional RAM-only move probe. This file is not imported or packaged by the app.

Load into RAM, then call attach(client, view) before driving a test move. Call
trace.rows() only after the measurement window and detach() before another run.
No credentials, row bodies, file writes or serial output are captured. A normal
app run has no tracing hooks. Compare enabled/disabled runs externally as well as
the timer/buffer calibration; Python wrappers themselves have an observer cost.
"""
from array import array
import time

QUEUED = 0
SAVED = 1
RPC_DONE = 2
DOWNLOADED = 3
APPLY_BEGIN = 4
COMMITTED = 5
CLEARED = 6
RENDERED = 7
STAGES = ("queued", "pending_saved", "rpc_done", "download_received",
          "apply_begin", "committed", "pending_cleared", "rendered")


class Trace:
    def __init__(self, capacity=32, clock=None, diff=None):
        if type(capacity) is not int or not 1 <= capacity <= 64:
            raise ValueError("Trace capacity must be 1..64 moves")
        if clock is None:
            if hasattr(time, "ticks_us"):
                clock, diff = time.ticks_us, time.ticks_diff
            else:
                clock = lambda: (time.perf_counter_ns() // 1000) & 0xffffffff
                diff = lambda a, b: ((a - b + 0x80000000) & 0xffffffff) - 0x80000000
        self.clock = clock
        self.diff = diff or (lambda a, b: a - b)
        self.capacity = capacity
        self.stamps = array("I", [0] * (capacity * len(STAGES)))
        self.masks = bytearray(capacity)
        self.outcomes = bytearray(capacity)
        self.count = 0
        self.active = -1
        self.dropped = 0

    def begin(self):
        if self.active >= 0:
            return False
        if self.count == self.capacity:
            self.dropped += 1
            return False
        self.active = self.count
        self.mark(QUEUED)
        return True

    def mark(self, stage, latest=False):
        index = self.active
        bit = 1 << stage
        if index >= 0 and (latest or not self.masks[index] & bit):
            self.stamps[index * len(STAGES) + stage] = self.clock()
            self.masks[index] |= bit

    def finish(self, outcome=1):
        if self.active >= 0:
            self.outcomes[self.active] = outcome  # 1 accepted, 2 rejected, 3 failed
            self.count += 1
            self.active = -1

    def discard(self):
        if self.active >= 0:
            self.masks[self.active] = 0
            self.active = -1

    def rows(self):
        """Allocate a public timing report after the measured window only."""
        result = []
        for index in range(self.count):
            offset = index * len(STAGES)
            start = self.stamps[offset]
            row = {"outcome": self.outcomes[index]}
            for stage, name in enumerate(STAGES):
                if self.masks[index] & (1 << stage):
                    row[name + "_us"] = self.diff(self.stamps[offset + stage], start)
            result.append(row)
        return result


def attach(client, view=None, trace=None):
    """Wrap one already opened client, without changing files or global classes.

    Returns (trace, detach). Install before a move; existing pending moves are not
    profiled because their original button/save boundary is already in the past.
    """
    if client.sync is None or client.records.record["pending_move"] is not None:
        raise ValueError("Attach to a paired client with no saved pending move")
    trace = trace or Trace()
    restorations = []
    stream_seen = None
    stream_restoration = None

    def replace(owner, name, wrapper, remember=True):
        original = getattr(owner, name)
        had_own = name in getattr(owner, "__dict__", {})
        restoration = (owner, name, original, had_own)
        if remember:
            restorations.append(restoration)
        setattr(owner, name, wrapper(original))
        return restoration

    def restore(restoration):
        owner, name, original, had_own = restoration
        if had_own:
            setattr(owner, name, original)
        else:
            delattr(owner, name)

    def queue_wrapper(original):
        def queue(preview):
            started = trace.begin()
            try:
                result = original(preview)
            except Exception:
                if started:
                    trace.finish(3)
                raise
            if started:
                if result:
                    trace.mark(SAVED)
                else:
                    trace.discard()
            return result
        return queue

    replace(client, "_queue_move", queue_wrapper)
    old_claim = client.controller.on_claim
    client.controller.on_claim = client._queue_move

    def save_wrapper(original):
        def save(value):
            clears = (trace.active >= 0 and client.records.record["pending_move"] is not None
                      and value["pending_move"] is None)
            saves_pending = (trace.active >= 0 and client.records.record["pending_move"] is None
                             and value["pending_move"] is not None)
            result = original(value)
            if saves_pending:
                trace.mark(SAVED)
            if clears:
                trace.mark(CLEARED)
            return result
        return save
    replace(client.records, "save", save_wrapper)

    def after_wrapper(original):
        def after(accepted=False):
            trace.mark(RPC_DONE)
            if trace.active >= 0:
                trace.outcomes[trace.active] = 1 if accepted else 2
            return original(accepted=accepted)
        return after
    replace(client.sync, "_after_move", after_wrapper)

    def control_wrapper(original):
        def control(*args, **kwargs):
            measuring = (trace.active >= 0 and not trace.masks[trace.active] & (1 << CLEARED))
            if measuring:
                trace.mark(APPLY_BEGIN, latest=True)
            result = original(*args, **kwargs)
            if measuring:
                trace.mark(COMMITTED, latest=True)
            return result
        return control
    replace(client.store, "control_lines", control_wrapper)

    def stream_wrapper(original):
        def read():
            result = original()
            if isinstance(result, bytes) and result:
                trace.mark(DOWNLOADED)
            return result
        return read

    def step_wrapper(original):
        def step(*args, **kwargs):
            nonlocal stream_seen, stream_restoration
            stream = client.sync.stream
            if stream is not stream_seen:
                if stream_restoration is not None:
                    restore(stream_restoration)
                    stream_restoration = None
                stream_seen = stream
                if stream is not None and trace.count < trace.capacity:
                    stream_restoration = replace(stream, "step", stream_wrapper, remember=False)
            result = original(*args, **kwargs)
            if view is None and trace.active >= 0 and trace.masks[trace.active] & (1 << CLEARED):
                trace.finish(trace.outcomes[trace.active] or 1)
            if trace.count == trace.capacity and stream_restoration is not None:
                restore(stream_restoration)
                stream_restoration = None
            return result
        return step
    replace(client.sync, "step", step_wrapper)

    if view is not None:
        def draw_wrapper(original):
            def draw(*args, **kwargs):
                result = original(*args, **kwargs)
                if trace.active >= 0 and trace.masks[trace.active] & (1 << CLEARED):
                    trace.mark(RENDERED)
                    trace.finish(trace.outcomes[trace.active] or 1)
                return result
            return draw
        replace(view, "draw", draw_wrapper)

    def detach():
        nonlocal stream_seen, stream_restoration
        client.controller.on_claim = old_claim
        if stream_restoration is not None:
            restore(stream_restoration)
            stream_restoration = None
        stream_seen = None
        for restoration in reversed(restorations):
            restore(restoration)
        restorations.clear()
    return trace, detach


def calibrate(iterations=1000):
    """Measure eight timer/store milestones; excludes wrapper and export costs."""
    if type(iterations) is not int or not 1 <= iterations <= 10000:
        raise ValueError("Calibration iterations must be 1..10000")
    trace = Trace(capacity=1)
    started = trace.clock()
    for _ in range(iterations):
        trace.count = 0
        trace.masks[0] = 0
        trace.begin()
        for stage in range(1, len(STAGES)):
            trace.mark(stage)
        trace.finish()
    elapsed = trace.diff(trace.clock(), started)
    return {"iterations": iterations, "microseconds_per_eight_marks": elapsed / iterations,
            "buffer_bytes": len(trace.stamps) * 4 + len(trace.masks) + len(trace.outcomes)}
