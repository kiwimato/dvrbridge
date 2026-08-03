"""SDP session description for a single H.264 video track."""
from __future__ import annotations

import base64

from ..h264 import profile_level_id


def build_sdp(
    name: str,
    sps: bytes,
    pps: bytes,
    payload_type: int = 96,
    width: int | None = None,
    height: int | None = None,
) -> str:
    sprop = f"{base64.b64encode(sps).decode()},{base64.b64encode(pps).decode()}"
    lines = [
        "v=0",
        "o=- 0 0 IN IP4 0.0.0.0",
        f"s={name}",
        "t=0 0",
        f"m=video 0 RTP/AVP {payload_type}",
        "c=IN IP4 0.0.0.0",
        f"a=rtpmap:{payload_type} H264/90000",
        f"a=fmtp:{payload_type} packetization-mode=1;"
        f"profile-level-id={profile_level_id(sps)};"
        f"sprop-parameter-sets={sprop}",
        "a=control:track0",
        "a=recvonly",
    ]
    if width and height:
        lines.insert(-2, f"a=x-dimensions:{width},{height}")
    return "\r\n".join(lines) + "\r\n"
