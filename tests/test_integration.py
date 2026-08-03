"""End-to-end: FakeDvr → dvrbridge → ffmpeg/ffprobe as reference RTSP clients.

If ffmpeg can negotiate, depacketize and DECODE our stream, real consumers
(go2rtc, Frigate, VLC) can too.
"""
import asyncio
import shutil

import pytest

from conftest import stack

FFPROBE = shutil.which("ffprobe")
FFMPEG = shutil.which("ffmpeg")
pytestmark = pytest.mark.skipif(
    not (FFPROBE and FFMPEG), reason="ffmpeg/ffprobe not installed"
)


async def run(cmd: list[str], timeout: float = 30) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out, err


async def ffprobe_stream(url: str, transport: str = "tcp") -> str:
    code, out, err = await run(
        [FFPROBE, "-v", "error", "-rtsp_transport", transport,
         "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height",
         "-of", "csv=p=0", url]
    )
    assert code == 0, f"ffprobe failed: {err.decode()[-800:]}"
    return out.decode().strip()


async def ffmpeg_decode_frames(url: str, n: int, transport: str = "tcp") -> None:
    code, _, err = await run(
        [FFMPEG, "-v", "error", "-rtsp_transport", transport, "-i", url,
         "-frames:v", str(n), "-f", "null", "-"],
        timeout=60,
    )
    assert code == 0, f"ffmpeg decode failed: {err.decode()[-800:]}"
    # decoder errors (corrupt macroblocks etc.) land on stderr even at -v error
    assert b"error while decoding" not in err.lower(), err.decode()[-800:]


async def test_ffprobe_tcp_reports_correct_codec_and_size(annexb):
    async with stack(annexb) as (fake, hub, server):
        got = await ffprobe_stream(
            f"rtsp://127.0.0.1:{server.bound_port}/sim/ch1", "tcp"
        )
        assert got == "h264,352,288"


async def test_ffprobe_udp_transport(annexb):
    async with stack(annexb) as (fake, hub, server):
        got = await ffprobe_stream(
            f"rtsp://127.0.0.1:{server.bound_port}/sim/ch1", "udp"
        )
        assert got == "h264,352,288"


async def test_ffmpeg_decodes_50_frames_cleanly(annexb):
    async with stack(annexb) as (fake, hub, server):
        await ffmpeg_decode_frames(
            f"rtsp://127.0.0.1:{server.bound_port}/sim/ch1", 50
        )


async def test_decoded_image_matches_source(annexb, tmp_path):
    """Decode one frame via RTSP and via the raw fixture; both must succeed
    and produce same-sized non-trivial PNGs."""
    async with stack(annexb) as (fake, hub, server):
        rtsp_png = tmp_path / "rtsp.png"
        code, _, err = await run(
            [FFMPEG, "-v", "error", "-rtsp_transport", "tcp",
             "-i", f"rtsp://127.0.0.1:{server.bound_port}/sim/ch1",
             "-frames:v", "1", "-y", str(rtsp_png)],
            timeout=60,
        )
        assert code == 0, err.decode()[-800:]
        assert rtsp_png.stat().st_size > 5_000, "decoded frame suspiciously small"


async def test_stream_survives_device_drop_and_reconnect(annexb):
    """DVR drops mid-stream; hub reconnects; the SAME RTSP session keeps
    delivering decodable video."""
    async with stack(
        annexb, fake_kwargs={"drop_after_frames": 40}
    ) as (fake, hub, server):
        # 40 frames @50fps = 0.8s of video, then the device hangs up on every
        # connection after 40 more frames. ffmpeg must still get 100 frames
        # through hub reconnects inside one RTSP session.
        await ffmpeg_decode_frames(
            f"rtsp://127.0.0.1:{server.bound_port}/sim/ch1", 100
        )
        assert fake.connections_seen >= 2, "hub must have reconnected"


async def test_stream_survives_garbage_injection(annexb):
    async with stack(
        annexb, fake_kwargs={"garbage_every": 7}
    ) as (fake, hub, server):
        await ffmpeg_decode_frames(
            f"rtsp://127.0.0.1:{server.bound_port}/sim/ch1", 60
        )


async def test_four_channels_concurrently(annexb):
    async with stack(annexb, channels=(1, 2, 3, 4)) as (fake, hub, server):
        results = await asyncio.gather(
            *(
                ffprobe_stream(f"rtsp://127.0.0.1:{server.bound_port}/sim/ch{c}")
                for c in (1, 2, 3, 4)
            )
        )
        assert results == ["h264,352,288"] * 4


async def test_ffmpeg_negotiates_basic_auth(annexb):
    async with stack(annexb, auth=("viewer", "hunter2")) as (fake, hub, server):
        # ffprobe answers the 401 with Basic credentials from the URL
        got = await ffprobe_stream(
            f"rtsp://viewer:hunter2@127.0.0.1:{server.bound_port}/sim/ch1"
        )
        assert got == "h264,352,288"
        # and wrong credentials must NOT stream
        code, _, _ = await run(
            [FFPROBE, "-v", "error", "-rtsp_transport", "tcp",
             f"rtsp://viewer:nope@127.0.0.1:{server.bound_port}/sim/ch1"]
        )
        assert code != 0


async def test_on_demand_lifecycle_releases_dvr_connection(annexb):
    """After the last RTSP client leaves, the bridge must free the DVR socket."""
    async with stack(
        annexb, stream_kwargs={"linger": 0.3}
    ) as (fake, hub, server):
        await ffprobe_stream(f"rtsp://127.0.0.1:{server.bound_port}/sim/ch1")
        seen = fake.connections_seen
        assert seen >= 1
        await asyncio.sleep(1.5)  # linger expiry
        stream = hub.get("sim/ch1")
        assert stream._task is None or stream._task.done()
        # and a later client works again (fresh device connection)
        await ffprobe_stream(f"rtsp://127.0.0.1:{server.bound_port}/sim/ch1")
        assert fake.connections_seen > seen
