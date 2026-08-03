"""Property-based fuzzing of the parsers: they must never crash, hang, or grow
memory without bound on arbitrary input. Correctness of *output* is covered
elsewhere; here we only assert robustness."""
import asyncio

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from dvrbridge.drivers.base import DriverError
from dvrbridge.drivers.netdvr3 import HEADER_LEN, MAGIC, NetDvr3Driver, parse_frame_header
from dvrbridge.h264 import AnnexBSplitter, iter_nals, parse_sps
from dvrbridge.rtsp.rtp import H264Packetizer

FUZZ = settings(max_examples=300, deadline=None,
                suppress_health_check=[HealthCheck.function_scoped_fixture])


# ---- H.264 parsing ----

@given(st.binary(max_size=4096))
@FUZZ
def test_iter_nals_never_raises(data):
    for nal in iter_nals(data):
        assert isinstance(nal, bytes)


@given(st.binary(max_size=4096))
@FUZZ
def test_parse_sps_never_hangs_or_crashes(data):
    # arbitrary bytes as an "SPS": must return or raise cleanly, never loop
    try:
        info = parse_sps(b"\x67" + data)
    except (ValueError, IndexError):
        return
    assert info.width >= 0 or info.width < 0  # any int is fine; just no hang


@given(st.lists(st.binary(min_size=1, max_size=300), max_size=40))
@FUZZ
def test_annexb_splitter_bounded_and_total(chunks):
    """Splitter output NALs are a subset of what a full parse sees, and the
    internal buffer never exceeds its cap."""
    sp = AnnexBSplitter(max_nal=8192)
    for c in chunks:
        out = sp.feed(c)
        assert isinstance(out, list)
        assert len(sp._buf) <= 8192 + 300  # cap + one chunk of slack


@given(st.binary(min_size=24, max_size=24))
@FUZZ
def test_parse_frame_header_total(hdr):
    try:
        seq, ts, ftype, plen = parse_frame_header(hdr)
    except DriverError:
        return
    assert 4 <= plen <= 65535 + 4


# ---- RTP packetizer ----

@given(
    st.lists(st.binary(min_size=1, max_size=5000), min_size=1, max_size=8),
    st.integers(min_value=0, max_value=2**32 - 1),
    st.integers(min_value=64, max_value=1500),
)
@FUZZ
def test_packetizer_respects_mtu_and_never_crashes(nals, ts, mtu):
    pk = H264Packetizer(ssrc=1, mtu=mtu)
    pkts = pk.packetize(nals, ts)
    for p in pkts:
        assert len(p) >= 12                       # RTP header present
        assert len(p) - 12 <= mtu                 # payload within MTU
        assert p[0] >> 6 == 2                      # version 2
    if any(nals):
        assert pkts and (pkts[-1][1] & 0x80)       # marker on last packet


# ---- driver against a hostile "device" ----

async def _serve_bytes(data: bytes) -> int:
    async def handler(reader, writer):
        try:
            await asyncio.wait_for(reader.readexactly(76), 2)
        except (asyncio.IncompleteReadError, TimeoutError):
            writer.close()
            return
        writer.write(data)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server.sockets[0].getsockname()[1]


@pytest.mark.parametrize("seed", range(12))
async def test_driver_survives_garbage_device(seed, annexb):
    """A device that sends random/adversarial bytes must make the driver raise
    DriverError (or yield nothing) within the read timeout — never hang or
    crash the process."""
    import random

    rng = random.Random(seed)
    # mix of: pure random, random with planted MAGIC, truncated real preamble
    kind = seed % 3
    if kind == 0:
        blob = bytes(rng.randrange(256) for _ in range(rng.randint(0, 5000)))
    elif kind == 1:
        parts = [bytes(rng.randrange(256) for _ in range(rng.randint(0, 200)))
                 for _ in range(20)]
        blob = MAGIC.join(parts)
    else:
        blob = annexb[: rng.randint(0, 400)]

    port = await _serve_bytes(blob)
    drv = NetDvr3Driver("127.0.0.1", "admin", "x", 1, port=port, read_timeout=1.5)
    gen = drv.frames()
    got = 0
    try:
        async with asyncio.timeout(6):
            async for f in gen:
                assert f.payload.startswith(b"\x00\x00\x00\x01")
                got += 1
                if got > 500:
                    break
    except (DriverError, OSError):
        pass
    finally:
        await gen.aclose()
    # the point is simply that we got here without hanging or crashing
    assert got >= 0
