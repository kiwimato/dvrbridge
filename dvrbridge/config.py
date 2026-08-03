"""TOML configuration.

Example:

    [server]
    listen = "0.0.0.0"
    port = 8554
    # username = "viewer"        # optional RTSP basic auth
    # password = "secret"

    [[device]]
    name = "dvr"
    driver = "netdvr3"
    host = "192.168.1.108"
    port = 8888
    username = "admin"
    password = "CHANGE_ME"
    channels = [1, 2, 3, 4]
    # always_on = false          # keep device connections up without viewers
    # linger = 10.0              # seconds to stay connected after last viewer
    # substream = false

Streams are exposed as rtsp://host:8554/<device-name>/ch<N>.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .drivers import get_driver
from .hub import Stream, StreamHub


@dataclass
class ServerConfig:
    listen: str = "0.0.0.0"
    port: int = 8554
    username: str | None = None
    password: str | None = None


@dataclass
class Config:
    server: ServerConfig = field(default_factory=ServerConfig)
    devices: list[dict] = field(default_factory=list)


class ConfigError(Exception):
    pass


def load_config(path: str | Path) -> Config:
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"invalid TOML in {path}: {e}") from None

    srv = raw.get("server", {})
    cfg = Config(
        server=ServerConfig(
            listen=srv.get("listen", "0.0.0.0"),
            port=int(srv.get("port", 8554)),
            username=srv.get("username"),
            password=srv.get("password"),
        ),
        devices=raw.get("device", []),
    )
    if bool(cfg.server.username) != bool(cfg.server.password):
        raise ConfigError("[server] username and password must be set together")
    if not cfg.devices:
        raise ConfigError("no [[device]] sections configured")
    for dev in cfg.devices:
        for key in ("name", "driver", "host"):
            if key not in dev:
                raise ConfigError(f"[[device]] missing required key {key!r}")
    return cfg


def build_hub(cfg: Config) -> StreamHub:
    hub = StreamHub()
    for dev in cfg.devices:
        driver_cls = get_driver(dev["driver"])
        channels = dev.get("channels", [1])
        for ch in channels:
            kwargs = dict(
                host=dev["host"],
                username=dev.get("username", ""),
                password=dev.get("password", ""),
                channel=int(ch),
                substream=bool(dev.get("substream", False)),
            )
            if "port" in dev:
                kwargs["port"] = int(dev["port"])

            def factory(_cls=driver_cls, _kw=kwargs):
                return _cls(**_kw)

            hub.add(
                Stream(
                    name=f"{dev['name']}/ch{ch}",
                    driver_factory=factory,
                    always_on=bool(dev.get("always_on", False)),
                    linger=float(dev.get("linger", 10.0)),
                )
            )
    return hub
