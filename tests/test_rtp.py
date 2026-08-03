"""RTP packetizer: RFC 6184 conformance (single-NAL + FU-A round-trip)."""
import struct

import pytest

from dvrbridge.rtsp.rtp import FU_A, H264Packetizer


def parse_rtp(pkt: bytes) -> dict:
    b1, b2, seq, ts, ssrc = struct.unpack("!BBHII", pkt[:12])
    assert b1 >> 6 == 2, "RTP version must be 2"
    assert b1 & 0x20 == 0, "no padding expected"
    assert b1 & 0x0F == 0, "no CSRCs expected"
    return {
        "marker": bool(b2 & 0x80),
        "pt": b2 & 0x7F,
        "seq": seq,
        "ts": ts,
        "ssrc": ssrc,
        "payload": pkt[12:],
    }


def depacketize(packets: list[bytes]) -> list[bytes]:
    """Reassemble NALs from RTP payloads (single-NAL + FU-A)."""
    nals: list[bytes] = []
    fu: bytearray | None = None
    for pkt in packets:
        p = parse_rtp(pkt)["payload"]
        if p[0] & 0x1F == FU_A:
            indicator, fu_hdr = p[0], p[1]
            if fu_hdr & 0x80:  # start
                fu = bytearray([(indicator & 0xE0) | (fu_hdr & 0x1F)])
            assert fu is not None, "FU-A middle/end without start"
            fu += p[2:]
            if fu_hdr & 0x40:  # end
                nals.append(bytes(fu))
                fu = None
        else:
            nals.append(p)
    assert fu is None, "unterminated FU-A"
    return nals


class TestPacketizer:
    def test_single_nal(self):
        pk = H264Packetizer(ssrc=0x1234)
        nal = bytes([0x67]) + b"\x42" * 20
        pkts = pk.packetize([nal], ts90k=90_000)
        assert len(pkts) == 1
        p = parse_rtp(pkts[0])
        assert p["payload"] == nal
        assert p["marker"] is True
        assert p["ts"] == 90_000
        assert p["ssrc"] == 0x1234

    def test_marker_only_on_last_packet_of_au(self):
        pk = H264Packetizer(ssrc=1)
        nals = [bytes([0x67]) + b"a" * 10, bytes([0x68]) + b"b" * 4,
                bytes([0x65]) + b"c" * 3000]
        pkts = pk.packetize(nals, ts90k=1000)
        markers = [parse_rtp(p)["marker"] for p in pkts]
        assert markers[-1] is True
        assert not any(markers[:-1])

    def test_fu_a_round_trip(self):
        pk = H264Packetizer(ssrc=1, mtu=100)
        nal = bytes([0x65]) + bytes(range(256)) * 20  # 5121 bytes
        pkts = pk.packetize([nal], ts90k=0)
        assert len(pkts) > 50
        for p in pkts:
            assert len(p) - 12 <= 100
        assert depacketize(pkts) == [nal]

    def test_fu_a_preserves_nri(self):
        pk = H264Packetizer(ssrc=1, mtu=50)
        nal = bytes([0x65]) + b"x" * 200  # NRI bits 0b11
        first = parse_rtp(pk.packetize([nal], 0)[0])["payload"]
        assert first[0] == (0x65 & 0xE0) | FU_A
        assert first[1] & 0x1F == 0x05

    def test_sequence_increments_and_wraps(self):
        pk = H264Packetizer(ssrc=1)
        pk.seq = 0xFFFE
        pkts = pk.packetize([b"\x67aa", b"\x68bb", b"\x65cc"], 0)
        seqs = [parse_rtp(p)["seq"] for p in pkts]
        assert seqs == [0xFFFE, 0xFFFF, 0x0000]

    def test_same_timestamp_across_au(self):
        pk = H264Packetizer(ssrc=1, mtu=64)
        pkts = pk.packetize([b"\x67" + b"a" * 300, b"\x65" + b"b" * 300], ts90k=777)
        assert {parse_rtp(p)["ts"] for p in pkts} == {777}

    def test_mixed_au_round_trip(self):
        pk = H264Packetizer(ssrc=1, mtu=1400)
        nals = [bytes([0x67]) + b"s" * 9, bytes([0x68]) + b"p" * 4,
                bytes([0x65]) + b"i" * 40_000]
        assert depacketize(pk.packetize(nals, 0)) == nals

    def test_empty_nals_skipped(self):
        pk = H264Packetizer(ssrc=1)
        assert len(pk.packetize([b"", b"\x65abc"], 0)) == 1
