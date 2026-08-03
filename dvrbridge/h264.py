"""H.264 Annex-B utilities: NAL splitting, type inspection, SPS parsing.

Everything here operates on raw bytes; no external dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

NAL_SLICE = 1
NAL_IDR = 5
NAL_SEI = 6
NAL_SPS = 7
NAL_PPS = 8
NAL_AUD = 9

_START3 = b"\x00\x00\x01"


def nal_type(nal: bytes) -> int:
    return nal[0] & 0x1F if nal else -1


def is_vcl(ntype: int) -> bool:
    return 1 <= ntype <= 5


def iter_nals(buf: bytes) -> Iterator[bytes]:
    """Split a complete Annex-B buffer into NAL payloads (start codes removed).

    Tolerates 3- and 4-byte start codes and arbitrary junk before the first
    start code (proprietary DVR headers). Trailing bytes after the last NAL
    are included in that NAL — callers get exact payloads only when the buffer
    is a clean access unit, which is what the drivers emit.
    """
    i = buf.find(_START3)
    while i != -1:
        start = i + 3
        j = buf.find(_START3, start)
        if j == -1:
            nal = buf[start:]
            if nal:
                yield nal
            return
        end = j
        # a 4-byte start code 00 00 00 01 looks like junk 00 + 3-byte code;
        # strip the trailing zero(s) that belong to the next start code
        while end > start and buf[end - 1] == 0:
            end -= 1
        if end > start:
            yield buf[start:end]
        i = j


class AnnexBSplitter:
    """Incremental Annex-B splitter for a raw byte stream.

    feed() returns fully-delimited NAL payloads; bytes between the end of one
    NAL and the next start code (e.g. proprietary frame headers a driver could
    not strip) are discarded only when they trail a start-code boundary —
    inside a NAL nothing is dropped.
    """

    def __init__(self, max_nal: int = 2 * 1024 * 1024) -> None:
        self._buf = bytearray()
        self._max = max_nal

    def feed(self, data: bytes) -> list[bytes]:
        self._buf += data
        out: list[bytes] = []
        while True:
            i = self._buf.find(_START3)
            if i == -1:
                # no start code at all; keep only a tail that could hold one
                if len(self._buf) > 2:
                    del self._buf[:-2]
                return out
            j = self._buf.find(_START3, i + 3)
            if j == -1:
                if i:
                    del self._buf[:i]
                if len(self._buf) > self._max:
                    del self._buf[:]  # runaway; drop and resync
                return out
            end = j
            while end > i + 3 and self._buf[end - 1] == 0:
                end -= 1
            nal = bytes(self._buf[i + 3 : end])
            if nal:
                out.append(nal)
            del self._buf[:j]


def strip_emulation_prevention(rbsp: bytes) -> bytes:
    return rbsp.replace(b"\x00\x00\x03", b"\x00\x00")


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def bit(self) -> int:
        byte = self.data[self.pos >> 3]
        b = (byte >> (7 - (self.pos & 7))) & 1
        self.pos += 1
        return b

    def bits(self, n: int) -> int:
        v = 0
        for _ in range(n):
            v = (v << 1) | self.bit()
        return v

    def ue(self) -> int:
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("invalid exp-golomb")
        return (1 << zeros) - 1 + (self.bits(zeros) if zeros else 0)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


@dataclass
class SpsInfo:
    profile_idc: int
    level_idc: int
    width: int
    height: int


def parse_sps(sps: bytes) -> SpsInfo:
    """Parse resolution out of an SPS NAL (payload incl. the NAL header byte)."""
    r = _BitReader(strip_emulation_prevention(sps[1:]))
    profile_idc = r.bits(8)
    r.bits(8)  # constraint flags + reserved
    level_idc = r.bits(8)
    r.ue()  # seq_parameter_set_id
    chroma_format_idc = 1
    if profile_idc in (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135):
        chroma_format_idc = r.ue()
        if chroma_format_idc == 3:
            r.bit()  # separate_colour_plane_flag
        r.ue()  # bit_depth_luma_minus8
        r.ue()  # bit_depth_chroma_minus8
        r.bit()  # qpprime_y_zero_transform_bypass_flag
        if r.bit():  # seq_scaling_matrix_present_flag
            for i in range(8 if chroma_format_idc != 3 else 12):
                if r.bit():
                    size = 16 if i < 6 else 64
                    last, nxt = 8, 8
                    for _ in range(size):
                        if nxt:
                            nxt = (last + r.se() + 256) % 256
                        last = nxt or last
    r.ue()  # log2_max_frame_num_minus4
    poc_type = r.ue()
    if poc_type == 0:
        r.ue()
    elif poc_type == 1:
        r.bit()
        r.se()
        r.se()
        for _ in range(r.ue()):
            r.se()
    r.ue()  # max_num_ref_frames
    r.bit()  # gaps_in_frame_num_value_allowed_flag
    pic_width_in_mbs = r.ue() + 1
    pic_height_in_map_units = r.ue() + 1
    frame_mbs_only = r.bit()
    if not frame_mbs_only:
        r.bit()  # mb_adaptive_frame_field_flag
    r.bit()  # direct_8x8_inference_flag
    crop_l = crop_r = crop_t = crop_b = 0
    if r.bit():  # frame_cropping_flag
        crop_l, crop_r, crop_t, crop_b = r.ue(), r.ue(), r.ue(), r.ue()

    width = pic_width_in_mbs * 16
    height = pic_height_in_map_units * 16 * (1 if frame_mbs_only else 2)
    # crop units per chroma format (Table 6-1)
    sub_w = 2 if chroma_format_idc in (1, 2) else 1
    sub_h = 2 if chroma_format_idc == 1 else 1
    crop_x = sub_w if chroma_format_idc else 1
    crop_y = sub_h * (1 if frame_mbs_only else 2) if chroma_format_idc else (
        1 if frame_mbs_only else 2
    )
    width -= (crop_l + crop_r) * crop_x
    height -= (crop_t + crop_b) * crop_y
    return SpsInfo(profile_idc, level_idc, width, height)


def profile_level_id(sps: bytes) -> str:
    """RFC 6184 profile-level-id: first three bytes after the NAL header."""
    return sps[1:4].hex()
