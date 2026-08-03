# dvrbridge

**Give your old CCTV DVR a standard RTSP interface — no transcoding, no
ffmpeg, no cloud, no extra hardware.**

Millions of 2008–2018 analog DVRs (XMEye/NetDvrV3/"MEye"-app boards and
friends) speak closed binary protocols: no RTSP, no ONVIF, nothing that
modern NVR software can consume. dvrbridge connects to the DVR with its
native protocol, strips the proprietary framing, and re-serves every channel
as plain RTSP/H.264 — stream-copied, byte-for-byte, zero CPU-hungry
re-encoding. Point [Frigate], [go2rtc], [Scrypted], Home Assistant, VLC or
Blue Iris at it and the 15-year-old camera wall is suddenly a first-class
citizen again.

- **Pure Python ≥ 3.11, zero dependencies.** One file tree, `pip install`,
  done. Runs happily on a Raspberry Pi.
- **Built-in RTSP server** (TCP-interleaved + UDP, RFC 6184 packetization,
  optional basic auth). No exec-pipeline hacks, no orphaned processes.
- **Polite to fragile hardware:** connects to the DVR only while someone is
  actually watching (configurable linger / always-on), reconnects with
  backoff, resynchronizes mid-stream after corruption.
- **Protocol simulator included** (`dvrbridge.testing.FakeDvr`) — the whole
  stack is tested end-to-end against real captured device traffic, with
  ffmpeg as the reference client, without needing hardware on the desk.

```
DVR (closed protocol) ──► dvrbridge daemon ──► rtsp://bridge:8554/dvr/ch1..N ──► Frigate / HA / VLC
```

## Quickstart

```bash
pip install .          # or: python -m dvrbridge --help straight from the repo

# 1. find your channels (also verifies credentials + protocol)
#    pass the password via env so it doesn't show up in `ps`/shell history:
export DVRBRIDGE_PASSWORD='yourpassword'
dvrbridge probe 192.168.1.108 -u admin
# ...prints a ready-to-paste dvrbridge.toml (with a CHANGE_ME password placeholder)

# 2. run the bridge
dvrbridge serve -c dvrbridge.toml

# 3. watch
ffplay rtsp://127.0.0.1:8554/dvr/ch1
```

Minimal `dvrbridge.toml`:

```toml
[server]
port = 8554

[[device]]
name = "dvr"
driver = "netdvr3"
host = "192.168.1.108"
username = "admin"
password = "yourpassword"
channels = [1, 2, 3, 4]
```

### Frigate / go2rtc

```yaml
go2rtc:
  streams:
    dvr_ch1: rtsp://<bridge-host>:8554/dvr/ch1

cameras:
  dvr_ch1:
    ffmpeg:
      inputs:
        - path: rtsp://127.0.0.1:8554/dvr_ch1
          input_args: preset-rtsp-restream
          roles: [detect, record]
```

Run `dvrbridge serve` as a systemd service or sidecar container on the same
host; there is nothing to install inside the Frigate image.

## Supported devices

| Driver | Devices | Status |
|---|---|---|
| `netdvr3` | "NetDvrV3" web-UI boards, MEye-app OEM DVRs (port 8888) | ✅ verified on real hardware |
| `dvrip` (planned) | standard XMEye/Sofia boards (port 34567) | go2rtc already covers many of these |

Don't know what your board speaks? If the web UI title is `NetDvrV3`, the
mobile app was "MEye", and port 8888 is open — that's the `netdvr3` driver.
Otherwise open an issue with `nmap` output and a packet capture of the
vendor app; new drivers are the roadmap.

## CLI

```
dvrbridge serve -c config.toml     run the daemon
dvrbridge probe HOST -u U -p P     detect channels, print config
dvrbridge cat HOST --channel 1     dump raw Annex-B H.264 to stdout
```

## Development

```bash
pip install -e .[dev]
pytest                             # 121 tests, ffmpeg used as reference client
```

`docs/DESIGN.md` documents the architecture and the reverse-engineered
NetDvrV3 wire format (24-byte frame headers, millisecond timestamps,
channel-byte semantics) in enough detail to write a compatible
implementation.

## Security notes

These DVRs are ancient embedded boards: credentials travel in cleartext on
the LAN and the firmware is unpatched by definition. Keep the DVR on an
isolated VLAN with no internet egress, let only the dvrbridge host reach it,
and put RTSP basic auth (`[server] username/password`) on the bridge if the
consumer network is shared.

[Frigate]: https://frigate.video
[go2rtc]: https://github.com/AlexxIT/go2rtc
[Scrypted]: https://www.scrypted.app
