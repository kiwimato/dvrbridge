"""CLI: argument parsing + probe/cat command paths against the simulator."""
import argparse

import pytest

from dvrbridge import cli
from dvrbridge.testing import FakeDvr


class TestArgParsing:
    def test_no_command_errors(self):
        with pytest.raises(SystemExit):
            cli.main([])

    def test_version(self, capsys):
        with pytest.raises(SystemExit) as e:
            cli.main(["--version"])
        assert e.value.code == 0
        assert "dvrbridge" in capsys.readouterr().out

    def test_serve_missing_config_returns_2(self, tmp_path):
        rc = cli.main(["serve", "-c", str(tmp_path / "nope.toml")])
        assert rc == 2

    def test_password_read_from_env_when_flag_omitted(self, monkeypatch):
        seen = {}

        async def fake_cat(args):
            seen["pw"] = args.password
            return 0

        monkeypatch.setattr(cli, "_cat", fake_cat)
        monkeypatch.setenv("DVRBRIDGE_PASSWORD", "fromenv")
        assert cli.main(["cat", "127.0.0.1", "-u", "admin"]) == 0
        assert seen["pw"] == "fromenv"

    def test_explicit_password_flag_overrides_env(self, monkeypatch):
        seen = {}

        async def fake_cat(args):
            seen["pw"] = args.password
            return 0

        monkeypatch.setattr(cli, "_cat", fake_cat)
        monkeypatch.setenv("DVRBRIDGE_PASSWORD", "fromenv")
        assert cli.main(["cat", "127.0.0.1", "-p", "explicit"]) == 0
        assert seen["pw"] == "explicit"


async def _fake(annexb, **kw):
    fake = FakeDvr(annexb=annexb, fps=200, **kw)
    port = await fake.start()
    return fake, port


class TestProbe:
    async def test_probe_finds_channels(self, annexb, capsys):
        fake, port = await _fake(annexb, channels=4)
        args = argparse.Namespace(
            host="127.0.0.1", username="admin", password="secret",
            port=port, max_channels=6, timeout=2.0,
        )
        rc = await cli._probe(args)
        await fake.stop()
        out = capsys.readouterr().out
        assert rc == 0
        assert "[[device]]" in out
        assert f"port = {port}" in out
        assert "channels = [1, 2, 3, 4]" in out
        # the emitted config must NOT echo the real password into scrollback
        assert "secret" not in out
        assert "CHANGE_ME" in out

    async def test_probe_wrong_credentials_finds_nothing(self, annexb):
        fake, port = await _fake(annexb, channels=4)
        args = argparse.Namespace(
            host="127.0.0.1", username="admin", password="WRONG",
            port=port, max_channels=3, timeout=0.6,
        )
        rc = await cli._probe(args)
        await fake.stop()
        assert rc == 1


class TestCat:
    async def test_cat_emits_annexb_to_stdout(self, annexb, capsysbinary):
        fake, port = await _fake(annexb)
        args = argparse.Namespace(
            host="127.0.0.1", username="admin", password="secret",
            port=port, channel=1, substream=False, frames=10,
        )
        rc = await cli._cat(args)
        await fake.stop()
        assert rc == 0
        out = capsysbinary.readouterr().out
        assert out.startswith(b"\x00\x00\x00\x01")
        assert out.count(b"\x00\x00\x00\x01") >= 10

    async def test_cat_bad_host_returns_1(self):
        args = argparse.Namespace(
            host="127.0.0.1", username="a", password="b",
            port=1, channel=1, substream=False, frames=0,
        )
        rc = await cli._cat(args)
        assert rc == 1
