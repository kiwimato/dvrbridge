"""Regression tests for the concurrency review (subscribe/backoff race,
deterministic shutdown, pump crash handling)."""
import asyncio
import time

import pytest

from conftest import stack
from dvrbridge.drivers.base import Driver, DriverError, VideoFrame
from dvrbridge.hub import Stream

SPS = bytes.fromhex("6742001495a8582590")
PPS = bytes.fromhex("68ce3c80")
KEY_AU = b"".join(b"\x00\x00\x00\x01" + n for n in (SPS, PPS, b"\x65" + b"k" * 40))


class FlakyDriver(Driver):
    """Fails immediately the first N connects, then streams — to exercise the
    reconnect backoff path."""

    name = "flaky"
    attempts = 0

    def __init__(self, fail_times: int):
        super().__init__()
        self.fail_times = fail_times

    async def frames(self):
        FlakyDriver.attempts += 1
        if FlakyDriver.attempts <= self.fail_times:
            raise DriverError("simulated connect failure")
        while True:
            yield VideoFrame(KEY_AU, ts_ms=0, keyframe=True)
            await asyncio.sleep(0.005)


async def test_new_subscriber_cuts_reconnect_backoff_short():
    """A subscriber that arrives while _run is parked in backoff must be served
    quickly, not after the full (up to 15s) delay."""
    FlakyDriver.attempts = 0
    # first connect fails, forcing a backoff wait before the second attempt
    stream = Stream("s", lambda: FlakyDriver(fail_times=1), linger=5.0)
    stream.touch()  # kick off _run; it will fail once and enter backoff
    await asyncio.sleep(0.3)  # let the first attempt fail and enter backoff
    # now subscribe; without the wake, this would wait out the ~1s backoff
    sub = stream.subscribe()
    t0 = time.monotonic()
    frame = await asyncio.wait_for(sub.queue.get(), 2)
    elapsed = time.monotonic() - t0
    assert frame.keyframe
    assert elapsed < 0.9, f"backoff not interrupted (waited {elapsed:.2f}s)"
    stream.unsubscribe(sub)
    await stream.stop()


async def test_stream_restarts_after_idle_when_new_subscriber_arrives():
    """After an idle shutdown, a fresh subscriber must revive the stream and
    receive frames (no permanent deadlock)."""
    FlakyDriver.attempts = 0
    stream = Stream("s", lambda: FlakyDriver(fail_times=0), linger=0.2)
    sub = stream.subscribe()
    await asyncio.wait_for(sub.queue.get(), 2)
    stream.unsubscribe(sub)
    await asyncio.sleep(0.6)  # exceed linger → _run exits
    assert stream._task is None or stream._task.done()
    # revive
    sub2 = stream.subscribe()
    frame = await asyncio.wait_for(sub2.queue.get(), 2)
    assert frame.keyframe
    stream.unsubscribe(sub2)
    await stream.stop()


async def test_server_stop_closes_live_client_connections(annexb):
    """server.stop() must deterministically tear down in-flight clients (and
    their bound UDP endpoints), not leave them for loop finalization."""
    from tests_rtsp_client import RtspTestClient

    fds_before = _open_fds()
    async with stack(annexb) as (fake, hub, server):
        clients = []
        for _ in range(3):
            c = await RtspTestClient.connect("127.0.0.1", server.bound_port)
            await c.request("DESCRIBE", c.url("sim/ch1"))
            _, hdrs, _ = await c.request(
                "SETUP", c.url("sim/ch1/track0"),
                {"Transport": "RTP/AVP;unicast;client_port=41000-41001"},
            )
            await c.request("PLAY", c.url("sim/ch1"),
                            {"Session": hdrs["session"].split(";")[0]})
            clients.append(c)
        assert len(server._conns) == 3
        # stack() exits here → server.stop() + hub.stop()
    # after teardown, no lingering server-side connection objects
    assert len(server._conns) == 0
    for c in clients:
        await c.close()
    await asyncio.sleep(0.2)
    fds_after = _open_fds()
    assert fds_after - fds_before < 6, f"leaked ~{fds_after - fds_before} fds"


def _open_fds() -> int:
    import os
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return 0


async def test_pump_crash_tears_down_without_hanging(annexb, monkeypatch):
    """If the packetizer raises, the pump must close the connection instead of
    silently zombie-ing; the client's socket should drop."""
    from tests_rtsp_client import RtspTestClient
    import dvrbridge.rtsp.server as srv

    async with stack(annexb) as (fake, hub, server):
        c = await RtspTestClient.connect("127.0.0.1", server.bound_port)
        await c.request("DESCRIBE", c.url("sim/ch1"))
        _, hdrs, _ = await c.request(
            "SETUP", c.url("sim/ch1/track0"),
            {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
        )
        # make packetize blow up on the next access unit
        def boom(*a, **k):
            raise RuntimeError("synthetic packetizer failure")
        monkeypatch.setattr(srv.H264Packetizer, "packetize", boom)
        await c.request("PLAY", c.url("sim/ch1"),
                        {"Session": hdrs["session"].split(";")[0]})
        # the connection must close (read returns EOF) rather than hang forever
        data = await asyncio.wait_for(c.reader.read(4096), 5)
        assert data == b"", "connection should have been torn down after pump crash"
        await c.close()
