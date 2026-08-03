"""RTP packetization of H.264 per RFC 6184 (packetization-mode=1).

Single NAL unit packets when the NAL fits in the MTU, FU-A fragmentation
otherwise. The marker bit is set on the final packet of each access unit.
"""
from __future__ import annotations

import struct

RTP_VERSION = 2
FU_A = 28


class H264Packetizer:
    def __init__(self, ssrc: int, payload_type: int = 96, mtu: int = 1400) -> None:
        self.ssrc = ssrc & 0xFFFFFFFF
        self.pt = payload_type
        self.mtu = mtu
        self.seq = 0
        self.packets_sent = 0
        self.octets_sent = 0

    def _header(self, marker: bool, ts: int) -> bytes:
        b1 = RTP_VERSION << 6
        b2 = (0x80 if marker else 0) | self.pt
        hdr = struct.pack("!BBHII", b1, b2, self.seq, ts & 0xFFFFFFFF, self.ssrc)
        self.seq = (self.seq + 1) & 0xFFFF
        return hdr

    def _emit(self, out: list[bytes], payload: bytes, marker: bool, ts: int) -> None:
        pkt = self._header(marker, ts) + payload
        out.append(pkt)
        self.packets_sent += 1
        self.octets_sent += len(pkt) - 12

    def packetize(self, nals: list[bytes], ts90k: int) -> list[bytes]:
        """Packetize one access unit (list of NAL payloads, no start codes)."""
        out: list[bytes] = []
        for i, nal in enumerate(nals):
            if not nal:
                continue
            last_nal = i == len(nals) - 1
            if len(nal) <= self.mtu:
                self._emit(out, nal, last_nal, ts90k)
            else:
                indicator = (nal[0] & 0xE0) | FU_A
                ntype = nal[0] & 0x1F
                body = nal[1:]
                chunk = self.mtu - 2
                for off in range(0, len(body), chunk):
                    piece = body[off : off + chunk]
                    start = off == 0
                    end = off + chunk >= len(body)
                    fu_hdr = (0x80 if start else 0) | (0x40 if end else 0) | ntype
                    self._emit(
                        out,
                        bytes((indicator, fu_hdr)) + piece,
                        last_nal and end,
                        ts90k,
                    )
        return out
