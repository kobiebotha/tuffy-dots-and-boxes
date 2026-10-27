"""Qualify the packaged resolver without network, firmware, or shared helpers."""
from pathlib import Path
import struct
import sys
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from powersync import bounded_dns as dns
from powersync.http import HTTPS, PENDING


def wire(name):
    return b"".join(bytes((len(part),)) + part.encode() for part in name.split(".")) + b"\0"


def response(query, answers=None, flags=0x8180, transaction=None, question=None):
    if answers is None:
        answers = [(b"\xc0\x0c", 1, bytes((203, 0, 113, 9)))]
    head = struct.pack(">HHHHHH", query.transaction if transaction is None else transaction,
                       flags, 1, len(answers), 0, 0)
    body = query.packet[12:] if question is None else question
    for name, kind, data in answers:
        body += name + struct.pack(">HHIH", kind, 1, 60, len(data)) + data
    return head + body


class Clock:
    def __init__(self):
        self.now = 0

    def ticks_ms(self):
        return self.now % 8192

    def ticks_diff(self, left, right):
        return (left - right + 4096) % 8192 - 4096


class Socket:
    def __init__(self):
        self.sent = []
        self.received = []
        self.closed = 0
        self.blocking = None
        self.send_error = None
        self.short_send = False
        self.read_sizes = []

    def setblocking(self, value):
        self.blocking = value

    def sendto(self, packet, target):
        if self.send_error:
            raise self.send_error
        self.sent.append((packet, target))
        return len(packet) - int(self.short_send)

    def recvfrom(self, size):
        self.read_sizes.append(size)
        if not self.received:
            raise OSError(11)
        packet = self.received.pop(0)
        if isinstance(packet, Exception):
            raise packet
        return packet

    def close(self):
        self.closed += 1


class SocketModule:
    AF_INET, SOCK_DGRAM = 2, 2

    def __init__(self):
        self.instance = Socket()
        self.opened = []

    def socket(self, family, kind):
        self.opened.append((family, kind))
        return self.instance


class DNSTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.platform = Clock(), SocketModule()
        self.query = self.make_query()
        self.addCleanup(self.query.close)

    def make_query(self, **options):
        return dns.SteppedQuery("sync.example", "192.0.2.53", socket_module=self.platform,
                                time_module=self.clock, entropy=lambda n: b"\x12\x34", **options)

    def queue(self, packet, source=("192.0.2.53", 53)):
        self.platform.instance.received.append((packet, source))

    def test_query_wire_and_a_answer_close_the_nonblocking_socket(self):
        self.assertEqual(self.query.packet, b"\x12\x34\x01\x00\x00\x01\0\0\0\0\0\0" +
                         wire("sync.example") + b"\0\1\0\1")
        self.assertIs(self.query.step(), dns.PENDING)
        self.assertFalse(self.platform.instance.blocking)
        self.assertEqual(len(self.platform.instance.sent), 1)
        self.assertEqual(self.platform.instance.read_sizes, [])
        self.queue(response(self.query))
        self.assertEqual(self.query.step(), "203.0.113.9")
        self.assertEqual(self.platform.instance.read_sizes, [513])
        self.assertEqual(self.platform.instance.closed, 1)
        self.query.close()
        self.assertEqual(self.platform.instance.closed, 1)
        with self.assertRaises(OSError):
            self.query.step()

    def test_unrelated_source_id_question_type_class_and_opcode_are_ignored(self):
        self.query.step()
        valid = response(self.query)
        cases = [(valid, ("192.0.2.54", 53)), (valid, ("192.0.2.53", 54)),
                 (response(self.query, transaction=0), ("192.0.2.53", 53))]
        for question in (wire("other.example") + b"\0\1\0\1",
                         wire("sync.example") + b"\0\x1c\0\1",
                         wire("sync.example") + b"\0\1\0\3"):
            cases.append((response(self.query, question=question), ("192.0.2.53", 53)))
        cases.append((response(self.query, flags=0x8980), ("192.0.2.53", 53)))
        for packet, source in cases:
            with self.subTest(packet=packet, source=source):
                self.queue(packet, source)
                self.assertIs(self.query.step(), dns.PENDING)
                self.assertFalse(self.query.closed)
        self.queue(valid)
        self.assertEqual(self.query.step(), "203.0.113.9")

    def test_cname_chain_matches_owner_even_when_a_answer_comes_first(self):
        self.query.step()
        self.queue(response(self.query, [
            (wire("unrelated.example"), 1, b"\x01\x01\x01\x01"),
            (wire("final.example"), 1, b"\xcb\0\x71\x0a"),
            (b"\xc0\x0c", 5, wire("FINAL.example")),
        ]))
        self.assertEqual(self.query.step(), "203.0.113.10")

    def test_malformed_oversized_and_compression_cycle_packets_do_not_escape_or_grow(self):
        self.query.step()
        valid = response(self.query)
        packets = [valid[:size] for size in range(len(valid))]
        packets += [valid + bytes(513 - len(valid)),
                    valid[:12] + b"\xc0\x0c" + valid[14:],
                    response(self.query, [(b"\xc0\xff", 1, b"1234")])]
        for packet in packets:
            with self.subTest(length=len(packet)):
                self.queue(packet)
                self.assertIs(self.query.step(), dns.PENDING)
                self.assertFalse(self.query.closed)
        self.queue(valid)
        self.assertEqual(self.query.step(), "203.0.113.9")

    def test_wrong_owner_nxdomain_and_truncation_fail_as_retryable_io_errors(self):
        packets = [response(self.query, [(wire("other.example"), 1, b"1234")]),
                   response(self.query, flags=0x8183), response(self.query, flags=0x8380),
                   response(self.query, [(b"\xc0\x0c", 5, wire("sync.example"))])]
        for packet in packets:
            query = self.make_query()
            query.step()
            self.queue(packet)
            with self.assertRaises(OSError):
                query.step()
            self.assertTrue(query.closed)

    def test_one_receive_per_step_and_exact_retry_deadline_including_tick_wrap(self):
        self.clock.now = 8000
        self.query.step()
        self.clock.now += 2999
        self.assertIs(self.query.step(), dns.PENDING)
        self.assertEqual(len(self.platform.instance.read_sizes), 1)
        self.clock.now += 1
        self.assertIs(self.query.step(), dns.PENDING)
        self.assertEqual(len(self.platform.instance.sent), 2)
        self.assertEqual(len(self.platform.instance.read_sizes), 1)
        self.clock.now += 3000
        with self.assertRaisesRegex(OSError, "deadline"):
            self.query.step()
        self.assertEqual(self.platform.instance.closed, 1)

    def test_send_would_block_is_bounded_and_other_io_failures_close(self):
        for error in (OSError(11), OSError(35), OSError(119)):
            self.platform.instance.send_error = error
            self.assertIs(self.query.step(), dns.PENDING)
        self.clock.now = 3000
        self.assertIs(self.query.step(), dns.PENDING)
        self.clock.now = 6000
        with self.assertRaisesRegex(OSError, "deadline"):
            self.query.step()
        self.assertTrue(self.query.closed)
        for error in (OSError(5), RuntimeError("platform failure")):
            query = self.make_query()
            self.platform.instance.send_error = error
            with self.assertRaises(type(error)):
                query.step()
            self.assertTrue(query.closed)

    def test_short_send_receive_failure_and_cancellation_close_resources(self):
        self.platform.instance.short_send = True
        with self.assertRaisesRegex(OSError, "Incomplete"):
            self.query.step()
        self.assertTrue(self.query.closed)
        self.platform.instance.short_send = False
        query = self.make_query()
        query.step()
        self.platform.instance.received.append(OSError(5))
        with self.assertRaises(OSError):
            query.step()
        self.assertTrue(query.closed)
        query = self.make_query()
        query.step()
        query.close()
        self.assertIsNone(query.sock)

    def test_invalid_host_resolver_limits_and_entropy_fail_before_open(self):
        for host in ("", "a..b", "a..", "-a.test", "a-.test", "a_1.test", "é.test", "a" * 64,
                     "a." * 127 + "a"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                dns.SteppedQuery(host, "192.0.2.53")
        for server in ("", "0.0.0.0", "dns.example", "192.0.2.256", "192.00.2.53", "::1"):
            with self.subTest(server=server), self.assertRaises(ValueError):
                dns.SteppedQuery("example.com", server)
        for options in ({"timeout_ms": 0}, {"timeout_ms": True}, {"attempts": 0}, {"attempts": 5}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.make_query(**options)
        with self.assertRaises(ValueError):
            dns.SteppedQuery("example.com", "192.0.2.53", entropy=lambda n: b"")
        self.assertEqual(self.platform.opened, [])

    def test_interface_resolver_has_no_public_fallback(self):
        class WLAN:
            def ifconfig(self):
                return ("192.0.2.10", "255.255.255.0", "192.0.2.1", self.server)
        wlan = WLAN()
        wlan.server = "192.0.2.53"
        self.assertEqual(dns.resolver_address(wlan), wlan.server)
        for server in ("0.0.0.0", "", None, "dns.example"):
            wlan.server = server
            with self.assertRaises(OSError):
                dns.resolver_address(wlan)

    def test_https_uses_packaged_resolver_even_with_unrelated_global_module(self):
        class WLAN:
            def isconnected(self):
                return True

            def ifconfig(self):
                return ("192.0.2.10", "255.255.255.0", "192.0.2.1", "192.0.2.53")
        network = types.SimpleNamespace(STA_IF=0, WLAN=lambda interface: WLAN())
        # A global/frozen helper cannot supply the SDK's resolver accidentally.
        global_helper = types.SimpleNamespace()
        with patch.dict(sys.modules, {"network": network, "bounded_dns": global_helper}), \
                patch.object(dns.socket, "socket", side_effect=self.platform.socket), \
                patch.object(dns.time, "ticks_ms", side_effect=self.clock.ticks_ms, create=True), \
                patch.object(dns.time, "ticks_diff", side_effect=self.clock.ticks_diff, create=True):
            request = HTTPS("https://sync.example/sync/stream", {}, ())
            self.assertIs(request.step(), PENDING)
            self.assertIsInstance(request.query, dns.SteppedQuery)
            self.assertEqual(len(self.platform.instance.sent), 1)
            request.close()
            self.assertIsNone(request.query)
            self.assertEqual(self.platform.instance.closed, 1)


if __name__ == "__main__":
    unittest.main()
