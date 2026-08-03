"""Driver contract: one driver instance == one live channel connection.

A driver connects to the device, performs whatever proprietary handshake the
protocol needs, and yields clean Annex-B access units. Drivers never
reconnect — the StreamHub supervises them and applies backoff policy.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class VideoFrame:
    """One access unit of Annex-B H.264 (may contain SPS/PPS/SEI + slice)."""

    payload: bytes
    ts_ms: int | None = None  # device clock, milliseconds, arbitrary epoch
    keyframe: bool = False


@dataclass
class StreamInfo:
    codec: str = "h264"
    width: int | None = None
    height: int | None = None
    device_id: str | None = None
    firmware: str | None = None
    extra: dict = field(default_factory=dict)


class DriverError(Exception):
    """Protocol-level failure (bad handshake, desync, device closed)."""


class Driver(ABC):
    """Async source of video frames for a single channel."""

    #: registry key, e.g. "netdvr3"
    name: str = "abstract"

    def __init__(self) -> None:
        self.info = StreamInfo()

    @abstractmethod
    def frames(self) -> AsyncIterator[VideoFrame]:
        """Connect and yield frames until the connection dies.

        Must raise DriverError/OSError on failure and clean up its socket on
        exit (including generator close / task cancellation).
        """
        raise NotImplementedError
