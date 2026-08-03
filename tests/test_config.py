"""Config parsing/validation and hub construction."""
import pytest

from dvrbridge.config import Config, ConfigError, build_hub, load_config
from dvrbridge.drivers import available, get_driver


def write(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text)
    return str(p)


GOOD = """
[server]
listen = "127.0.0.1"
port = 8600

[[device]]
name = "dvr"
driver = "netdvr3"
host = "192.168.1.9"
port = 8888
username = "admin"
password = "secret"
channels = [1, 2, 3, 4]
"""


class TestLoad:
    def test_parses_good_config(self, tmp_path):
        cfg = load_config(write(tmp_path, GOOD))
        assert cfg.server.port == 8600
        assert cfg.server.listen == "127.0.0.1"
        assert len(cfg.devices) == 1
        assert cfg.devices[0]["channels"] == [1, 2, 3, 4]

    def test_missing_file(self):
        with pytest.raises(ConfigError, match="not found"):
            load_config("/nonexistent/x.toml")

    def test_invalid_toml(self, tmp_path):
        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config(write(tmp_path, "this is = = not toml"))

    def test_no_devices_rejected(self, tmp_path):
        with pytest.raises(ConfigError, match="no \\[\\[device\\]\\]"):
            load_config(write(tmp_path, "[server]\nport=8554\n"))

    def test_missing_required_device_key(self, tmp_path):
        bad = '[[device]]\nname="x"\ndriver="netdvr3"\n'  # no host
        with pytest.raises(ConfigError, match="host"):
            load_config(write(tmp_path, bad))

    def test_auth_requires_both_fields(self, tmp_path):
        bad = GOOD + '\n[server]\n'  # duplicate table would fail; do it inline
        one = """
[server]
port = 8554
username = "u"

[[device]]
name = "d"
driver = "netdvr3"
host = "h"
"""
        with pytest.raises(ConfigError, match="username and password"):
            load_config(write(tmp_path, one))

    def test_defaults_when_server_absent(self, tmp_path):
        minimal = '[[device]]\nname="d"\ndriver="netdvr3"\nhost="h"\n'
        cfg = load_config(write(tmp_path, minimal))
        assert cfg.server.listen == "0.0.0.0"
        assert cfg.server.port == 8554


class TestBuildHub:
    def test_one_stream_per_channel(self, tmp_path):
        hub = build_hub(load_config(write(tmp_path, GOOD)))
        assert set(hub.streams) == {"dvr/ch1", "dvr/ch2", "dvr/ch3", "dvr/ch4"}

    def test_factory_builds_configured_driver(self, tmp_path):
        hub = build_hub(load_config(write(tmp_path, GOOD)))
        drv = hub.get("dvr/ch2")._factory()
        assert drv.channel == 2
        assert drv.host == "192.168.1.9"
        assert drv.port == 8888
        assert drv.username == "admin"

    def test_default_single_channel(self, tmp_path):
        cfg = load_config(write(tmp_path, '[[device]]\nname="d"\ndriver="netdvr3"\nhost="h"\n'))
        hub = build_hub(cfg)
        assert set(hub.streams) == {"d/ch1"}

    def test_unknown_driver_raises(self, tmp_path):
        bad = '[[device]]\nname="d"\ndriver="nope"\nhost="h"\n'
        with pytest.raises(KeyError, match="unknown driver"):
            build_hub(load_config(write(tmp_path, bad)))

    def test_linger_and_always_on_passed_through(self, tmp_path):
        c = """
[[device]]
name = "d"
driver = "netdvr3"
host = "h"
channels = [1]
always_on = true
linger = 42.0
"""
        hub = build_hub(load_config(write(tmp_path, c)))
        s = hub.get("d/ch1")
        assert s.always_on is True and s.linger == 42.0


def test_registry():
    assert "netdvr3" in available()
    assert get_driver("netdvr3").name == "netdvr3"
    with pytest.raises(KeyError):
        get_driver("bogus")
