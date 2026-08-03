"""StreamHub: fan-out, keyframe gating, slow clients, timestamp mapping."""
import asyncio

import pytest

from dvrbridge.drivers.base import Driver, VideoFrame
from dvrbridge.hub import Stream, StreamHub, TimestampMapper

SPS = bytes.fromhex("6742001495a8582590")
PPS = bytes.fromhex("68ce3c80")


def au(*nals: bytes) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


KEY_AU = au(SPS, PPS, b"\x65" + b"k" * 50)
P_AU = au(b"\x41" + b"p" * 30)


class StubDriver(Driver):
    """Emits a fixed GOP pattern forever, 1ms apart."""

    name = "stub"

    def __init__(self, gop: int = 5, interval: float = 0.001) -> None:
        super().__init__()
        self.gop, self.interval = gop, interval

    async def frames(self):
        ts = 0
        n = 0
        while True:
            key = n % self.gop == 0
            yield VideoFrame(KEY_AU if key else P_AU, ts_ms=ts, keyframe=key)
            ts += 40
            n += 1
            await asyncio.sleep(self.interval)


class TestTimestampMapper:
    def test_uses_device_deltas(self):
        m = TimestampMapper()
        t0 = m.map(1000)
        t1 = m.map(1120)
        assert t1 - t0 == 120 * 90

    def test_monotonic_despite_clock_jump(self):
        m = TimestampMapper()
        m.map(1000)
        m.map(1100)  # typical delta learned: ~100ms
        before = m.map(1200)
        after = m.map(50)  # device reconnected, clock reset backwards
        assert after > before
        assert (after - before) < 5000 * 90

    def test_wraparound_of_device_clock(self):
        m = TimestampMapper()
        a = m.map(0xFFFFFFFF - 50)
        b = m.map(49)  # wraps: delta = 100ms modulo 2^32
        assert b - a == 100 * 90

    def test_none_timestamps_fall_back_to_wallclock(self):
        m = TimestampMapper()
        a = m.map(None)
        b = m.map(None)
        assert b >= a


class TestStream:
    async def test_fanout_and_keyframe_gating(self):
        stream = Stream("s", StubDriver, linger=0.5)
        sub1 = stream.subscribe()
        first = await asyncio.wait_for(sub1.queue.get(), 2)
        assert first.keyframe, "first delivered frame must be a keyframe"
        # a second subscriber joining mid-GOP also starts at a keyframe
        sub2 = stream.subscribe()
        first2 = await asyncio.wait_for(sub2.queue.get(), 2)
        assert first2.keyframe
        # both keep receiving
        await asyncio.wait_for(sub1.queue.get(), 2)
        await asyncio.wait_for(sub2.queue.get(), 2)
        stream.unsubscribe(sub1)
        stream.unsubscribe(sub2)
        await stream.stop()

    async def test_ready_exposes_sps_pps(self):
        stream = Stream("s", StubDriver, linger=0.5)
        await stream.wait_ready(2)
        assert stream.sps == SPS and stream.pps == PPS
        await stream.stop()

    async def test_slow_subscriber_drops_to_next_keyframe(self):
        stream = Stream("s", lambda: StubDriver(interval=0.0005), linger=0.5)
        sub = stream.subscribe()
        await asyncio.sleep(0.5)  # never drain: queue (64) must overflow
        assert sub.dropped > 0
        # drain everything buffered, then the next fresh frame is a keyframe
        while not sub.queue.empty():
            sub.queue.get_nowait()
        nxt = await asyncio.wait_for(sub.queue.get(), 2)
        assert nxt.keyframe
        stream.unsubscribe(sub)
        await stream.stop()

    async def test_idle_disconnect_after_linger(self):
        stream = Stream("s", StubDriver, linger=0.2)
        sub = stream.subscribe()
        await asyncio.wait_for(sub.queue.get(), 2)
        stream.unsubscribe(sub)
        await asyncio.sleep(1.0)
        assert stream._task is None or stream._task.done(), (
            "stream must stop after linger with no subscribers"
        )
        # resubscribing revives it
        sub2 = stream.subscribe()
        frame = await asyncio.wait_for(sub2.queue.get(), 2)
        assert frame.keyframe
        stream.unsubscribe(sub2)
        await stream.stop()

    async def test_keyframe_without_sps_gets_parameter_sets_prepended(self):
        class NoSpsOnKey(StubDriver):
            async def frames(self):
                yield VideoFrame(KEY_AU, ts_ms=0, keyframe=True)  # teaches SPS/PPS
                while True:
                    await asyncio.sleep(0.001)
                    yield VideoFrame(au(b"\x65" + b"K" * 10), ts_ms=40, keyframe=True)

        stream = Stream("s", NoSpsOnKey, linger=0.5)
        sub = stream.subscribe()
        await asyncio.wait_for(sub.queue.get(), 2)
        second = await asyncio.wait_for(sub.queue.get(), 2)
        assert second.nals[0] == SPS and second.nals[1] == PPS
        stream.unsubscribe(sub)
        await stream.stop()


class TestHub:
    async def test_duplicate_names_rejected(self):
        hub = StreamHub()
        hub.add(Stream("a", StubDriver))
        with pytest.raises(ValueError):
            hub.add(Stream("a", StubDriver))
        await hub.stop()
