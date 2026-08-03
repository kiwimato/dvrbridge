"""RTSP server protocol tests with a hand-rolled client (no ffmpeg needed)."""
import asyncio
import base64

import pytest

from conftest import stack
from tests_rtsp_client import RtspTestClient


async def test_options_describe_setup_play_teardown(annexb):
    async with stack(annexb) as (fake, hub, server):
        c = await RtspTestClient.connect("127.0.0.1", server.bound_port)
        try:
            st, hdrs, _ = await c.request("OPTIONS", c.url("sim/ch1"))
            assert st == 200
            assert "DESCRIBE" in hdrs["public"] and "SETUP" in hdrs["public"]

            st, hdrs, body = await c.request("DESCRIBE", c.url("sim/ch1"))
            assert st == 200
            sdp = body.decode()
            assert "m=video" in sdp
            assert "H264/90000" in sdp
            assert "sprop-parameter-sets=" in sdp
            assert "packetization-mode=1" in sdp

            st, hdrs, _ = await c.request(
                "SETUP",
                c.url("sim/ch1/track0"),
                {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
            )
            assert st == 200
            assert "interleaved=0-1" in hdrs["transport"]
            session = hdrs["session"].split(";")[0]
            assert session

            st, hdrs, _ = await c.request(
                "PLAY", c.url("sim/ch1"), {"Session": session}
            )
            assert st == 200

            pkts = await c.read_rtp_packets(20, timeout=5)
            assert len(pkts) == 20
            # all on channel 0, valid RTP v2
            for ch, pkt in pkts:
                assert ch == 0
                assert pkt[0] >> 6 == 2

            # keepalive works while streaming (demuxed from RTP data)
            st, hdrs, _ = await c.request(
                "GET_PARAMETER", c.url("sim/ch1"), {"Session": session}
            )
            assert st == 200

            st, hdrs, _ = await c.request(
                "TEARDOWN", c.url("sim/ch1"), {"Session": session}
            )
            assert st == 200
        finally:
            await c.close()


async def test_first_rtp_frame_is_keyframe_with_sps(annexb):
    from dvrbridge.h264 import nal_type
    from test_rtp import depacketize

    async with stack(annexb) as (fake, hub, server):
        c = await RtspTestClient.connect("127.0.0.1", server.bound_port)
        try:
            await c.request("DESCRIBE", c.url("sim/ch1"))
            _, hdrs, _ = await c.request(
                "SETUP", c.url("sim/ch1/track0"),
                {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
            )
            session = hdrs["session"].split(";")[0]
            await c.request("PLAY", c.url("sim/ch1"), {"Session": session})
            pkts = [p for _, p in await c.read_rtp_packets(6, timeout=5)]
            nals = depacketize(pkts[:6])
            types = [nal_type(n) for n in nals]
            assert types[0] == 7, f"stream must start with SPS, got {types}"
            assert 8 in types and 5 in types
        finally:
            await c.close()


async def test_describe_unknown_stream_404(annexb):
    async with stack(annexb) as (fake, hub, server):
        c = await RtspTestClient.connect("127.0.0.1", server.bound_port)
        try:
            st, _, _ = await c.request("DESCRIBE", c.url("nope/ch9"))
            assert st == 404
        finally:
            await c.close()


async def test_basic_auth(annexb):
    async with stack(annexb, auth=("viewer", "hunter2")) as (fake, hub, server):
        c = await RtspTestClient.connect("127.0.0.1", server.bound_port)
        try:
            st, hdrs, _ = await c.request("DESCRIBE", c.url("sim/ch1"))
            assert st == 401
            assert "basic" in hdrs.get("www-authenticate", "").lower()
            token = base64.b64encode(b"viewer:hunter2").decode()
            st, _, _ = await c.request(
                "DESCRIBE", c.url("sim/ch1"), {"Authorization": f"Basic {token}"}
            )
            assert st == 200
            # wrong password
            bad = base64.b64encode(b"viewer:nope").decode()
            st, _, _ = await c.request(
                "DESCRIBE", c.url("sim/ch1"), {"Authorization": f"Basic {bad}"}
            )
            assert st == 401
        finally:
            await c.close()


async def test_udp_setup_delivers_rtp(annexb):
    async with stack(annexb) as (fake, hub, server):
        c = await RtspTestClient.connect("127.0.0.1", server.bound_port)
        udp_sock = None
        try:
            # open a local UDP socket to receive RTP
            loop = asyncio.get_running_loop()
            received: asyncio.Queue = asyncio.Queue()

            class Sink(asyncio.DatagramProtocol):
                def datagram_received(self, data, addr):
                    received.put_nowait(data)

            udp_sock, _ = await loop.create_datagram_endpoint(
                Sink, local_addr=("127.0.0.1", 0)
            )
            rtp_port = udp_sock.get_extra_info("sockname")[1]

            await c.request("DESCRIBE", c.url("sim/ch1"))
            st, hdrs, _ = await c.request(
                "SETUP", c.url("sim/ch1/track0"),
                {"Transport": f"RTP/AVP;unicast;client_port={rtp_port}-{rtp_port+1}"},
            )
            assert st == 200
            assert "server_port=" in hdrs["transport"]
            session = hdrs["session"].split(";")[0]
            st, _, _ = await c.request("PLAY", c.url("sim/ch1"), {"Session": session})
            assert st == 200
            pkt = await asyncio.wait_for(received.get(), 5)
            assert pkt[0] >> 6 == 2, "valid RTP over UDP"
        finally:
            if udp_sock:
                udp_sock.close()
            await c.close()


async def test_too_many_headers_rejected(annexb):
    """An endless stream of distinct headers must be capped, not OOM the server."""
    async with stack(annexb) as (fake, hub, server):
        r, w = await asyncio.open_connection("127.0.0.1", server.bound_port)
        try:
            w.write(b"OPTIONS rtsp://x/ RTSP/1.0\r\n")
            for i in range(500):  # well past MAX_HEADERS
                w.write(f"X-Pad-{i}: {'a' * 40}\r\n".encode())
            w.write(b"\r\n")
            await w.drain()
            status = await asyncio.wait_for(r.readline(), 5)
            assert b"431" in status, f"expected 431, got {status!r}"
        finally:
            w.close()


async def test_slowloris_partial_request_times_out(annexb):
    """A client that sends a partial request and then stalls must be dropped
    within REQUEST_TIMEOUT rather than pinning the connection open forever."""
    from dvrbridge.rtsp import server as srv

    async with stack(annexb, server_kwargs={}) as (fake, hub, server):
        # shrink the deadline so the test is fast
        orig = srv.REQUEST_TIMEOUT
        srv.REQUEST_TIMEOUT = 0.5
        try:
            r, w = await asyncio.open_connection("127.0.0.1", server.bound_port)
            w.write(b"OPTIONS rtsp://x/ RTSP/1.0\r\n")  # first byte arrives...
            await w.drain()                              # ...then we stall (no \r\n)
            # the server must close the socket (EOF) once the deadline elapses
            data = await asyncio.wait_for(r.read(), 3)
            assert data == b"" or b"400" in data or b"Bad Request" in data
            w.close()
        finally:
            srv.REQUEST_TIMEOUT = orig


async def test_connection_limit_refuses_excess(annexb):
    async with stack(annexb, server_kwargs={"max_connections": 2}) as (f, hub, server):
        conns = []
        try:
            # fill the two allowed slots and keep them open
            for _ in range(2):
                r, w = await asyncio.open_connection("127.0.0.1", server.bound_port)
                conns.append((r, w))
            await asyncio.sleep(0.1)
            # the third connection must be refused (server closes it -> EOF)
            r3, w3 = await asyncio.open_connection("127.0.0.1", server.bound_port)
            conns.append((r3, w3))
            data = await asyncio.wait_for(r3.read(), 3)
            assert data == b"", "over-limit connection must be closed immediately"
        finally:
            for _, w in conns:
                w.close()


async def test_setup_rejects_out_of_range_interleaved(annexb):
    async with stack(annexb) as (fake, hub, server):
        c = await RtspTestClient.connect("127.0.0.1", server.bound_port)
        try:
            await c.request("DESCRIBE", c.url("sim/ch1"))
            st, _, _ = await c.request(
                "SETUP", c.url("sim/ch1/track0"),
                {"Transport": "RTP/AVP/TCP;unicast;interleaved=999-1000"},
            )
            assert st == 400
            # garbage (non-numeric) interleaved value is also a clean 400
            st, _, _ = await c.request(
                "SETUP", c.url("sim/ch1/track0"),
                {"Transport": "RTP/AVP/TCP;unicast;interleaved=abc"},
            )
            assert st == 400
        finally:
            await c.close()


async def test_two_rtsp_clients_share_one_dvr_connection(annexb):
    async with stack(annexb) as (fake, hub, server):
        clients = []
        try:
            for _ in range(2):
                c = await RtspTestClient.connect("127.0.0.1", server.bound_port)
                await c.request("DESCRIBE", c.url("sim/ch1"))
                _, hdrs, _ = await c.request(
                    "SETUP", c.url("sim/ch1/track0"),
                    {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
                )
                await c.request(
                    "PLAY", c.url("sim/ch1"),
                    {"Session": hdrs["session"].split(";")[0]},
                )
                clients.append(c)
            for c in clients:
                pkts = await c.read_rtp_packets(5, timeout=5)
                assert len(pkts) == 5
            assert fake.connections_seen == 1, (
                "hub must multiplex RTSP clients over ONE device connection"
            )
        finally:
            for c in clients:
                await c.close()
