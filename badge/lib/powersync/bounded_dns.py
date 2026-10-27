"""Bounded, foreground-stepped IPv4 DNS for the SDK's HTTPS transport.

Uses one nonblocking UDP socket, no getaddrinfo(), and no shared firmware helper.
Only classic 512-byte DNS responses with an echoed A/IN question are accepted.
CNAME chains must include their final A answer; TCP/IPv6/caching are unsupported.
Packet format: RFC 1035 sections 4.1/4.2; response matching: RFC 5452 section 9.1.
"""
import os
import socket
import struct
import time

PENDING = object()
_WOULD_BLOCK = (11, 35, 119)


def _ipv4(value):
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    return (len(parts) == 4 and all(part and len(part) <= 3
            and all("0" <= char <= "9" for char in part)
            and int(part) <= 255 and str(int(part)) == part for part in parts))


def resolver_address(wlan):
    """Use the connected interface's DNS server; never choose a public fallback."""
    address = wlan.ifconfig()[3]
    if not _ipv4(address) or address == "0.0.0.0":
        raise OSError("Wi-Fi has no IPv4 DNS resolver")
    return address


def _question(host):
    if not isinstance(host, str):
        raise ValueError("Invalid DNS hostname")
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    if not host or len(host) > 253:
        raise ValueError("Invalid DNS hostname")
    labels = host.split(".")
    for label in labels:
        if (not 1 <= len(label) <= 63 or label[0] == "-" or label[-1] == "-"
                or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in label)):
            raise ValueError("Invalid DNS hostname")
    wire = b"".join(bytes((len(label),)) + label.encode() for label in labels)
    return host.encode(), wire + b"\0\0\1\0\1"


def _name(packet, offset):
    labels, end, expanded = [], None, 0
    # Bounds both compression loops and total parsing work on an untrusted packet.
    for _ in range(128):
        if offset >= len(packet):
            raise ValueError("Truncated DNS name")
        count = packet[offset]
        if count & 0xc0 == 0xc0:
            if offset + 1 >= len(packet):
                raise ValueError("Truncated DNS pointer")
            pointer = ((count & 0x3f) << 8) | packet[offset + 1]
            if pointer >= offset:
                raise ValueError("Invalid DNS pointer")
            if end is None:
                end = offset + 2
            offset = pointer
        elif count & 0xc0:
            raise ValueError("Invalid DNS label")
        elif count == 0:
            return b".".join(labels).lower(), end if end is not None else offset + 1
        else:
            offset += 1
            if offset + count > len(packet):
                raise ValueError("Truncated DNS label")
            expanded += count + 1
            if expanded > 254:
                raise ValueError("DNS name exceeds limit")
            labels.append(packet[offset:offset + count])
            offset += count
    raise ValueError("DNS compression exceeds limit")


def _answer(packet, transaction, host):
    if len(packet) < 12 or len(packet) > 512:
        return PENDING
    ident, flags, questions, answers, _, _ = struct.unpack_from(">HHHHHH", packet)
    if ident != transaction or not flags & 0x8000 or flags & 0x7800 or questions != 1:
        return PENDING
    try:
        question, offset = _name(packet, 12)
        if question != host or packet[offset:offset + 4] != b"\0\1\0\1":
            return PENDING
        offset += 4
        if flags & 0x0200:
            raise OSError("Truncated DNS response; TCP is unsupported")
        if flags & 15:
            raise OSError("DNS resolver error %d" % (flags & 15))
        addresses, aliases = {}, {}
        for _ in range(answers):
            name, offset = _name(packet, offset)
            if offset + 10 > len(packet):
                return PENDING
            kind, cls, _, length = struct.unpack_from(">HHIH", packet, offset)
            offset += 10
            end = offset + length
            if end > len(packet):
                return PENDING
            if cls == 1 and kind == 1 and length == 4:
                addresses[name] = ".".join(str(value) for value in packet[offset:end])
            elif cls == 1 and kind == 5:
                target, after = _name(packet, offset)
                if after != end:
                    return PENDING
                aliases[name] = target
            offset = end
        for _ in range(len(aliases) + 1):
            if host in addresses:
                return addresses[host]
            if host not in aliases:
                break
            host = aliases[host]
    except (ValueError, IndexError):
        return PENDING
    raise OSError("DNS response has no IPv4 answer")


class SteppedQuery:
    """At most one UDP send or receive per step, with bounded attempts/time.

    Optional platform arguments allow deterministic host qualification. The
    default implementation uses MicroPython socket/time and hardware entropy.
    """
    def __init__(self, host, server, timeout_ms=3000, attempts=2,
                 socket_module=socket, time_module=time, entropy=os.urandom):
        self.host, question = _question(host)
        if not _ipv4(server) or server == "0.0.0.0":
            raise ValueError("DNS server must be an IPv4 address")
        if (type(timeout_ms) is not int or not 1 <= timeout_ms <= 60000
                or type(attempts) is not int or not 1 <= attempts <= 4):
            raise ValueError("Invalid DNS retry limits")
        random = entropy(2)
        if len(random) != 2:
            raise ValueError("DNS requires two entropy bytes")
        self.transaction = (random[0] << 8) | random[1]
        self.packet = struct.pack(">HHHHHH", self.transaction, 0x0100, 1, 0, 0, 0) + question
        self.server, self.timeout_ms, self.attempts = server, timeout_ms, attempts
        self.socket_module, self.time = socket_module, time_module
        self.sock = None
        self.started = None
        self.attempt = 0
        self.sent = False
        self.closed = False

    def close(self):
        self.closed = True
        if self.sock is not None:
            sock, self.sock = self.sock, None
            sock.close()

    def step(self):
        if self.closed:
            raise OSError("DNS query is closed")
        try:
            now = self.time.ticks_ms()
            if self.started is None:
                self.started, self.attempt = now, 1
            elif self.time.ticks_diff(now, self.started) >= self.timeout_ms:
                if self.attempt >= self.attempts:
                    raise OSError("DNS deadline")
                self.started, self.attempt, self.sent = now, self.attempt + 1, False
            if self.sock is None:
                self.sock = self.socket_module.socket(self.socket_module.AF_INET, self.socket_module.SOCK_DGRAM)
                self.sock.setblocking(False)
            if not self.sent:
                if self.sock.sendto(self.packet, (self.server, 53)) != len(self.packet):
                    raise OSError("Incomplete DNS query")
                self.sent = True
                return PENDING
            packet, source = self.sock.recvfrom(513)
            if source != (self.server, 53):
                return PENDING
            result = _answer(packet, self.transaction, self.host)
            if result is not PENDING:
                self.close()
            return result
        except OSError as exc:
            if exc.args and exc.args[0] in _WOULD_BLOCK:
                return PENDING
            self.close()
            raise
        except Exception:
            self.close()
            raise
