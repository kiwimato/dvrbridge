"""Driver registry. Each driver bridges one proprietary DVR protocol family."""
from __future__ import annotations

from .base import Driver, DriverError, StreamInfo, VideoFrame
from .netdvr3 import NetDvr3Driver

_REGISTRY: dict[str, type[Driver]] = {
    NetDvr3Driver.name: NetDvr3Driver,
}


def get_driver(name: str) -> type[Driver]:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown driver {name!r}; available: {', '.join(sorted(_REGISTRY))}"
        ) from None


def available() -> list[str]:
    return sorted(_REGISTRY)


__all__ = [
    "Driver",
    "DriverError",
    "StreamInfo",
    "VideoFrame",
    "NetDvr3Driver",
    "get_driver",
    "available",
]
