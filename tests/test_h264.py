"""Unit tests for Annex-B parsing and SPS decoding."""
import pytest

from dvrbridge.h264 import (
    NAL_IDR,
    NAL_PPS,
    NAL_SPS,
    AnnexBSplitter,
    iter_nals,
    nal_type,
    parse_sps,
    profile_level_id,
)

# SPS/PPS captured from the real DVR (352x288 CIF)
REAL_SPS = bytes.fromhex("6742001495a8582590")
REAL_PPS = bytes.fromhex("68ce3c80")


def sc(*nals: bytes) -> bytes:
    return b"".join(b"\x00\x00\x00\x01" + n for n in nals)


class TestIterNals:
    def test_basic_split(self):
        nals = list(iter_nals(sc(REAL_SPS, REAL_PPS)))
        assert nals == [REAL_SPS, REAL_PPS]

    def test_three_byte_start_codes(self):
        buf = b"\x00\x00\x01" + REAL_SPS + b"\x00\x00\x01" + REAL_PPS
        assert list(iter_nals(buf)) == [REAL_SPS, REAL_PPS]

    def test_junk_before_first_start_code(self):
        buf = b"\xde\xad\xbe\xef" * 4 + sc(REAL_SPS, REAL_PPS)
        assert list(iter_nals(buf)) == [REAL_SPS, REAL_PPS]

    def test_no_start_code(self):
        assert list(iter_nals(b"\xff" * 100)) == []

    def test_empty(self):
        assert list(iter_nals(b"")) == []

    def test_nal_containing_zero_bytes(self):
        nal = bytes([0x65]) + b"\x11\x00\x22\x00\x00\x03\x00\x44"
        assert list(iter_nals(sc(nal, REAL_PPS))) == [nal, REAL_PPS]


class TestAnnexBSplitter:
    @pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, 100, 10_000])
    def test_chunked_feeding(self, chunk_size):
        payload = sc(REAL_SPS, REAL_PPS, bytes([0x65]) + b"\xaa" * 500) * 3
        splitter = AnnexBSplitter()
        got = []
        for i in range(0, len(payload), chunk_size):
            got += splitter.feed(payload[i : i + chunk_size])
        # the final NAL stays buffered (no trailing start code to delimit it)
        expected = [REAL_SPS, REAL_PPS, bytes([0x65]) + b"\xaa" * 500] * 3
        assert got == expected[: len(got)]
        assert len(got) >= len(expected) - 1

    def test_junk_between_nals_is_dropped(self):
        splitter = AnnexBSplitter()
        # junk before the first start code is not part of any NAL
        got = splitter.feed(b"\x63\x00\x0c\x00" * 6 + sc(REAL_SPS) + sc(REAL_PPS))
        assert got == [REAL_SPS]


class TestSps:
    def test_real_dvr_sps_is_cif(self):
        info = parse_sps(REAL_SPS)
        assert (info.width, info.height) == (352, 288)
        assert info.profile_idc == 66  # baseline

    def test_fixture_sps(self, annexb):
        sps = next(n for n in iter_nals(annexb) if nal_type(n) == NAL_SPS)
        info = parse_sps(sps)
        assert (info.width, info.height) == (352, 288)

    def test_1080p_sps(self):
        # ffmpeg-generated High profile 1920x1080 SPS (has cropping: 1088->1080)
        sps = bytes.fromhex("67640028acd940780227e5c044000003000400000300c83c60c658")
        info = parse_sps(sps)
        assert (info.width, info.height) == (1920, 1080)

    def test_profile_level_id(self):
        assert profile_level_id(REAL_SPS) == "420014"

    def test_types(self, annexb):
        types = {nal_type(n) for n in iter_nals(annexb)}
        assert NAL_SPS in types and NAL_PPS in types and NAL_IDR in types
