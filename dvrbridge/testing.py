"""FakeDvr: an in-process NetDvrV3 device simulator.

Speaks the same wire protocol as a real board (76-byte start-stream packet in,
preamble + 24-byte-framed H.264 out) so the driver, hub and RTSP server can be
integration-tested — and demoed — without hardware. Fault injection knobs
cover the failure modes we've seen from real boards.
"""
from __future__ import annotations

import asyncio
import struct
from dataclasses import dataclass, field

from .h264 import is_vcl, iter_nals, nal_type, NAL_IDR

def synth_preamble(
    device_id: str = "FAKEDVR000000001",
    firmware: str = "V000000",
    width: int = 352,
    height: int = 288,
) -> bytes:
    """Build a preamble with the fields the driver knows how to read."""
    pre = bytearray(184)
    pre[0:4] = struct.pack(">I", 0x14)
    pre[8:20] = bytes.fromhex("28000400030007002a000400")
    did = device_id.encode()[:16]
    pre[0x24 : 0x24 + len(did)] = did
    fw = firmware.encode()[:8]
    pre[0x54 : 0x54 + len(fw)] = fw
    pre[0x7C:0x80] = b"H264"
    struct.pack_into("<HH", pre, 0x84, width, height)
    return bytes(pre)


def split_access_units(annexb: bytes) -> list[tuple[bytes, bool]]:
    """Group an Annex-B stream into (access_unit, keyframe) tuples.

    AU = leading non-VCL NALs (SPS/PPS/SEI) + one VCL NAL, matching how the
    real device frames its stream (I-frame AUs carry SPS+PPS+SEI+IDR).
    """
    aus: list[tuple[bytes, bool]] = []
    pending: list[bytes] = []
    for nal in iter_nals(annexb):
        pending.append(nal)
        t = nal_type(nal)
        if is_vcl(t):
            au = b"".join(b"\x00\x00\x00\x01" + n for n in pending)
            aus.append((au, t == NAL_IDR))
            pending = []
    return aus


def frame_header(seq: int, ts_ms: int, keyframe: bool, payload_len: int) -> bytes:
    hdr = bytearray(24)
    struct.pack_into("<IHHIII", hdr, 0, seq & 0xFFFFFFFF, 0x0063, 0x000C, 0,
                     (seq + 0x1000) & 0xFFFFFFFF, ts_ms & 0xFFFFFFFF)
    hdr[20] = 0x64 if keyframe else 0x66
    struct.pack_into("<H", hdr, 22, (payload_len - 4) & 0xFFFF)
    return bytes(hdr)


@dataclass
class FakeDvr:
    """NetDvrV3 protocol simulator.

    Fault injection:
      drop_after_frames  close the socket abruptly after N frames
      garbage_every      inject junk bytes between frames every N frames
      max_connections    refuse (close immediately) beyond this many sockets
      reject_credentials go silent on wrong user/pass (matches real behavior:
                         the board never answers a bad start packet)
    """

    annexb: bytes
    username: str = "admin"
    password: str = "secret"
    fps: float = 25.0
    channels: int = 4
    preamble: bytes = field(default_factory=synth_preamble)
    loop_stream: bool = True
    drop_after_frames: int | None = None
    garbage_every: int | None = None
    garbage: bytes = b"\xde\xad\xbe\xef" * 8
    max_connections: int = 4
    reject_credentials: bool = True

    def __post_init__(self) -> None:
        self.aus = split_access_units(self.annexb)
        if not self.aus:
            raise ValueError("fixture contains no access units")
        self._server: asyncio.Server | None = None
        self._active = 0
        self.connections_seen = 0
        self.channels_requested: list[int] = []

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self._server = await asyncio.start_server(self._client, host, port)
        return self._server.sockets[0].getsockname()[1]

    @property
    def port(self) -> int:
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections_seen += 1
        if self._active >= self.max_connections:
            writer.close()
            return
        self._active += 1
        try:
            await self._session(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            self._active -= 1
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _session(self, reader, writer) -> None:
        try:
            start = await asyncio.wait_for(reader.readexactly(76), 10)
        except TimeoutError:
            return
        if start[:4] != struct.pack(">I", 0x48):
            return  # not our protocol: hang up
        user = start[20:52].split(b"\x00")[0].decode("latin-1")
        pw = start[52:72].split(b"\x00")[0].decode("latin-1")
        channel = start[73]
        self.channels_requested.append(channel)
        if self.reject_credentials and (user != self.username or pw != self.password):
            # real boards go silent on bad credentials — mimic that, but do not
            # block the test forever: wait for the client to give up
            await reader.read(1)
            return
        if channel >= self.channels:
            await reader.read(1)
            return

        writer.write(self.preamble)
        seq = 0x0B00
        ts = 47_000_000  # arbitrary epoch, like the real board
        interval = 1.0 / self.fps
        sent = 0
        while True:
            for au, key in self.aus:
                writer.write(frame_header(seq, ts, key, len(au)) + au)
                sent += 1
                seq += 1
                ts += int(1000 * interval)
                if self.garbage_every and sent % self.garbage_every == 0:
                    writer.write(self.garbage)
                if self.drop_after_frames is not None and sent >= self.drop_after_frames:
                    await writer.drain()
                    return  # abrupt close
                await writer.drain()
                await asyncio.sleep(interval)
            if not self.loop_stream:
                return
