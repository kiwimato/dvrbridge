"""Tiny RTSP test client with interleaved-data demuxing."""
from __future__ import annotations

import asyncio


class RtspTestClient:
    def __init__(self, reader, writer, host, port):
        self.reader, self.writer = reader, writer
        self.host, self.port = host, port
        self.cseq = 0
        self._data: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue()

    @classmethod
    async def connect(cls, host: str, port: int) -> "RtspTestClient":
        reader, writer = await asyncio.open_connection(host, port)
        return cls(reader, writer, host, port)

    def url(self, path: str) -> str:
        return f"rtsp://{self.host}:{self.port}/{path}"

    async def request(
        self, method: str, url: str, headers: dict | None = None, timeout: float = 5.0
    ) -> tuple[int, dict, bytes]:
        self.cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self.cseq}"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await self.writer.drain()
        return await asyncio.wait_for(self._read_response(), timeout)

    async def _read_response(self) -> tuple[int, dict, bytes]:
        # demux: skip interleaved data frames until a response line arrives
        while True:
            first = await self.reader.readexactly(1)
            if first == b"$":
                hdr = await self.reader.readexactly(3)
                ch = hdr[0]
                ln = int.from_bytes(hdr[1:3], "big")
                self._data.put_nowait((ch, await self.reader.readexactly(ln)))
                continue
            status_line = (first + await self.reader.readline()).decode()
            if not status_line.strip():
                continue
            break
        assert status_line.startswith("RTSP/1.0 "), f"bad status line: {status_line!r}"
        code = int(status_line.split()[1])
        headers: dict[str, str] = {}
        while True:
            line = await self.reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode().partition(":")
            headers[k.strip().lower()] = v.strip()
        body = b""
        if "content-length" in headers:
            body = await self.reader.readexactly(int(headers["content-length"]))
        return code, headers, body

    async def read_rtp_packets(
        self, n: int, timeout: float = 5.0
    ) -> list[tuple[int, bytes]]:
        async def _read():
            out = []
            while len(out) < n:
                if not self._data.empty():
                    out.append(self._data.get_nowait())
                    continue
                first = await self.reader.readexactly(1)
                if first != b"$":
                    # unsolicited response text — consume the line and move on
                    await self.reader.readline()
                    continue
                hdr = await self.reader.readexactly(3)
                ch = hdr[0]
                ln = int.from_bytes(hdr[1:3], "big")
                pkt = await self.reader.readexactly(ln)
                if ch % 2 == 0:  # even = RTP, odd = RTCP
                    out.append((ch, pkt))
            return out

        return await asyncio.wait_for(_read(), timeout)

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass
