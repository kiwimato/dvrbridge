"""NetDvrV3 / MEye-family driver (generic OEM XMEye-style boards, port 8888).

Protocol (reverse-engineered from packet captures + live device, firmware
V111126 — see docs/DESIGN.md for the full spec):

  1. TCP connect, send ONE 76-byte start-stream packet. Credentials are
     embedded; there is NO login/challenge exchange on the video path.
  2. Device sends a preamble (device id, firmware, codec fourcc, WxH), then
  3. frames: a 24-byte little-endian header followed by one Annex-B access
     unit. Header: seq_a u32 | magic 0x0063 u16 | 0x000c u16 | 0 u32 |
     seq_b u32 | timestamp-ms u32 | frame-type u8 ('d'=I, 'f'=P) | 0 u8 |
     payload_len u16 (= AU size minus the 4-byte leading start code).

The u16 length caps at 64 KiB so it is treated as a prediction: the parser
verifies the magic at the predicted boundary and otherwise scans forward to
resynchronize (also recovers from mid-stream corruption).
"""
from __future__ import annotations

import asyncio
import logging
import struct
from typing import AsyncIterator

from ..h264 import NAL_IDR, iter_nals, nal_type
from .base import Driver, DriverError, VideoFrame

log = logging.getLogger("dvrbridge.netdvr3")

MAGIC = b"\x63\x00\x0c\x00"  # at header offset +4
HEADER_LEN = 24
FRAME_I = 0x64  # 'd'
FRAME_P = 0x66  # 'f'
_MAX_PREAMBLE = 64 * 1024
_MAX_FRAME = 4 * 1024 * 1024


def build_start_stream(user: str, password: str, channel: int, substream: bool = False) -> bytes:
    """76-byte start-stream packet; `channel` is 1-based here, 0-indexed on the wire."""
    if not 1 <= channel <= 256:
        raise ValueError(f"channel out of range: {channel}")
    buf = bytearray(76)
    struct.pack_into(">I", buf, 0, 0x48)
    buf[8:20] = bytes.fromhex("280004000500000029003800")
    u = user.encode()[:31]
    buf[20 : 20 + len(u)] = u
    p = password.encode()[:23]
    buf[52 : 52 + len(p)] = p
    if substream:
        buf[72] = 0x01
    buf[73] = (channel - 1) & 0xFF
    return bytes(buf)


def parse_frame_header(hdr: bytes) -> tuple[int, int, int, int]:
    """Return (seq, ts_ms, frame_type, payload_len_incl_startcode)."""
    seq_a, magic, const, _zero, _seq_b, ts_ms = struct.unpack_from("<IHHIII", hdr, 0)
    ftype = hdr[20]
    payload_len = struct.unpack_from("<H", hdr, 22)[0]
    if magic != 0x0063 or const != 0x000C:
        raise DriverError("bad frame header magic")
    return seq_a, ts_ms, ftype, payload_len + 4


class NetDvr3Driver(Driver):
    name = "netdvr3"

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        channel: int,
        port: int = 8888,
        substream: bool = False,
        connect_timeout: float = 5.0,
        read_timeout: float = 15.0,
    ) -> None:
        super().__init__()
        self.host, self.port = host, port
        self.username, self.password = username, password
        self.channel, self.substream = channel, substream
        self.connect_timeout, self.read_timeout = connect_timeout, read_timeout
        self.resyncs = 0

    def __repr__(self) -> str:  # for hub logs
        return f"netdvr3://{self.host}:{self.port}/ch{self.channel}"

    async def _read(self, reader: asyncio.StreamReader, n: int) -> bytes:
        try:
            return await asyncio.wait_for(reader.readexactly(n), self.read_timeout)
        except asyncio.IncompleteReadError as e:
            raise DriverError("device closed the connection") from e
        except TimeoutError as e:
            raise DriverError(f"no data for {self.read_timeout}s") from e

    def _parse_preamble(self, pre: bytes) -> None:
        """Best-effort metadata extraction; layout is marker-relative."""
        try:
            if len(pre) >= 0x34 and pre[0x24:0x25].isalnum():
                self.info.device_id = (
                    pre[0x24:0x34].split(b"\x00")[0].decode("ascii", "replace") or None
                )
            if len(pre) >= 0x5B:
                fw = pre[0x54:0x64].split(b"\x00")[0].decode("ascii", "replace")
                self.info.firmware = fw or None
            i = pre.find(b"H264")
            if i != -1 and len(pre) >= i + 12:
                w, h = struct.unpack_from("<HH", pre, i + 8)
                if 64 <= w <= 4096 and 64 <= h <= 4096:
                    self.info.width, self.info.height = w, h
            if len(pre) >= 0x6C:
                # observed on fw V111126: u32 @0x68 == number of channels.
                # single-sample evidence, so treat strictly as a hint — the
                # board answers ANY channel byte by wrapping modulo this count
                nch = struct.unpack_from("<I", pre, 0x68)[0]
                if 1 <= nch <= 64:
                    self.info.extra["channel_count"] = nch
        except Exception:  # metadata is nice-to-have, never fatal
            log.debug("preamble parse failed", exc_info=True)

    async def frames(self) -> AsyncIterator[VideoFrame]:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.connect_timeout
            )
        except (TimeoutError, OSError) as e:
            raise DriverError(f"connect to {self.host}:{self.port} failed: {e}") from e
        try:
            writer.write(
                build_start_stream(self.username, self.password, self.channel, self.substream)
            )
            await writer.drain()

            # ---- preamble: buffer until the first frame-header magic ----
            buf = bytearray()
            while True:
                i = buf.find(MAGIC)
                if i >= 4:
                    break
                if len(buf) > _MAX_PREAMBLE:
                    raise DriverError(
                        "no frame header in first 64KiB — wrong protocol, "
                        "channel, or credentials"
                    )
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), self.read_timeout)
                except TimeoutError as e:
                    raise DriverError(f"no data for {self.read_timeout}s") from e
                if not chunk:
                    raise DriverError("device closed the connection during preamble")
                buf += chunk
            hdr_start = i - 4  # header begins at seq_a, 4 bytes before magic
            self._parse_preamble(bytes(buf[:hdr_start]))
            log.info(
                "%s: connected (device=%s fw=%s %sx%s)",
                self,
                self.info.device_id,
                self.info.firmware,
                self.info.width,
                self.info.height,
            )
            del buf[:hdr_start]

            # ---- frame loop ----
            while True:
                while len(buf) < HEADER_LEN:
                    buf += await self._read(reader, HEADER_LEN - len(buf))
                try:
                    seq, ts_ms, ftype, plen = parse_frame_header(bytes(buf[:HEADER_LEN]))
                except DriverError:
                    await self._resync(reader, buf)
                    continue
                if plen > _MAX_FRAME:
                    await self._resync(reader, buf)
                    continue
                need = HEADER_LEN + plen
                while len(buf) < need + 8:  # +8 to peek the next header's magic
                    buf += await self._read(reader, need + 8 - len(buf))
                payload = bytes(buf[HEADER_LEN:need])
                nxt = buf[need : need + 8]
                if not payload.startswith(b"\x00\x00\x00\x01") or nxt[4:8] != MAGIC:
                    # length lied (u16 overflow) or corruption — rescan
                    await self._resync(reader, buf)
                    continue
                del buf[:need]
                keyframe = ftype == FRAME_I or any(
                    nal_type(n) == NAL_IDR for n in iter_nals(payload)
                )
                yield VideoFrame(payload=payload, ts_ms=ts_ms, keyframe=keyframe)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _resync(self, reader: asyncio.StreamReader, buf: bytearray) -> None:
        """Scan forward for the next frame-header magic, discarding garbage."""
        self.resyncs += 1
        log.warning("%s: framing desync #%d, rescanning", self, self.resyncs)
        del buf[:4]  # get past the current (bad) header candidate
        scanned = 0
        while True:
            i = buf.find(MAGIC)
            if i >= 4:
                del buf[: i - 4]
                return
            if i != -1:
                # magic found but its header start was already discarded —
                # skip this occurrence and keep scanning (costs one frame)
                del buf[: i + 4]
            elif len(buf) > 7:
                drop = len(buf) - 7  # keep a tail that may hold a split magic
                del buf[:drop]
                scanned += drop
            if scanned > _MAX_FRAME:
                raise DriverError("could not resynchronize")
            buf += await self._read(reader, 4096)
