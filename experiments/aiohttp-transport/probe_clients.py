"""Host probes of pinned MicroPython HTTP clients; never contacts a badge/server.

Usage: python3 experiments/aiohttp-transport/probe_clients.py SOURCE_DIRECTORY
SOURCE_DIRECTORY contains micropython-lib/__init__.py and tangerino/__init__.py.
Only unused WebSocket imports and connection creation are stubbed. Response code
runs unchanged with in-memory readers, not CPython aiohttp. The fork's MicroPython
body path is selected explicitly and gets a nonblocking in-memory socket shim.
This deliberately does not claim to qualify MicroPython scheduling, TLS or RAM.
"""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch


SOURCE_DIRECTORY = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path(__file__).parent / "sources"


def load_client(name):
    manifest = json.loads((Path(__file__).parent / "sources.json").read_text())
    expected = next(item["sha256"] for item in manifest if item["name"] == name)
    source_file = SOURCE_DIRECTORY / name / "__init__.py"
    if hashlib.sha256(source_file.read_bytes()).hexdigest() != expected:
        raise ValueError("Probe source digest mismatch: " + name)
    package = "research_" + name.replace("-", "_")
    websocket = types.ModuleType(package + ".aiohttp_ws")
    for symbol in ("_WSRequestContextManager", "ClientWebSocketResponse", "WebSocketClient", "WSMsgType"):
        setattr(websocket, symbol, type(symbol, (), {}))
    sys.modules[websocket.__name__] = websocket
    spec = importlib.util.spec_from_file_location(
        package, source_file,
        submodule_search_locations=[str(SOURCE_DIRECTORY / name)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    spec.loader.exec_module(module)
    return module


class MemoryConnection(asyncio.StreamReader):
    """MicroPython compatibility names over an incremental host StreamReader."""
    def __init__(self):
        super().__init__()
        self.closed = False

    async def aclose(self):
        self.closed = True


class NonblockingSocket:
    def __init__(self, reader):
        self.reader = reader

    def read(self, size):
        if not self.reader._buffer:
            return b"" if self.reader._eof else None
        data = bytes(self.reader._buffer[:size])
        del self.reader._buffer[:size]
        return data


async def turns():
    # Fixed scheduler turns, no elapsed-time threshold or network dependency.
    for _ in range(4):
        await asyncio.sleep(0)


class ReadSomeChunked:
    """Illustrative partial-read fix, NOT a complete/hardened HTTP decoder.

    Unlike the candidates, return available payload before awaiting its CRLF.
    A production patch still needs strict framing, bounds and cleanup policies.
    """
    def __init__(self, content):
        self.content = content
        self.remaining = 0
        self.separator = False
        self.done = False

    async def read(self, size):
        if self.done:
            return b""
        if self.separator:
            if await self.content.readexactly(2) != b"\r\n":
                raise ValueError("chunk boundary")
            self.separator = False
        if not self.remaining:
            self.remaining = int(await self.content.readline(), 16)
            if not self.remaining:
                if await self.content.readexactly(2) != b"\r\n":
                    raise ValueError("chunk end")
                self.done = True
                return b""
        data = await self.content.read(min(size, self.remaining))
        if not data:
            raise EOFError("truncated chunk")
        self.remaining -= len(data)
        self.separator = self.remaining == 0
        return data


class ClientProbes(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.clients = {name: load_client(name) for name in ("micropython-lib", "tangerino")}
        cls.clients["tangerino"]._IS_MICROPYTHON = True

    def reader(self):
        reader = MemoryConnection()
        reader.s = NonblockingSocket(reader)
        return reader

    async def dispose(self, task):
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def test_small_fixed_response_waits_for_requested_size(self):
        for name, client in self.clients.items():
            with self.subTest(client=name):
                reader = self.reader()
                response = client.ClientResponse(reader)
                response.headers = {}
                reader.feed_data(b"{}\n")
                task = asyncio.create_task(response.read(2048))
                await turns()
                self.assertFalse(task.done())
                await self.dispose(task)

    async def test_chunk_payload_waits_for_chunk_suffix(self):
        for name, client in self.clients.items():
            with self.subTest(client=name):
                reader = self.reader()
                response = client.ChunkedClientResponse(reader)
                response.headers = {}
                reader.feed_data(b"3\r\n{}\n")
                task = asyncio.create_task(response.read(2048))
                await turns()
                self.assertFalse(task.done())
                reader.feed_data(b"\r\n")
                await turns()
                self.assertEqual(await task, b"{}\n")

    async def test_partial_large_chunk_waits_for_requested_size(self):
        for name, client in self.clients.items():
            with self.subTest(client=name):
                reader = self.reader()
                response = client.ChunkedClientResponse(reader)
                response.headers = {}
                reader.feed_data(b"1000\r\n{}\n")
                task = asyncio.create_task(response.read(2048))
                await turns()
                self.assertFalse(task.done())
                await self.dispose(task)

    async def test_content_is_raw_chunk_framing(self):
        for name, client in self.clients.items():
            with self.subTest(client=name):
                reader = self.reader()
                response = client.ChunkedClientResponse(reader)
                reader.feed_data(b"3\r\n{}\n\r\n")
                self.assertEqual(await response.content.read(2048), b"3\r\n{}\n\r\n")

    async def test_illustrative_fix_returns_small_update_before_chunk_suffix(self):
        for prefix in (b"3\r\n", b"1000\r\n"):
            with self.subTest(prefix=prefix):
                reader = self.reader()
                response = ReadSomeChunked(reader)
                reader.feed_data(prefix + b"{}\n")
                task = asyncio.create_task(response.read(2048))
                await turns()
                self.assertTrue(task.done())
                self.assertEqual(await task, b"{}\n")

    async def test_illustrative_fix_keeps_chunk_state_and_eof(self):
        reader = self.reader()
        response = ReadSomeChunked(reader)
        reader.feed_data(b"3\r\n{}\n\r\n2\r\n[]\r\n0\r\n\r\n")
        self.assertEqual(await response.read(2), b"{}")
        self.assertEqual(await response.read(2), b"\n")
        self.assertEqual(await response.read(2048), b"[]")
        self.assertEqual(await response.read(2048), b"")
        self.assertEqual(await response.read(2048), b"")

    async def test_post_response_parser_and_request_context_close(self):
        for name, client in self.clients.items():
            with self.subTest(client=name):
                reader = self.reader()
                reader.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                tls_context = object()
                calls = []

                session = client.ClientSession()

                async def request_raw(method, url, data, json, ssl, params, headers, **options):
                    calls.append((method, url, json, ssl))
                    # Mirrors ownership in each real request_raw: the fork saves
                    # the connection before headers, upstream only after headers.
                    if hasattr(session, "_writer"):
                        session._reader = session._writer = reader
                    return reader

                with patch.object(session, "request_raw", request_raw):
                    async with session:
                        async with session.post("https://sync.example/sync/stream",
                                                json={"streams": []}, ssl=tls_context) as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(await response.read(2), b"{}")
                    self.assertTrue(reader.closed)
                self.assertEqual(calls[0][0:3], ("POST", "https://sync.example/sync/stream", {"streams": []}))
                self.assertIs(calls[0][3], tls_context)

    async def test_cancel_during_headers_cleanup_differs(self):
        for name, client in self.clients.items():
            with self.subTest(client=name):
                reader = self.reader()
                reader.feed_data(b"HTTP/1.1 200 OK\r\n")
                session = client.ClientSession()

                async def request_raw(*args, **kwargs):
                    if hasattr(session, "_writer"):
                        session._reader = session._writer = reader
                    return reader

                async def request():
                    async with session:
                        async with session.post("https://sync.example/", json={"x": 1}):
                            pass

                with patch.object(session, "request_raw", request_raw):
                    task = asyncio.create_task(request())
                    await turns()
                    self.assertFalse(task.done())
                    await self.dispose(task)
                self.assertEqual(reader.closed, name == "tangerino")

    async def test_candidates_accept_conflicting_framing_and_oversized_headers(self):
        for name, client in self.clients.items():
            for header in (b"Content-Length: 1\r\nContent-Length: 2\r\n",
                           b"X-Large: " + b"x" * 5000 + b"\r\n",
                           b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n"):
                with self.subTest(client=name, header=header[:60]):
                    reader = self.reader()
                    reader.feed_data(b"HTTP/1.1 200 OK\r\n" + header + b"\r\n")
                    session = client.ClientSession()

                    async def request_raw(*args, **kwargs):
                        if hasattr(session, "_writer"):
                            session._reader = session._writer = reader
                        return reader

                    with patch.object(session, "request_raw", request_raw):
                        async with session:
                            async with session.post("https://sync.example/", json={"x": 1}) as response:
                                self.assertEqual(response.status, 200)

    async def test_redirect_retains_authorization_and_allows_http_target(self):
        for name, client in self.clients.items():
            with self.subTest(client=name):
                redirect = self.reader()
                redirect.feed_data(b"HTTP/1.1 302 Found\r\nLocation: http://other.example/\r\n\r\n")
                final = self.reader()
                final.feed_data(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
                replies = iter((redirect, final))
                calls = []
                session = client.ClientSession()

                async def request_raw(method, url, data, json, ssl, params, headers, **options):
                    calls.append((url, dict(headers)))
                    reader = next(replies)
                    if hasattr(session, "_writer"):
                        session._reader = session._writer = reader
                    return reader

                with patch.object(session, "request_raw", request_raw):
                    async with session:
                        async with session.post("https://sync.example/", json={"x": 1},
                                                headers={"Authorization": "Bearer synthetic-test-value"}):
                            pass
                self.assertEqual(calls[1][0], "http://other.example/")
                self.assertEqual(calls[1][1]["Authorization"], "Bearer synthetic-test-value")


if __name__ == "__main__":
    unittest.main(verbosity=2)
