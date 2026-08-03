from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from dvrbridge.hub import Stream, StreamHub
from dvrbridge.drivers.netdvr3 import NetDvr3Driver
from dvrbridge.rtsp import RTSPServer
from dvrbridge.testing import FakeDvr

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def annexb() -> bytes:
    return (FIXTURES / "test.h264").read_bytes()


@pytest.fixture(scope="session")
def real_preamble() -> bytes:
    return (FIXTURES / "preamble.bin").read_bytes()


@pytest.fixture(scope="session")
def raw_capture() -> bytes:
    """First 300KB of a real live-DVR session (preamble + framed H.264)."""
    return (FIXTURES / "raw_capture.bin").read_bytes()


@contextlib.asynccontextmanager
async def stack(
    annexb: bytes,
    fake_kwargs: dict | None = None,
    stream_kwargs: dict | None = None,
    auth: tuple[str, str] | None = None,
    channels: tuple[int, ...] = (1,),
    server_kwargs: dict | None = None,
):
    """FakeDvr → hub → RTSP server, all on ephemeral ports."""
    fake = FakeDvr(annexb=annexb, fps=50.0, **(fake_kwargs or {}))
    dvr_port = await fake.start()
    hub = StreamHub()
    for ch in channels:
        def factory(_ch=ch):
            return NetDvr3Driver(
                "127.0.0.1", fake.username, fake.password, _ch,
                port=dvr_port, connect_timeout=2, read_timeout=5,
            )
        hub.add(Stream(f"sim/ch{ch}", factory, **(stream_kwargs or {"linger": 1.0})))
    server = RTSPServer(hub, "127.0.0.1", 0, auth=auth, describe_timeout=8,
                        **(server_kwargs or {}))
    await server.start()
    try:
        yield fake, hub, server
    finally:
        await server.stop()
        await hub.stop()
        await fake.stop()
        await asyncio.sleep(0)
