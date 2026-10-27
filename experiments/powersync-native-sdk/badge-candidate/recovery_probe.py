"""Run the installed app with one injected RPC failure for its saved move.

Loaded in RAM by device.py. The app exposes a qualification hook only when this
USB-only object is in sys.modules. Credentials remain on the badge.
"""
import badgeware
import time
import network

boot = open("/system/main.py").read()
marker = boot.find("# Eat the wakeup button")
assert marker > 0
exec(boot[:marker])
print("MANAGED_NETWORK_READY", network.WLAN(network.STA_IF).isconnected())


class RecoveryDriver:
    def __init__(self):
        self.done = False
        self.injected = False
        self.attempts = []
        self.started = time.ticks_ms()
        self.next_report = 0

    def wrap_update(self, update, client):
        assert client is not None and client.sync is not None, "App startup failed"
        record = client.records.record
        assert record["pending_move"] is not None, "No pending move to recover"
        self.move_id = record["pending_move"]["id"]
        self.room_id = record["room_id"]
        if RECOVERY_EXPECTED_MOVE:
            assert self.move_id == RECOVERY_EXPECTED_MOVE
        if RECOVERY_EXPECTED_ROOM:
            assert self.room_id == RECOVERY_EXPECTED_ROOM
        print("RECOVERY_PENDING", self.room_id, self.move_id)
        real_factory = client.sync.connector.json_factory
        driver = self

        class DiagnosticGateway:
            def __init__(self, *args, **kwargs):
                self.real = real_factory(*args, **kwargs)
                self.fail = False

            def start(self, body):
                if "move_id" in body:
                    driver.attempts.append(body["move_id"])
                    self.fail = not driver.injected
                    if self.fail:
                        driver.injected = True
                    print("RECOVERY_RPC_ATTEMPT", len(driver.attempts), body["move_id"])
                self.real.start(body)

            def step(self):
                if self.fail:
                    self.fail = False
                    raise OSError("Injected one-shot Wi-Fi failure")
                return self.real.step()

            def close(self):
                self.real.close()

        client.sync.connector.json_factory = DiagnosticGateway

        def frame():
            install_watchdog.feed()
            result = update()
            link = client.sync
            pending = client.records.record["pending_move"]
            assert pending is None or pending["id"] == driver.move_id, "Pending UUID changed"
            runtime = time.ticks_diff(time.ticks_ms(), driver.started)
            if runtime >= driver.next_report:
                print("RECOVERY_PROGRESS", runtime, link.phase, link.status,
                      link.connected, link.synced, link.full_generation,
                      pending["id"] if pending else None, link.last_error)
                driver.next_report = runtime + 2000
            if pending is None and link.synced:
                assert client.store.has_move(driver.move_id, driver.room_id), "No exact publication"
                assert driver.injected and len(driver.attempts) >= 2, "SDK retry not observed"
                assert all(m == driver.move_id for m in driver.attempts), "RPC UUID changed"
                print("RECOVERY_EXACT_MOVE_CONFIRMED", driver.move_id, link.full_generation)
                print("RECOVERY_RPC_ATTEMPTS", driver.attempts)
                driver.done = True
                return True
            if link.phase in ("blocked", "revoked"):
                print("RECOVERY_STOPPED", link.phase, link.last_error)
                return True
            if runtime > 120000:
                print("RECOVERY_TIMEOUT")
                return True
            return result
        return frame


recovery_driver = RecoveryDriver()
sys.modules["_badge_native_recovery"] = recovery_driver
try:
    badgeware.launch(RECOVERY_TARGET)
finally:
    del sys.modules["_badge_native_recovery"]
    install_watchdog.feed()
print("RECOVERY_RESULT", recovery_driver.done)
