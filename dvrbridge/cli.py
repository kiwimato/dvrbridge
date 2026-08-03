"""dvrbridge command-line interface.

    dvrbridge serve -c dvrbridge.toml         run the RTSP bridge daemon
    dvrbridge probe HOST -u admin -p PASS     detect channels, emit config
    dvrbridge cat HOST --channel 1 > ch1.h264 dump raw H.264 to stdout
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import __version__
from .config import ConfigError, build_hub, load_config
from .drivers import DriverError
from .drivers.netdvr3 import NetDvr3Driver
from .h264 import NAL_SPS, iter_nals, nal_type, parse_sps
from .rtsp import RTSPServer

log = logging.getLogger("dvrbridge")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )


async def _serve(args: argparse.Namespace) -> int:
    try:
        cfg = load_config(args.config)
        hub = build_hub(cfg)
    except ConfigError as e:
        log.error("%s", e)
        return 2
    auth = None
    if cfg.server.username:
        auth = (cfg.server.username, cfg.server.password or "")
    server = RTSPServer(hub, cfg.server.listen, cfg.server.port, auth=auth)
    await server.start()
    await hub.start_always_on()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("shutting down")
    await server.stop()
    await hub.stop()
    return 0


async def _probe_channel(args: argparse.Namespace, ch: int) -> dict | None:
    drv = NetDvr3Driver(
        args.host, args.username, args.password, ch, port=args.port,
        connect_timeout=args.timeout, read_timeout=args.timeout,
    )
    gen = drv.frames()
    try:
        async with asyncio.timeout(args.timeout + 5):
            async for frame in gen:
                res = None
                for n in iter_nals(frame.payload):
                    if nal_type(n) == NAL_SPS:
                        s = parse_sps(n)
                        res = (s.width, s.height)
                return {
                    "channel": ch,
                    "device_id": drv.info.device_id,
                    "firmware": drv.info.firmware,
                    "resolution": res or (drv.info.width, drv.info.height),
                    "channel_count": drv.info.extra.get("channel_count"),
                }
    except (DriverError, OSError, TimeoutError):
        return None
    finally:
        await gen.aclose()
    return None


async def _probe(args: argparse.Namespace) -> int:
    print(f"probing {args.host}:{args.port} (netdvr3) ...", file=sys.stderr)
    found = []
    limit = args.max_channels
    ch = 1
    while ch <= limit:
        info = await _probe_channel(args, ch)
        if info:
            w, h = info["resolution"]
            print(
                f"  ch{ch}: OK  {w}x{h}  device={info['device_id']} "
                f"fw={info['firmware']}",
                file=sys.stderr,
            )
            found.append(ch)
            nch = info.get("channel_count")
            if nch and nch < limit:
                # boards wrap the channel byte modulo their channel count, so
                # probing past the advertised count only yields duplicates
                print(
                    f"  (device advertises {nch} channels; higher channel "
                    f"numbers wrap around — stopping there)",
                    file=sys.stderr,
                )
                limit = nch
        else:
            print(f"  ch{ch}: no stream", file=sys.stderr)
        ch += 1
    if not found:
        print("no channels found — check host/credentials/port", file=sys.stderr)
        return 1
    print("\n# paste into dvrbridge.toml:")
    print("[server]\nport = 8554\n")
    print("[[device]]")
    print('name = "dvr"')
    print('driver = "netdvr3"')
    print(f'host = "{args.host}"')
    print(f"port = {args.port}")
    print(f'username = "{args.username}"')
    # never echo the secret into scrollback / a redirected file
    print('password = "CHANGE_ME"  # set to your DVR password')
    print(f"channels = {found}")
    return 0


async def _cat(args: argparse.Namespace) -> int:
    drv = NetDvr3Driver(
        args.host, args.username, args.password, args.channel,
        port=args.port, substream=args.substream,
    )
    out = sys.stdout.buffer
    n = 0
    gen = drv.frames()
    try:
        async for frame in gen:
            out.write(frame.payload)
            out.flush()
            n += 1
            if args.frames and n >= args.frames:
                break
    except (DriverError, OSError) as e:
        log.error("%s", e)
        return 1
    except BrokenPipeError:
        pass
    finally:
        await gen.aclose()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dvrbridge",
        description="Bridge legacy closed-protocol CCTV DVRs to standard RTSP.",
    )
    ap.add_argument("--version", action="version", version=f"dvrbridge {__version__}")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the RTSP bridge daemon")
    p.add_argument("-c", "--config", default="dvrbridge.toml")

    p = sub.add_parser("probe", help="detect channels on a device, emit config")
    p.add_argument("host")
    p.add_argument("-u", "--username", default="admin")
    p.add_argument("-p", "--password", default="",
                   help="DVR password; if omitted, read from $DVRBRIDGE_PASSWORD "
                        "(passing it here exposes it via `ps`/proc)")
    p.add_argument("--port", type=int, default=8888)
    p.add_argument("--max-channels", type=int, default=8)
    p.add_argument("--timeout", type=float, default=5.0)

    p = sub.add_parser("cat", help="dump raw Annex-B H.264 for one channel to stdout")
    p.add_argument("host")
    p.add_argument("-u", "--username", default="admin")
    p.add_argument("-p", "--password", default="",
                   help="DVR password; if omitted, read from $DVRBRIDGE_PASSWORD "
                        "(passing it here exposes it via `ps`/proc)")
    p.add_argument("--port", type=int, default=8888)
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--substream", action="store_true")
    p.add_argument("--frames", type=int, default=0, help="stop after N frames")

    args = ap.parse_args(argv)
    # keep the secret off the command line when possible: fall back to the env
    if getattr(args, "password", None) == "":
        args.password = os.environ.get("DVRBRIDGE_PASSWORD", "")
    _setup_logging(args.verbose)
    runner = {"serve": _serve, "probe": _probe, "cat": _cat}[args.cmd]
    try:
        return asyncio.run(runner(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
