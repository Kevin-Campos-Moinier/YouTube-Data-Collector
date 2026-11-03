"""CLI end to end, with the in-memory YouTube."""

from fakeyt import FakeYouTube, make_video
from smdc import cli, net
from smdc.sources.youtube import YouTubeSource

TOML = """
[run]
name = "cli-demo"
targets = ["aaaaaaaaaaa  # a trailer", "https://youtu.be/bbbbbbbbbbb"]

[collect]
max_items = 20
order = "relevance"

[process]
languages = ["en", "fr"]
"""


def test_cli_run_status_export_process(tmp_path, monkeypatch, capsys):
    fake = FakeYouTube({"aaaaaaaaaaa": make_video("aaaaaaaaaaa", 50), "bbbbbbbbbbb": make_video("bbbbbbbbbbb", 5)})
    monkeypatch.setattr(cli, "get_source", lambda name, b: YouTubeSource("KEY", b, get=fake))
    (tmp_path / "run.toml").write_text(TOML)
    data = ["--data", str(tmp_path / "data")]
    assert cli.main(data + ["estimate", str(tmp_path / "run.toml")]) == 0
    assert "estimated cost: 2-" in capsys.readouterr().out
    assert cli.main(data + ["run", str(tmp_path / "run.toml")]) == 0
    assert (tmp_path / "data" / "runs" / "cli-demo.toml").exists()
    assert cli.main(data + ["run", str(tmp_path / "run.toml")]) == 2  # name already taken
    assert cli.main(data + ["status"]) == 0
    assert "cli-demo" in capsys.readouterr().out
    assert cli.main(data + ["export", "cli-demo", "--excel"]) == 0
    items = (tmp_path / "data" / "exports" / "cli-demo" / "items.csv").read_bytes()
    assert items.startswith(b"\xef\xbb\xbf") and b";" in items.splitlines()[0]
    (tmp_path / "p.toml").write_text(TOML.replace('["en", "fr"]', '["en"]'))
    assert cli.main(data + ["process", "cli-demo", "--config", str(tmp_path / "p.toml")]) == 0
    assert cli.main(data + ["resume", "cli-demo"]) == 0
    assert cli.main(data + ["export", "nope"]) == 2


def test_cli_delete_asks_first_and_reports_a_bad_target_before_the_key(tmp_path, monkeypatch, capsys):
    fake = FakeYouTube({"aaaaaaaaaaa": make_video("aaaaaaaaaaa", 20), "bbbbbbbbbbb": make_video("bbbbbbbbbbb", 5)})
    monkeypatch.setattr(cli, "get_source", lambda name, b: YouTubeSource("KEY", b, get=fake))
    (tmp_path / "run.toml").write_text(TOML)
    data = ["--data", str(tmp_path / "data")]
    assert cli.main(data + ["run", str(tmp_path / "run.toml")]) == 0
    assert cli.main(data + ["export", "cli-demo"]) == 0
    assert "delete by" in (capsys.readouterr(), cli.main(data + ["status"]), capsys.readouterr())[2].out
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(data + ["delete", "cli-demo"]) == 1
    assert cli.main(data + ["status", "cli-demo"]) == 0
    assert cli.main(data + ["delete", "cli-demo", "--yes"]) == 0
    assert "1 export folder" in capsys.readouterr().out
    assert not (tmp_path / "data" / "runs" / "cli-demo.toml").exists()
    assert not (tmp_path / "data" / "exports" / "cli-demo").exists()
    assert cli.main(data + ["delete", "cli-demo", "--yes"]) == 2

    (tmp_path / "bad.toml").write_text(TOML.replace('"aaaaaaaaaaa  # a trailer"', '"oops"'))
    monkeypatch.setattr(cli, "get_source", lambda *a: (_ for _ in ()).throw(AssertionError("no key needed yet")))
    assert cli.main(data + ["estimate", str(tmp_path / "bad.toml")]) == 2
    assert "oops" in capsys.readouterr().err


def test_cli_explains_an_api_error(tmp_path, monkeypatch, capsys):
    def refuse(name, budget):
        raise net.HttpError(400, "badRequest", {"error": {"details": [{"reason": "API_KEY_INVALID"}]}})
    monkeypatch.setattr(cli, "get_source", refuse)
    (tmp_path / "run.toml").write_text(TOML)
    assert cli.main(["--data", str(tmp_path / "d"), "estimate", str(tmp_path / "run.toml")]) == 2
    assert "not valid" in capsys.readouterr().err
