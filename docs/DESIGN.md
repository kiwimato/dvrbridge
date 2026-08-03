# dvrbridge — design

**One-line pitch:** a single self-contained daemon that speaks the closed
binary protocols of legacy CCTV DVRs and re-exposes every channel as a
standard RTSP stream — no ffmpeg, no transcoding, no cloud — so old analog
boards plug straight into Frigate, go2rtc, Scrypted, Home Assistant, VLC.

Working name `dvrbridge` (branding TBD, see PRODUCT.md).

## Why this shape

- The pain: millions of installed 2008–2018 analog DVRs speak vendor-locked
  TCP protocols (XMEye/Sofia, NetDvrV3, old Dahua/Hik OEM stacks). No RTSP,
  no ONVIF. Modern NVR software consumes RTSP. Today people either throw the
  hardware away, buy per-channel encoder boxes, or run fragile
  `exec:python|ffmpeg` pipelines (what this project's own `bridge.py` does).
- The fix: DVRs already emit standard H.264 inside thin proprietary framing.
  Strip framing, re-packetize as RTP — **stream copy, zero transcode**. A
  Raspberry Pi can bridge dozens of channels.
- Architecture bet: pluggable **drivers** (one per protocol family) feeding a
  shared **hub** and a built-in **RTSP server**. The NetDvrV3 driver is the
  proof; every additional driver multiplies the addressable install base.

## Architecture

```
                 ┌────────────────────────── dvrbridge daemon ───────────────────────┐
 DVR #1 ────────►│ driver: netdvr3 ─┐                                                │
   (port 8888)   │                  ├─► StreamHub ─► RTSP server (:8554) ─► N clients│
 DVR #2 ────────►│ driver: xyz     ─┘   per-stream fan-out   TCP-interleaved + UDP   │
                 └────────────────────────────────────────────────────────────────---┘
```

- **Driver** (`drivers/base.py`): async, owns one TCP connection per channel.
  Yields `VideoFrame(payload=annexb_access_unit, ts_ms, keyframe)` plus a
  `StreamInfo` (codec, WxH, device id, firmware) parsed from the preamble.
  Drivers do NOT reconnect; the hub supervises.
- **StreamHub** (`hub.py`): one `Stream` per configured channel. On-demand:
  driver connects when the first RTSP client subscribes (DESCRIBE), and
  disconnects `linger` seconds after the last one leaves — critical because
  these boards have small concurrent-connection limits. `always_on` opt-in.
  Caches SPS/PPS (for SDP) and starts new subscribers at a keyframe.
  Reconnects with capped exponential backoff.
- **RTSP server** (`rtsp/`): stdlib asyncio. Implements OPTIONS, DESCRIBE,
  SETUP, PLAY, PAUSE (as no-op keepalive), TEARDOWN, GET/SET_PARAMETER.
  Transports: `RTP/AVP/TCP` (interleaved, primary — what go2rtc/Frigate use)
  and `RTP/AVP` UDP unicast. RFC 6184 packetization: single-NAL units when
  they fit the MTU, FU-A fragmentation otherwise; marker bit on the last
  packet of each access unit; 90 kHz timestamps derived from the DVR's own
  millisecond clock (fallback: server monotonic clock). Optional basic auth.
- **Zero dependencies** at runtime: pure CPython ≥ 3.11 stdlib (tomllib for
  config). ffmpeg is used only by the test suite as a reference RTSP client.

## NetDvrV3 protocol (reverse-engineered, refined 2026-08-01)

Live-verified against firmware V111126 (device ID redacted). Supersedes
the coarser notes in the parent repo's CLAUDE.md.

1. Open TCP to port 8888. Send ONE 76-byte start-stream packet (no login):
   BE u32 `0x48`, u32 0, 12-byte const `28 00 04 00 05 00 00 00 29 00 38 00`,
   username NUL-padded @0x14, password @0x34, sub-stream flag? @0x48,
   channel byte (0-indexed) @0x49.
2. DVR replies with a preamble: 20-byte header (`00 00 00 14` …) then a
   metadata block: device ID (ASCII @0x24), firmware (@0x54), codec fourcc
   `H264` (@0x7C), width/height LE u16 (@0x84/0x86). Parse opportunistically;
   sync forward to the first frame header.
3. Then a sequence of frames, each: **24-byte header + Annex-B access unit**.

   ```
   offset size  field                (all little-endian)
   +0     u32   seq_a       increments by 1 per frame
   +4     u16   0x0063      magic 'c'
   +6     u16   0x000c      const
   +8     u32   0
   +12    u32   seq_b       increments by 1 per frame (different base)
   +16    u32   timestamp   MILLISECONDS (delta 120 ms observed @ ~8.3 fps)
   +20    u8    frame type  0x64 'd' = I (AU = SPS+PPS+SEI+IDR), 0x66 'f' = P
   +21    u8    0
   +22    u16   payload_len = AU length MINUS the leading 4-byte start code
   ```

   Verified: `payload_len + 4` exactly spans start-code → next header, on both
   I and P frames. The u16 length caps at 65535; a >64 KiB AU would overflow,
   so the parser treats the length as advisory and **resyncs on the
   `63 00 0c 00` magic** if the byte at the predicted boundary doesn't start a
   valid header (scan-forward recovery, also handles mid-stream corruption).
4. Keepalive: none observed; the DVR streams until the client closes.
   The board's concurrent-connection budget is small — close sockets fast.

## Testing strategy (the point of the weekend)

1. **Unit**: Annex-B splitter under adversarial chunking (1-byte feeds, split
   start codes, 3- vs 4-byte codes), SPS resolution parser, RTP packetizer
   (fragment/reassemble round-trip, marker bits, seq/ts), RTSP request
   parsing, netdvr3 header parser incl. resync-after-corruption.
2. **Simulator** (`tests/fake_dvr.py`): asyncio server replaying the REAL
   captured preamble and 24-byte framing around fixture H.264 (generated by
   ffmpeg + real DVR bytes). Configurable: fps, drop-mid-stream, garbage
   injection, wrong-credential behavior, connection cap.
3. **Integration**: fake DVR → daemon → `ffprobe`/`ffmpeg` as reference RTSP
   clients over both transports; decode-to-PNG proves end-to-end integrity;
   multi-client fan-out; DVR drop → hub reconnect → client stream continues.
4. **Live**: all 4 channels of the real DVR concurrently through RTSP,
   snapshot + timestamp sanity + soak.

## Known limitations (surfaced by adversarial review, 2026-08-01)

- **Access units > 64 KiB** are not reassembled: the frame header's length is a
  u16, so for a >64 KiB AU the parser can't trust it and resyncs past the frame
  (dropping it) rather than emitting garbage. Irrelevant for the target CIF
  analog boards (frames are 2–8 KiB); would matter for a hypothetical 1080p
  device on this protocol. Fix would be magic-scan-based length recovery.
- **DESCRIBE without a following PLAY** spins up the device connection and holds
  it for `linger` seconds with no viewer. Behind the intended isolated VLAN this
  is benign; if the RTSP port is exposed, enable `[server]` auth (which gates
  DESCRIBE) and/or lower `linger`. Not a rate-limited hard cap today.
- Resync accepts a candidate on the 4-byte magic alone; the **frame loop then
  corroborates** it (payload must begin with a start code and the next header's
  magic must follow) before emitting, so a `63 00 0c 00` pattern occurring
  inside payload is rejected — verified in `tests/test_robustness.py`.

## Roadmap (post-weekend) — prioritized by round-2 research (docs/research_round2.md)

- **ONVIF Profile S emulation** (discovery + GetStreamUri) — highest-value
  *product* lever: wanted, under-served (existing tools are Unifi-scoped), and
  it turns dvrbridge into a vendor-neutral appliance ANY NVR can auto-discover.
  Validate against Blue Iris / Synology / Milestone, not just Unifi.
- **Driver #2: TVT / NVMS9000 (port 4567)** — a 79-brand OEM ecosystem,
  underserved, architecturally near-identical to netdvr3 (magic-GUID + base64
  XML behind a binary header); reference: mcw0/PoC.
- **Driver #3: standard XMEye/Sofia DVRIP (34567)** — huge base but already
  served by go2rtc/python-dvr; generalize the login-less 8888 variant instead.
- **Upstream a driver into go2rtc** — validated path (external protocol PRs do
  merge). Lead with a high-quality issue + the packet capture. Reputation ROI.
- PTZ as an ONVIF sub-feature (unlocks Frigate autotracking); G.711 audio
  pass-through (cheap bonus).
- Packaging: single-file zipapp, Docker image (done), HAOS add-on.
