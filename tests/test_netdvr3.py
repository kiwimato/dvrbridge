"""NetDvrV3 driver: wire format, framing parser, resync, fault handling.

The crown-jewel test here replays a REAL captured session (fixtures/
raw_capture.bin, recorded from the live board) through the driver and checks
that every emitted frame is a clean access unit with sane timestamps.
"""
import asyncio
import struct

import pytest

from dvrbridge.drivers.base import DriverError
from dvrbridge.drivers.netdvr3 import (
    FRAME_I,
    NetDvr3Driver,
    build_start_stream,
    parse_frame_header,
)
from dvrbridge.h264 import iter_nals, nal_type
from dvrbridge.testing import FakeDvr, frame_header


class TestStartStreamPacket:
    def test_byte_exact_layout(self):
        pkt = build_start_stream("admin", "password", 1)
        assert len(pkt) == 76
        assert pkt[:4] == b"\x00\x00\x00\x48"
        assert pkt[8:20] == bytes.fromhex("280004000500000029003800")
        assert pkt[20:28] == b"admin\x00\x00\x00"
        assert pkt[52:60] == b"password"
        assert pkt[72] == 0 and pkt[73] == 0

    @pytest.mark.parametrize("channel,wire", [(1, 0), (2, 1), (4, 3)])
    def test_channel_is_zero_indexed_on_wire(self, channel, wire):
        assert build_start_stream("a", "b", channel)[73] == wire

    def test_substream_flag(self):
        assert build_start_stream("a", "b", 1, substream=True)[72] == 1

    def test_channel_range(self):
        with pytest.raises(ValueError):
            build_start_stream("a", "b", 0)


class TestFrameHeader:
    def test_round_trip_with_simulator_builder(self):
        hdr = frame_header(seq=0xB7F, ts_ms=1_234_567, keyframe=True, payload_len=8335)
        seq, ts, ftype, plen = parse_frame_header(hdr)
        assert (seq, ts, ftype, plen) == (0xB7F, 1_234_567, FRAME_I, 8335)

    def test_rejects_bad_magic(self):
        hdr = bytearray(frame_header(1, 2, False, 100))
        hdr[4] = 0x99
        with pytest.raises(DriverError):
            parse_frame_header(bytes(hdr))


async def _serve_bytes(data: bytes, chunk: int = 4096, delay: float = 0.0) -> int:
    """One-shot TCP server that dumps `data` after receiving the start packet."""

    async def handler(reader, writer):
        await reader.readexactly(76)
        for i in range(0, len(data), chunk):
            writer.write(data[i : i + chunk])
            await writer.drain()
            if delay:
                await asyncio.sleep(delay)
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server.sockets[0].getsockname()[1]


async def _collect(driver: NetDvr3Driver, max_frames: int = 10_000):
    frames = []
    gen = driver.frames()
    try:
        async for f in gen:
            frames.append(f)
            if len(frames) >= max_frames:
                break
    except DriverError:
        pass  # server closed at end of data
    finally:
        await gen.aclose()
    return frames


class TestDriverAgainstRealCapture:
    @pytest.mark.parametrize("chunk", [17, 1400, 65536])
    async def test_parses_real_session(self, raw_capture, chunk):
        port = await _serve_bytes(raw_capture, chunk=chunk)
        drv = NetDvr3Driver("127.0.0.1", "admin", "x", 1, port=port, read_timeout=3)
        frames = await _collect(drv)
        assert len(frames) > 50
        assert drv.resyncs == 0, "real capture must parse without resync"
        # metadata from the real preamble
        assert drv.info.device_id == "DVRBRIDGE0000000"
        assert drv.info.firmware == "V111126"
        assert (drv.info.width, drv.info.height) == (352, 288)
        # every frame is a clean access unit
        for f in frames:
            assert f.payload.startswith(b"\x00\x00\x00\x01")
            assert list(iter_nals(f.payload)), "AU must contain NALs"
        # keyframes carry SPS+PPS+IDR
        keys = [f for f in frames if f.keyframe]
        assert keys
        ktypes = {nal_type(n) for n in iter_nals(keys[0].payload)}
        assert {5, 7, 8} <= ktypes
        # timestamps are milliseconds, ~120ms apart on this board
        deltas = [b.ts_ms - a.ts_ms for a, b in zip(frames, frames[1:])]
        assert all(0 < d < 1000 for d in deltas)
        assert 80 <= sum(deltas) / len(deltas) <= 200

    async def test_slow_trickle(self, raw_capture):
        port = await _serve_bytes(raw_capture[:60_000], chunk=999, delay=0.01)
        drv = NetDvr3Driver("127.0.0.1", "admin", "x", 1, port=port, read_timeout=3)
        frames = await _collect(drv)
        assert len(frames) > 5


class TestDriverAgainstSimulator:
    async def test_streams_and_recovers_from_garbage(self, annexb):
        fake = FakeDvr(annexb=annexb, fps=200, garbage_every=5)
        port = await fake.start()
        drv = NetDvr3Driver("127.0.0.1", "admin", "secret", 1, port=port)
        frames = await _collect(drv, max_frames=40)
        await fake.stop()
        assert len(frames) == 40
        assert drv.resyncs > 0, "garbage injection must trigger resyncs"
        for f in frames:
            assert f.payload.startswith(b"\x00\x00\x00\x01")

    async def test_wrong_credentials_time_out(self, annexb):
        fake = FakeDvr(annexb=annexb)
        port = await fake.start()
        drv = NetDvr3Driver(
            "127.0.0.1", "admin", "WRONG", 1, port=port, read_timeout=0.5
        )
        gen = drv.frames()
        with pytest.raises(DriverError):
            await anext(gen)
        await gen.aclose()
        await fake.stop()

    async def test_abrupt_drop_raises(self, annexb):
        fake = FakeDvr(annexb=annexb, fps=500, drop_after_frames=12)
        port = await fake.start()
        drv = NetDvr3Driver("127.0.0.1", "admin", "secret", 1, port=port)
        frames = await _collect(drv)
        await fake.stop()
        assert 1 <= len(frames) <= 12

    async def test_connection_refused(self):
        drv = NetDvr3Driver("127.0.0.1", "a", "b", 1, port=1, connect_timeout=1)
        gen = drv.frames()
        with pytest.raises(DriverError):
            await anext(gen)
        await gen.aclose()
