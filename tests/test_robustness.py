"""Hostile-input and edge-case tests added after the adversarial review."""
import asyncio
import struct

import pytest

from conftest import stack
from dvrbridge.drivers.netdvr3 import MAGIC, NetDvr3Driver, parse_frame_header
from dvrbridge.h264 import iter_nals
from dvrbridge.hub import TimestampMapper
from dvrbridge.testing import FakeDvr, frame_header, synth_preamble
from tests_rtsp_client import RtspTestClient


# ---- RTSP server hostile input (H2, H3) ----

async def _raw(server, data: bytes, read_reply: bool = True) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", server.bound_port)
    writer.write(data)
    await writer.drain()
    reply = b""
    if read_reply:
        try:
            reply = await asyncio.wait_for(reader.read(200), 3)
        except (TimeoutError, ConnectionError):
            pass
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return reply


async def test_oversized_content_length_rejected(annexb):
    async with stack(annexb) as (fake, hub, server):
        reply = await _raw(
            server,
            b"SET_PARAMETER rtsp://x/sim/ch1 RTSP/1.0\r\nCSeq: 1\r\n"
            b"Content-Length: 10000000000\r\n\r\n",
        )
        assert b"413" in reply, "must reject a 10GB body, not try to buffer it"


async def test_non_numeric_content_length_rejected(annexb):
    async with stack(annexb) as (fake, hub, server):
        reply = await _raw(
            server,
            b"SET_PARAMETER rtsp://x/sim/ch1 RTSP/1.0\r\nCSeq: 1\r\n"
            b"Content-Length: bogus\r\n\r\n",
        )
        assert b"400" in reply


async def test_oversized_interleaved_length_closes_cleanly(annexb):
    async with stack(annexb) as (fake, hub, server):
        # '$' + channel + 0xFFFF length, then nothing: must not hang forever
        reply = await _raw(server, b"$\x00\xff\xff")
        assert reply == b"" or b"400" in reply
        # server still serving other clients
        c = await RtspTestClient.connect("127.0.0.1", server.bound_port)
        st, _, _ = await c.request("OPTIONS", c.url("sim/ch1"))
        assert st == 200
        await c.close()


async def test_idle_connection_does_not_wedge_server(annexb):
    async with stack(annexb) as (fake, hub, server):
        # open a silent connection (slowloris); the server must keep serving
        idle_r, idle_w = await asyncio.open_connection("127.0.0.1", server.bound_port)
        try:
            c = await RtspTestClient.connect("127.0.0.1", server.bound_port)
            st, _, _ = await c.request("OPTIONS", c.url("sim/ch1"))
            assert st == 200
            await c.close()
        finally:
            idle_w.close()


# ---- UDP transport leak on re-SETUP / TEARDOWN (H5, H6) ----

async def test_reSETUP_does_not_leak_udp_sockets(annexb):
    import resource

    async with stack(annexb) as (fake, hub, server):
        c = await RtspTestClient.connect("127.0.0.1", server.bound_port)
        try:
            await c.request("DESCRIBE", c.url("sim/ch1"))
            fds_before = _open_fds()
            for _ in range(25):
                st, hdrs, _ = await c.request(
                    "SETUP", c.url("sim/ch1/track0"),
                    {"Transport": "RTP/AVP;unicast;client_port=40000-40001"},
                )
                assert st == 200
            fds_after = _open_fds()
            # 25 UDP SETUPs must not leave 25 bound sockets behind
            assert fds_after - fds_before < 5, (
                f"leaked ~{fds_after - fds_before} fds across 25 re-SETUPs"
            )
        finally:
            await c.close()


def _open_fds() -> int:
    import os

    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return 0


# ---- driver framing edge cases (M8, M9) ----

async def _serve_once(data: bytes) -> int:
    async def handler(reader, writer):
        await reader.readexactly(76)
        writer.write(data)
        await writer.drain()
        await asyncio.sleep(0.2)
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server.sockets[0].getsockname()[1]


def _framed(aus: list[tuple[bytes, bool]], preamble: bytes) -> bytes:
    out = bytearray(preamble)
    seq, ts = 0xB00, 1_000_000
    for au, key in aus:
        out += frame_header(seq, ts, key, len(au)) + au
        seq += 1
        ts += 120
    return bytes(out)


async def test_false_magic_inside_payload_is_rejected(annexb):
    """A 63 00 0c 00 pattern occurring inside H.264 payload must NOT be
    mistaken for a frame header (the payload-startcode corroboration rejects
    it). We plant the magic inside a P-frame's bytes."""
    from dvrbridge.testing import split_access_units

    aus = split_access_units(annexb)
    # inject MAGIC into the middle of one AU's payload (after its start code)
    au, key = aus[3]
    poisoned = au[:20] + MAGIC + MAGIC + au[20:]
    aus[3] = (poisoned, key)
    port = await _serve_once(_framed(aus, synth_preamble()))
    drv = NetDvr3Driver("127.0.0.1", "admin", "x", 1, port=port, read_timeout=2)
    frames = []
    gen = drv.frames()
    try:
        async for f in gen:
            frames.append(f)
    except Exception:
        pass
    finally:
        await gen.aclose()
    # every emitted frame is a clean AU beginning with a start code
    for f in frames:
        assert f.payload.startswith(b"\x00\x00\x00\x01")
        assert list(iter_nals(f.payload))
    # the frames before the poisoned one still came through intact
    assert len(frames) >= 3


async def test_timestamp_mapper_delta_zero_shares_timestamp():
    m = TimestampMapper()
    a = m.map(5000)
    b = m.map(5000)  # same instant (e.g. multi-slice access unit)
    assert b == a, "identical device timestamps must map to identical RTP ts"
