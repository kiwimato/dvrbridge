"""Minimal-but-correct RTSP 1.0 server serving live H.264 from the StreamHub.

Supported: OPTIONS, DESCRIBE, SETUP (RTP/AVP/TCP interleaved + RTP/AVP UDP
unicast), PLAY, PAUSE (no-op), GET_PARAMETER / SET_PARAMETER (keepalive),
TEARDOWN. One video track per stream (a=control:track0). Optional Basic auth.

Interleaved TCP is the primary transport (it is what go2rtc, Frigate and
`ffmpeg -rtsp_transport tcp` use) and needs no port negotiation.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from urllib.parse import unquote, urlparse

from .. import __version__
from ..hub import Stream, StreamHub, Subscriber
from .rtp import H264Packetizer
from .sdp import build_sdp

log = logging.getLogger("dvrbridge.rtsp")

SERVER_ID = f"dvrbridge/{__version__}"
ALLOWED = "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER, SET_PARAMETER"

# Hostile-input bounds. RTSP is a tiny text protocol; nothing legitimate comes
# close to these, so they only bite abuse (slowloris, oversized bodies, an
# interleaved-length DoS).
MAX_BODY = 64 * 1024
MAX_INTERLEAVED = 64 * 1024
MAX_HEADERS = 100  # generous: real RTSP requests carry a handful
MAX_HEADER_BYTES = 16 * 1024
IDLE_TIMEOUT = 70.0  # > the 60s Session timeout we advertise; clients keepalive
REQUEST_TIMEOUT = 15.0  # once a request starts, it must fully arrive within this
MAX_CONNECTIONS = 64  # concurrent client sockets; excess is refused (flood guard)


class RTSPError(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(f"{code} {reason}")
        self.code, self.reason = code, reason


class _UdpSender(asyncio.DatagramProtocol):
    def connection_made(self, transport) -> None:
        self.transport = transport

    def error_received(self, exc) -> None:  # ICMP unreachable etc.
        log.debug("udp send error: %s", exc)


class ClientConnection:
    def __init__(self, server: "RTSPServer", reader, writer) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.peer = writer.get_extra_info("peername")
        self.session_id: str | None = None
        self.stream: Stream | None = None
        self.sub: Subscriber | None = None
        self.packetizer = H264Packetizer(ssrc=secrets.randbits(32))
        self.transport_hdr: str | None = None
        self.interleaved: tuple[int, int] | None = None
        self.udp: tuple[asyncio.DatagramTransport, tuple[str, int]] | None = None
        self.play_task: asyncio.Task | None = None
        # serialize all socket writes: the media pump and the request loop
        # both write to self.writer from independent tasks
        self._wlock = asyncio.Lock()

    # ---------------- request plumbing ----------------

    async def run(self) -> None:
        try:
            while True:
                await self._read_request()
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            pass  # client went away, or idled past IDLE_TIMEOUT
        except RTSPError as e:
            await self._respond(e.code, e.reason, {})  # malformed framing
        except Exception:
            log.exception("connection %s crashed", self.peer)
        finally:
            await self.close()

    async def _read_request(self) -> None:
        # Between requests, wait up to IDLE_TIMEOUT for the first byte (clients
        # keepalive via GET_PARAMETER). Once a request starts, the whole thing
        # must arrive within REQUEST_TIMEOUT — otherwise a slowloris client
        # dribbling one byte at a time would pin the connection open forever.
        first = await asyncio.wait_for(self.reader.readexactly(1), IDLE_TIMEOUT)
        async with asyncio.timeout(REQUEST_TIMEOUT):
            if first == b"$":  # interleaved client->server data (RTCP reports)
                hdr = await self.reader.readexactly(3)
                length = int.from_bytes(hdr[1:3], "big")
                if length > MAX_INTERLEAVED:
                    raise RTSPError(400, "Bad Request")
                await self.reader.readexactly(length)
                return
            line = first + await self.reader.readline()
            if not line.strip():
                return
            headers: dict[str, str] = {}
            header_bytes = 0
            while True:
                h = await self.reader.readline()
                if h in (b"\r\n", b"\n", b""):
                    break
                header_bytes += len(h)
                # cap total header size AND count so an endless stream of
                # distinct `a:b\r\n` lines can't grow memory without bound
                if header_bytes > MAX_HEADER_BYTES or len(headers) >= MAX_HEADERS:
                    raise RTSPError(431, "Request Header Fields Too Large")
                if b":" in h:
                    k, v = h.split(b":", 1)
                    headers[k.decode().strip().lower()] = v.decode().strip()
            body = b""
            if "content-length" in headers:
                try:
                    n = int(headers["content-length"])
                except ValueError:
                    raise RTSPError(400, "Bad Request") from None
                if not 0 <= n <= MAX_BODY:
                    raise RTSPError(413, "Request Entity Too Large")
                body = await self.reader.readexactly(n)
        await self._dispatch(line.decode("latin-1").strip(), headers, body)

    async def _dispatch(self, request_line: str, headers: dict, body: bytes) -> None:
        parts = request_line.split()
        if len(parts) != 3:
            return await self._respond(400, "Bad Request", {})
        method, url, _version = parts
        cseq = headers.get("cseq", "0")
        log.debug("%s %s %s", self.peer, method, url)
        try:
            if self.server.auth and method in ("DESCRIBE", "SETUP", "PLAY"):
                self._check_auth(headers)
            handler = getattr(self, f"_on_{method.lower()}", None)
            if handler is None:
                raise RTSPError(501, "Not Implemented")
            await handler(url, headers, cseq)
        except RTSPError as e:
            extra = {"WWW-Authenticate": 'Basic realm="dvrbridge"'} if e.code == 401 else {}
            await self._respond(e.code, e.reason, {"CSeq": cseq, **extra})
        except TimeoutError:
            # device did not produce SPS/PPS in time
            await self._respond(504, "Gateway Timeout", {"CSeq": cseq})

    async def _respond(
        self, code: int, reason: str, headers: dict, body: bytes | str = b""
    ) -> None:
        if isinstance(body, str):
            body = body.encode()
        lines = [f"RTSP/1.0 {code} {reason}", f"Server: {SERVER_ID}"]
        headers.setdefault("CSeq", "0")
        if body:
            headers["Content-Length"] = str(len(body))
        lines += [f"{k}: {v}" for k, v in headers.items()]
        payload = ("\r\n".join(lines) + "\r\n\r\n").encode() + body
        async with self._wlock:
            self.writer.write(payload)
            await self.writer.drain()

    def _check_auth(self, headers: dict) -> None:
        expect = base64.b64encode(
            f"{self.server.auth[0]}:{self.server.auth[1]}".encode()
        ).decode()
        got = headers.get("authorization", "")
        if not (got.startswith("Basic ") and secrets.compare_digest(got[6:], expect)):
            raise RTSPError(401, "Unauthorized")

    def _stream_for(self, url: str) -> Stream:
        path = unquote(urlparse(url).path).strip("/")
        if path.endswith("/track0"):
            path = path[: -len("/track0")]
        try:
            return self.server.hub.get(path)
        except KeyError:
            raise RTSPError(404, "Not Found") from None

    # ---------------- methods ----------------

    async def _on_options(self, url: str, headers: dict, cseq: str) -> None:
        await self._respond(200, "OK", {"CSeq": cseq, "Public": ALLOWED})

    async def _on_describe(self, url: str, headers: dict, cseq: str) -> None:
        stream = self._stream_for(url)
        await stream.wait_ready(self.server.describe_timeout)
        sdp = build_sdp(
            stream.name,
            stream.sps,
            stream.pps,
            width=stream.info.width,
            height=stream.info.height,
        )
        await self._respond(
            200,
            "OK",
            {
                "CSeq": cseq,
                "Content-Base": url.rstrip("/") + "/",
                "Content-Type": "application/sdp",
            },
            sdp,
        )

    async def _on_setup(self, url: str, headers: dict, cseq: str) -> None:
        if self.play_task is not None:
            raise RTSPError(455, "Method Not Valid in This State")
        # a client may re-SETUP before PLAY; release any transport from a prior
        # SETUP so we don't leak the bound UDP socket
        self._release_transport()
        self.stream = self._stream_for(url)
        transport = headers.get("transport", "")
        fields = [f.strip() for f in transport.split(";")]
        proto = fields[0].upper() if fields else ""
        self.session_id = self.session_id or secrets.token_hex(8)

        if proto == "RTP/AVP/TCP":
            self.interleaved = (0, 1)
            for f in fields:
                if f.startswith("interleaved="):
                    a, _, b = f[len("interleaved=") :].partition("-")
                    try:
                        lo = int(a)
                        hi = int(b) if b else lo + 1
                    except ValueError:
                        raise RTSPError(400, "Bad Request") from None
                    # interleaved channel is a single byte on the wire
                    if not (0 <= lo <= 255 and 0 <= hi <= 255):
                        raise RTSPError(400, "Bad Request")
                    self.interleaved = (lo, hi)
            resp_transport = (
                f"RTP/AVP/TCP;unicast;interleaved="
                f"{self.interleaved[0]}-{self.interleaved[1]};"
                f"ssrc={self.packetizer.ssrc:08X}"
            )
        elif proto in ("RTP/AVP", "RTP/AVP/UDP"):
            client_port = None
            for f in fields:
                if f.startswith("client_port="):
                    a, _, _b = f[len("client_port=") :].partition("-")
                    try:
                        client_port = int(a)
                    except ValueError:
                        raise RTSPError(400, "Bad Request") from None
                    if not (1 <= client_port <= 65534):
                        raise RTSPError(400, "Bad Request")
            if client_port is None:
                raise RTSPError(461, "Unsupported Transport")
            loop = asyncio.get_running_loop()
            transport_udp, _ = await loop.create_datagram_endpoint(
                _UdpSender, local_addr=(self.server.listen_host_for_udp, 0)
            )
            rtp_port = transport_udp.get_extra_info("sockname")[1]
            self.udp = (transport_udp, (self.peer[0], client_port))
            resp_transport = (
                f"RTP/AVP;unicast;client_port={client_port}-{client_port + 1};"
                f"server_port={rtp_port}-{rtp_port + 1};"
                f"ssrc={self.packetizer.ssrc:08X}"
            )
        else:
            raise RTSPError(461, "Unsupported Transport")
        await self._respond(
            200,
            "OK",
            {
                "CSeq": cseq,
                "Transport": resp_transport,
                "Session": f"{self.session_id};timeout=60",
            },
        )

    async def _on_play(self, url: str, headers: dict, cseq: str) -> None:
        if self.stream is None or (self.interleaved is None and self.udp is None):
            raise RTSPError(455, "Method Not Valid in This State")
        await self._respond(
            200,
            "OK",
            {
                "CSeq": cseq,
                "Session": self.session_id or "",
                "Range": "npt=now-",
                "RTP-Info": f"url={url.rstrip('/')}/track0;seq={self.packetizer.seq}",
            },
        )
        if self.play_task is None:
            self.sub = self.stream.subscribe()
            self.play_task = asyncio.get_running_loop().create_task(
                self._pump(), name=f"play:{self.stream.name}:{self.peer}"
            )

    async def _on_pause(self, url: str, headers: dict, cseq: str) -> None:
        await self._respond(200, "OK", {"CSeq": cseq, "Session": self.session_id or ""})

    async def _on_get_parameter(self, url: str, headers: dict, cseq: str) -> None:
        await self._respond(200, "OK", {"CSeq": cseq, "Session": self.session_id or ""})

    _on_set_parameter = _on_get_parameter

    async def _on_teardown(self, url: str, headers: dict, cseq: str) -> None:
        await self._respond(200, "OK", {"CSeq": cseq, "Session": self.session_id or ""})
        await self._stop_play()
        self._release_transport()
        self.stream = None
        self.session_id = None

    # ---------------- media pump ----------------

    async def _pump(self) -> None:
        assert self.sub is not None
        try:
            while True:
                frame = await self.sub.queue.get()
                # build the whole access unit, then write it as one locked unit
                # so it can never interleave with an RTSP response on the wire
                if self.interleaved is not None:
                    ch = self.interleaved[0].to_bytes(1, "big")
                    blob = b"".join(
                        b"$" + ch + len(pkt).to_bytes(2, "big") + pkt
                        for pkt in self.packetizer.packetize(frame.nals, frame.ts90k)
                    )
                    async with self._wlock:
                        self.writer.write(blob)
                        await self.writer.drain()
                elif self.udp is not None:
                    for pkt in self.packetizer.packetize(frame.nals, frame.ts90k):
                        self.udp[0].sendto(pkt, self.udp[1])
        except ConnectionError:
            pass
        except asyncio.CancelledError:
            raise  # let _stop_play observe the cancellation cleanly
        except Exception:
            # a dead pump must not leave a silent zombie session — close the
            # writer (under the lock, preserving the single-writer invariant) so
            # the request loop's blocked read fails and the connection tears down
            log.exception("media pump for %s crashed", self.peer)
            async with self._wlock:
                self.writer.close()
        finally:
            if self.sub is not None and self.stream is not None:
                self.stream.unsubscribe(self.sub)
                self.sub = None

    def _release_transport(self) -> None:
        """Free per-SETUP transport state (closes any bound UDP socket)."""
        if self.udp is not None:
            self.udp[0].close()
            self.udp = None
        self.interleaved = None

    async def _stop_play(self) -> None:
        if self.play_task is not None:
            self.play_task.cancel()
            try:
                await self.play_task
            except asyncio.CancelledError:
                pass
            self.play_task = None

    async def close(self) -> None:
        await self._stop_play()
        self._release_transport()
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


class RTSPServer:
    def __init__(
        self,
        hub: StreamHub,
        host: str = "0.0.0.0",
        port: int = 8554,
        auth: tuple[str, str] | None = None,
        describe_timeout: float = 10.0,
        max_connections: int = MAX_CONNECTIONS,
    ) -> None:
        self.hub = hub
        self.host, self.port = host, port
        self.auth = auth
        self.describe_timeout = describe_timeout
        self.max_connections = max_connections
        self.listen_host_for_udp = "0.0.0.0"
        self._server: asyncio.base_events.Server | None = None
        self._conns: set[ClientConnection] = set()

    async def start(self) -> None:
        if self.auth is None and self.host not in ("127.0.0.1", "::1", "localhost"):
            log.warning(
                "RTSP bound to %s with NO authentication: any host that can reach "
                "port %d can view every camera stream. Set [server] username/"
                "password, or bind to 127.0.0.1, or keep the port on an isolated "
                "VLAN.", self.host, self.port,
            )
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        addrs = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        log.info("RTSP listening on %s", addrs)
        for name in self.hub.streams:
            log.info("  rtsp://<host>:%d/%s", self.port, name)

    @property
    def bound_port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def _client(self, reader, writer) -> None:
        if len(self._conns) >= self.max_connections:
            log.warning(
                "connection limit (%d) reached; refusing %s",
                self.max_connections, writer.get_extra_info("peername"),
            )
            writer.close()
            return
        conn = ClientConnection(self, reader, writer)
        self._conns.add(conn)
        log.info("client connected: %s", conn.peer)
        try:
            await conn.run()
        finally:
            self._conns.discard(conn)
        log.info("client gone: %s", conn.peer)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        # close live client connections deterministically: on Python 3.11
        # Server.wait_closed() does NOT wait for accepted connections, so
        # without this their sockets and bound UDP endpoints would leak past
        # shutdown until loop finalization
        await asyncio.gather(
            *(c.close() for c in list(self._conns)), return_exceptions=True
        )
        if self._server is not None:
            await self._server.wait_closed()
