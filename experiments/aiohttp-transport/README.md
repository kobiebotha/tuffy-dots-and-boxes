# MicroPython aiohttp transport investigation

Investigated 2026-10-26 (America/Denver), against application commit `a7d529ba`.

**Recommendation: retain the custom transport for now, for demonstrated reasons.**
Both aiohttp candidates implement useful HTTP clients, but neither provides the
current latency, DNS, memory and framing contract through a small adapter alone.
The replaceable code is only 260 lines. A scheduler bridge, bounded connector,
partial-read decoder, limits and lifecycle fixes would consume much of that
saving and create a maintained downstream patch set. Reconsider upstream after
those capabilities land, or if the application adopts asyncio more broadly.

Production files, packaged app copies, backend configuration and the badge were
not changed or operated. No credentials or device files were read. The isolated
experiment contacts no service when running its probes.

## Revisions and primary sources

Branch names were resolved through public GitHub repository/commit metadata.
The exact HTTP files and their SHA-256 digests are in [sources.json](sources.json).

| Source | Revision examined | Relevant primary files |
| --- | --- | --- |
| MicroPython `micropython-lib` | `7cbdd3ab8ef97edc17846f8b7cbb9e63e7a855c4` (master, 2026-10-17) | [HTTP implementation](https://github.com/micropython/micropython-lib/blob/7cbdd3ab8ef97edc17846f8b7cbb9e63e7a855c4/python-ecosys/aiohttp/aiohttp/__init__.py), [README](https://github.com/micropython/micropython-lib/blob/7cbdd3ab8ef97edc17846f8b7cbb9e63e7a855c4/python-ecosys/aiohttp/README.md), [manifest](https://github.com/micropython/micropython-lib/blob/7cbdd3ab8ef97edc17846f8b7cbb9e63e7a855c4/python-ecosys/aiohttp/manifest.py) |
| Tangerino adaptation | `2591ac0cb8b526881b73490326d7b07c89d05050` (main, 2026-06-29) | [single-module implementation](https://github.com/Tangerino/aiohttp/blob/2591ac0cb8b526881b73490326d7b07c89d05050/aiohttp.py), [README](https://github.com/Tangerino/aiohttp/blob/2591ac0cb8b526881b73490326d7b07c89d05050/README.md), [tests](https://github.com/Tangerino/aiohttp/blob/2591ac0cb8b526881b73490326d7b07c89d05050/test_aiohttp.py) |
| Public Badgeware Tufty release v3.1.1 | `ee84772fb7f7d5424e9593029caba815c6f89c80` | [board manifest](https://github.com/pimoroni/tufty2350/blob/ee84772fb7f7d5424e9593029caba815c6f89c80/board/manifest.py), [fetch.py](https://github.com/pimoroni/tufty2350/blob/ee84772fb7f7d5424e9593029caba815c6f89c80/modules/common/fetch.py), [managed run loop](https://github.com/pimoroni/tufty2350/blob/ee84772fb7f7d5424e9593029caba815c6f89c80/modules/common/badgeware/__init__.py) |
| Target-compatible Pimoroni MicroPython 1.29.0 sources | `41d1e007537f9b15cf13a9436228dd59d6485eca`, pinned by this repo's `native-sqlite/dependencies.json` | [asyncio streams](https://github.com/pimoroni/micropython/blob/41d1e007537f9b15cf13a9436228dd59d6485eca/extmod/asyncio/stream.py), [scheduler](https://github.com/pimoroni/micropython/blob/41d1e007537f9b15cf13a9436228dd59d6485eca/extmod/asyncio/core.py), [TLS implementation](https://github.com/pimoroni/micropython/blob/41d1e007537f9b15cf13a9436228dd59d6485eca/extmod/modtls_mbedtls.c), [lwIP DNS](https://github.com/pimoroni/micropython/blob/41d1e007537f9b15cf13a9436228dd59d6485eca/extmod/modlwip.c) |
| `micropython-lib` submodule at that firmware source revision | `ee4bb8ff139e24c42b739935fbd8ec7c4d061e02` | [ssl compatibility wrapper](https://github.com/micropython/micropython-lib/blob/ee4bb8ff139e24c42b739935fbd8ec7c4d061e02/python-stdlib/ssl/ssl.py) |

The target source pin is not proof of every file or build option in the installed
firmware. No fresh installed-module inventory was taken: the badge is unplugged.

This is **MicroPython's small aiohttp implementation**, not PyPI/CPython aiohttp.
Do not infer `ClientTimeout`, `TCPConnector`, connection pools, bounded header
parsing, `iter_chunked` or CPython streaming semantics from the shared name.
The upstream HTTP file is 278 lines / 8,989 bytes; the fork combines HTTP,
WebSockets and portability helpers into 906 lines / 35,072 bytes. Neither count
is a runtime RAM measurement.

## Capability comparison (source findings)

| Requirement | micropython-lib aiohttp | Tangerino adaptation |
| --- | --- | --- |
| JSON POST | Yes, `session.post(..., json=dict, ssl=context)`. Truthiness checks mishandle an empty dict; use serialized, bounded nonempty data if necessary. | Same JSON API and empty-dict caveat; builds requests portably for CPython too. |
| Long-lived downloads | Can keep a response open and read incrementally; no total-lifetime timeout is imposed. | Same; configured timeouts apply per operation rather than the whole stream lifetime. |
| Incremental/chunked decoding | `ChunkedClientResponse` decodes chunks, but `read(n)` uses `readexactly(min(n, chunk_remaining))`. Nonchunked `read(n)` also uses `readexactly(n)`. | Retains fill-to-size semantics. MicroPython `_drain_read(n)` drains directly from `content.s`, then sleeps/retries until n bytes, EOF, error or timeout. |
| Prompt small updates | Fails the required partial-available behavior. A chunk's final payload is held until its trailing CRLF is read too. | Same latency failures, including in the MicroPython fast path. A 10 ms retry sleep is also introduced when the socket has no data. |
| Explicit CA + hostname | Accepts an SSL context; asyncio supplies the original hostname when dialing that hostname. Caller must configure trust explicitly. | Same. Keep-alive connection identity uses `bool(ssl)`, not the trust context's identity; avoid reusing a session across trust policies. |
| Connection/read deadlines | Upstream explicitly leaves timeouts TODO. Need setup, inactivity and RPC deadlines in the bridge. | Adds `asyncio.wait_for` to connect/send/header/body operations, default `None`. This is not an overall setup deadline or a reset-on-every-byte inactivity policy. |
| DNS | `asyncio.open_connection` calls synchronous `socket.getaddrinfo`. A task or `wait_for` cannot preempt that call. | Adds another explicit synchronous `socket.getaddrinfo` for diagnostic timing before the timed open_connection call, even when logging is disabled. |
| Bounded memory | No HTTP header aggregate/line limits, bounded JSON convenience reads or strict framing policy comparable to the current transport. Default chunk read can allocate up to 4 MiB. | Same important limits missing; direct drain collects fragments and joins them. Keep-alive and decompression add behavior this app does not need. |
| Cancellation/cleanup | Response context closes after successful entry. Session exit does not close; cancellation or parse error before response entry can leave a connection owned only by local state. | Session exit closes established session-owned connections; timeout paths improve cleanup. Still inspect failures/cancellation before `request_raw` saves the socket, and inside `asyncio.open_connection`. |
| Upload with open stream | Feasible with **two separate ClientSession instances**, each owning a response/socket. One session stores one `_reader`; it is not a concurrency/pooling API. | Also use separate sessions. Reusing a session while its streaming response remains active can close/replace its connection. |
| Frame callback integration | Requires an asyncio pump and a demand-driven stepped bridge; not a drop-in factory substitution. | Same. |

Both automatically follow 301–303 redirects using the same method, body and
headers; neither exposes the no-redirect policy required here. Both accept
duplicate/conflicting Content-Length and Transfer-Encoding combinations that
the current parser rejects. Upstream's transfer-encoding value check is also
case-sensitive; the fork improves it. Preserve `Accept-Encoding: identity` and
reject unexpected compression; per-fragment decompression is not a bounded
long-lived NDJSON decompressor.

The fork's direct-drain helper also turns non-retry socket errors into short
returns, and EOF can leave a chunk unfinished without raising a framing error.
A production patch must propagate real errors and detect truncated chunks;
bounded read size alone does not supply those guarantees.

Reading `response.content.read(2048)` is **not** a shortcut: `content` is the raw
stream, including chunk lengths and CRLF. It bypasses HTTP decoding. Reducing
`read(n)` to one byte avoids the large-fill wait but does not remove the trailing
CRLF wait, and adds substantial per-byte work.

Tangerino's useful improvements are per-operation timeouts, session-owned
cleanup, HTTP/1.1 keep-alive, content-length bookkeeping, CPython compatibility
and case-insensitive chunked detection. Its claimed ESP32 greedy-read speedup
does not establish Tufty performance. The pinned Tufty-compatible Mbed TLS poll
implementation already checks `mbedtls_ssl_check_pending` and reports readable
decrypted data (`modtls_mbedtls.c`, approximately lines 992–1004). Do not assume
the fork's stated TCP-readiness problem exists on this firmware.

## Badgeware helpers (source findings, not installed-module tests)

The public v3.1.1 `fetch.AsyncFetch` source is a genuine alternative worth
comparing: `update()` advances a generator, with 1024-byte reads, a default
4096-byte step budget, an inactivity deadline, chunked decoding and a reset
method that closes the socket. It accepts an explicit method and pre-encoded
body, so a JSON POST can be made by serializing data and supplying Content-Type.
Its managed `run(update)` clears the frame, invokes the callback, presents the
display and polls inputs; it does not pump asyncio.

It does not satisfy this transport contract unchanged:

- `_connect()` calls blocking `socket.getaddrinfo`, then explicitly sets
  `tls.CERT_NONE`. No public CA/context injection exists. It passes a hostname
  and defers the handshake, but that does not enable certificate verification.
- Response data accumulates in a growable memory buffer, default cap 256 KiB,
  or is written to a named file. Tiny available bytes are read promptly, but
  there is no public consumptive/backpressured body-read interface. A long-lived
  stream eventually exhausts its memory cap or keeps writing a file; completion
  callbacks and `.to_json()` do not solve streaming delivery.
- Its 4096-byte header check runs after `_read_line()` completes. A single
  unterminated header/chunk-size line can still grow without that bound. Its
  framing policy is less strict than the current parser.
- Independent instances can own independent connections, but bounded simultaneous
  upload/download behavior and cancellation at each connect/parse phase have
  not been tested here. `reset()` is useful cleanup, not a qualified transport
  contract. Patching resolver, verified TLS, consumption, strict framing and
  bounds would replace substantial parts of a 774-line helper.

The board manifest also requires `bundle-networking` and `urllib.urequest`.
At the target-compatible micropython-lib revision, the
[networking bundle](https://github.com/micropython/micropython-lib/blob/ee4bb8ff139e24c42b739935fbd8ec7c4d061e02/micropython/bundles/bundle-networking/manifest.py)
requires `requests` and its `urequests` compatibility alias. The
[requests source](https://github.com/micropython/micropython-lib/blob/ee4bb8ff139e24c42b739935fbd8ec7c4d061e02/python-ecosys/requests/requests/__init__.py)
supports JSON POST and incremental chunk-decoding `Response.raw` reads, but
performs synchronous DNS/connect/read/write and synchronous TLS handshake,
explicitly disables certificate verification, and has no CA/context argument.
Its socket timeout is set after DNS. This does not preserve frame responsiveness
or verified HTTPS. `urllib.urequest` was established as a manifest dependency;
its exact implementation was not retrieved, so it is not treated as a qualified
alternative on the strength of its name.

Neither that board manifest nor its networking bundle directly requires the
full aiohttp package. The HTTP source at micropython-lib revision
`ee4bb8ff...` is byte-identical to the current `7cbdd3ab...` file examined here.
This still does **not** establish what the badge's installed/frozen module named
`aiohttp` exports. It may expose only WebSocket support. A future authorized
inventory should check `ClientSession`, `post` and response classes, module path
and source/build provenance, rather than relying on import success. The
installed `bounded_dns` helper's complete provenance likewise remains unresolved;
its API use is established by this app, not requalified in this investigation.

For reproducibility, SHA-256 of the public v3.1.1 `fetch.py` inspected here is
`11bb4cb6163b2cee44e86e2cbe7a1e67bc051c591d7e11d88f092bfaf323f974`;
managed Badgeware `__init__.py` is
`7bab4587af23ad4de3dbbe7fdcc40291b517b24c69a68c5e39bc862e331fa9a6`.
Public release source availability is not proof that the installed filesystem
helpers match it.

## Preserve the current contract

The custom `http.py` has 334 physical lines / 13,685 bytes. `Response` is 115
lines and `HTTPS` 145: **260 lines is the gross deletion opportunity**.
`Gateway` (46 lines), URL/request validation, constants and `PENDING` remain
useful. The 287-line PowerSync NDJSON parser in `stream.py` remains necessary.

The existing source provides:

- Explicit CA files, `tls.CERT_REQUIRED`, the original hostname, deferred TLS
  handshake and GCM/CHACHA20 cipher selection.
- A stepped `bounded_dns.SteppedQuery`, 3-second query timeout and two attempts;
  nonblocking TCP and zero-timeout polling.
- 512-byte request writes/JSON reads and 2048-byte download reads, which return
  available bytes rather than filling the buffer.
- 4096-byte HTTP headers, 16 KiB request/JSON response limits and strict framing
  rejection; setup/headers deadline 15 seconds and idle limits 15 seconds for
  JSON / 60 seconds for streams.
- Sync budgets of two transport reads and six core records per frame;
  2048-byte NDJSON fragments, 8192-byte records, bounded metadata/nesting.

Those deadlines are checked between steps, not preemptive guarantees on a TLS
cryptographic call. `HTTPS.step()` also relies on its caller to close on most
exceptions; `SyncClient` and pairing provide that cleanup. Retaining this
transport is not a claim that its hardware behavior is fully qualified by these
host tests.

During upload, current sync code leaves the download socket **open**, but pauses
download consumption until upload finishes. An async replacement must retain
this backpressure, checkpoint publication, persisted move UUID and one-socket
fallback on second-TLS `MemoryError`.

## Smallest viable integration if revisited

Keep `.step() -> PENDING | bytes | None`, `.status` and `.close()`.

1. **One shared foreground scheduler pump.** Badgeware owns `run(update)` and
   display/input/VSYNC. Do not call `asyncio.run()` around each HTTP request or
   run the loop until a network operation completes in `update`. A public-API
   prototype can pump `loop.run_until_complete(frame_turn())`, where
   `async def frame_turn(): await asyncio.sleep_ms(0)`. Call it from every frame,
   including upload, pairing and cancellation/exit cleanup. This services ready
   tasks and returns through an immediate sentinel; it is a cooperative turn,
   not a measured millisecond or task-count budget. It can still block inside
   DNS or any other synchronous operation. For an explicit scheduler budget,
   the smallest reusable upstream improvement is a supported
   `run_once(timeout_ms=0, max_resumes=...)`; private TaskQueue patching would
   itself become custom maintenance. A turn budget cannot preempt a coroutine.
2. **Demand-driven response bridge.** Schedule request establishment, copy status
   as soon as headers finish, and keep at most one pending read/result of at most
   2048 bytes (512 for JSON). Request the next read only when the consumer drains
   the previous one. Store exceptions for `.step()` to raise. Return `None` at
   EOF while retaining status. Feed the existing heartbeat and enforce the
   existing setup/idle deadlines. Cancel/close all owned work on `.close()`.
   Qualify an additional bounded pump when `.step()` requests its next read:
   pumping only once at frame start can otherwise delay the second already-
   available fragment until the next frame, regressing an existing sync test.
   Shutdown must close sockets immediately or drain cancellation/finally work
   before Badgeware unloads the module; merely scheduling `aclose()` is not
   sufficient at Home/exit.
3. **Per-session connection hook.** Add `open_connection=`/connector injection
   to aiohttp, rather than globally monkeypatching asyncio. Adapt existing
   `bounded_dns` using short yields, then dial the resulting numeric address
   while passing the **original** hostname separately for SNI and certificate
   verification. Numeric-address parsing avoids a DNS network query; do not
   substitute an IP in the request URL or verification hostname. The fork's
   diagnostic lookup must use the same hook or be removed. Establish socket
   ownership immediately and close it in `finally` on connect/TLS cancellation
   or failure, including before asyncio returns its Stream. The pinned
   `open_connection` has no such cleanup guard. A thin wrapper cannot close a
   socket hidden in a cancelled coroutine, so a small connector implementation
   or an upstream asyncio fix is needed.
4. **HTTP changes beyond the bridge.** Add decoded `read_some(n)` semantics to
   both response types, retain chunk state and defer consuming a chunk suffix
   until the next read. Add content-length EOF/truncation tracking, bounds on
   headers/chunk metadata, strict framing validation, compression rejection and
   a no-redirect option checked **before** redirecting. Slice outgoing writes
   into bounded pieces and yield. The illustrative decoder in the probe proves
   the partial-read direction only; it is deliberately not production-ready.
5. **Explicit TLS configuration.** Use a `tls.SSLContext`, load the same bounded
   CA files, assign/verify `CERT_REQUIRED`, retain cipher policy and use deferred
   handshake. `ssl=True` is insufficient: the pinned micropython-lib `ssl`
   compatibility wrapper sets `CERT_NONE`. Pass the configured context all the
   way to the connector. Use one session per live request; disable keep-alive
   initially and preserve no redirects.
6. **Wire every caller.** Inject the HTTP factory for downloads and an explicit
   `json_factory` wrapper constructing `Gateway(factory=...)` for checkpoints,
   credentials and uploads. `Gateway` captures its default factory at function
   definition, so changing just `SyncClient.http_factory` is insufficient.
   Pairing in `dots_client.py` directly constructs `Gateway` and needs an
   injection point too. Do not change native core or NDJSON publication logic.

This is app-level work; no firmware replacement is inherently necessary for a
prototype. A supported scheduler tick or asyncio cancellation fix would improve
the upstream route, but modifying firmware is a separate decision.

## Isolated evidence and reproduction

```sh
python3 experiments/aiohttp-transport/fetch_sources.py
python3 experiments/aiohttp-transport/probe_clients.py
python3 -m unittest discover -s badge/tests
```

The fetcher downloads only two public, revision/hash-pinned source files into
ignored `sources/`. The probe does not import the installed CPython aiohttp.
It executes the pinned modules, stubbing unused WebSocket imports and injecting
in-memory `request_raw` connections. CPython StreamReader supplies incremental
reads. For the fork, `_IS_MICROPYTHON=True` selects its actual greedy-drain code
over a synchronous nonblocking memory-socket shim. Therefore its CPython
fallback does not mask the MicroPython fill-to-size behavior.

**Ten host probe tests passed.** They establish:

- Both clients hold a three-byte update when asked for 2048 bytes, including
  inside a partially delivered 4096-byte HTTP chunk.
- Both hold a complete three-byte chunk payload until its trailing CRLF arrives.
- `content.read()` exposes raw HTTP chunk framing.
- Both parse an injected POST response and close normally after response entry;
  the SSL object reaches injected `request_raw` unchanged.
- With ownership matching each implementation, cancellation during headers
  closes the fork's session-owned connection; upstream's session context leaves
  it open.
- Both accept a 5000-byte header, duplicate CL and TE+CL; both forward unchanged
  authorization headers into a redirected HTTP target. All test values are
  synthetic; no network request is sent.
- A small illustrative partial-read decoder returns those tiny payloads before
  a suffix or larger requested buffer fills, and preserves multi-chunk/EOF state.

Request serialization, real connection ownership, DNS, TLS verification,
MicroPython's scheduler, TLS WANT_READ/WANT_WRITE, frame latency and RAM were
**not executed by these probes**. The injected ownership model supports the
source finding; it is not an end-to-end cancellation qualification. The
unmodified upstream request builder also uses MicroPython-specific bytes
formatting, so these probes deliberately do not pretend to exercise it on
CPython.

The first full badge regression run passed **181 tests**. Additional latency
work appeared concurrently in the workspace and was left untouched. The final
full-suite run passed **229 tests** in 28.151 seconds, with no skips reported.

## Cost, unknowns and switch gate

Gross deletion: 260 lines. Planning estimate for a production bridge and policy
glue is roughly 100–180 lines, plus 50–120 lines of connector/HTTP/scheduler
changes. That leaves approximately **a small increase to 110 lines saved**, not
a demonstrated large reduction; these ranges are estimates, not implemented
counts. Upstream package/dependency code still ships, and downstream patches
need rebasing and qualification. Keeping the current connector while replacing
only framing removes even less code. The fork's extra keep-alive/OTA behavior
does not justify its larger surface for this app's long-lived stream.

Estimated integration work: 2–4 engineering days to implement and host-test a
bounded patched-upstream prototype, followed by hardware qualification whose
duration depends on findings. This is not a benchmark or delivery commitment.
An adapter alone is insufficient; the necessary patches touch several layers,
so neither candidate currently meets the maintenance-reduction objective.

Before switching, require:

1. Shared transport contract tests against old/new implementations: JSON POST,
   non-2xx/auth refresh, bounded requests/responses, incremental fixed-length /
   close-delimited / chunked bodies, tiny updates, fragmented chunk suffixes,
   malformed/truncated framing, compression/redirect rejection and EOF/status.
2. Deterministic DNS/setup/idle timeout and cancellation tests at DNS, TCP, TLS,
   request write, headers and body; assert all sockets, resolver queries, tasks
   and poll registrations disappear. Exercise peer resets, Wi-Fi loss, repeated
   reconnect, error propagation and cancellation before response entry.
3. Run existing sync/SDK tests through the new injected factories, including
   pairing, refresh, checkpoint RPCs, duplicate UUID retries, accepted/rejected
   uploads, full-checkpoint publication and second-connection MemoryError.
   Slow uploads must keep the stream open with bounded read-ahead.
4. With fresh user authorization, test the actual firmware's wrong-host,
   untrusted-chain and expired-certificate failures, valid CA success, resolved
   IP with original hostname, deferred TLS handshake and cancellation/Home.
5. On that badge, compare longest frame/update duration, small-update-to-
   publication latency and heap high-water/fragmentation against this transport:
   startup, idle keepalives, full 49-move download, uploads with open download,
   DNS failure, reconnect storms and two TLS connections. Retain the 16-second
   watchdog and existing two-read/six-record work budgets. Measure rather than
   infer hardware behavior from host tests or ESP32 claims.

Unresolved: exact installed helper provenance, on-device scheduler fairness,
worst TLS-call duration, two-connection memory headroom with asyncio, and whether
upstream maintainers would accept the small-read/connector/policy changes.
These questions justify a later experiment if maintenance benefits increase;
they do not justify replacing the current production transport now.
