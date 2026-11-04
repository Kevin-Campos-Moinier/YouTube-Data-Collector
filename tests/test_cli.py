"""CLI end to end, with the in-memory YouTube."""

import pytest

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
    assert "estimated cost: about" in capsys.readouterr().out
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


def test_cli_says_what_comes_next_and_explains_a_missing_file(tmp_path, monkeypatch, capsys):
    fake = FakeYouTube({"aaaaaaaaaaa": make_video("aaaaaaaaaaa", 20), "bbbbbbbbbbb": make_video("bbbbbbbbbbb", 5)})
    monkeypatch.setattr(cli, "get_source", lambda name, b: YouTubeSource("KEY", b, get=fake))
    (tmp_path / "run.toml").write_text(TOML)
    data = ["--data", str(tmp_path / "d")]
    assert cli.main(data + ["estimate", str(tmp_path / "run.toml")]) == 0
    assert f"next: smdc run {tmp_path / 'run.toml'}" in capsys.readouterr().out
    assert cli.main(data + ["run", str(tmp_path / "run.toml")]) == 0
    assert "next: smdc export cli-demo" in capsys.readouterr().out
    assert cli.main(data + ["export", "cli-demo"]) == 0
    assert "30-day rule" in capsys.readouterr().out
    assert cli.main(data + ["estimate", str(tmp_path / "nope.toml")]) == 2
    assert "no such file" in capsys.readouterr().err


def test_help_has_examples(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert "examples:" in out and "smdc init" in out and "doctor" in out


KEY = "AIzaGOODgoodGOODgoodGOODgoodGOOD0000000"


def test_init_tests_then_saves_the_key(tmp_path, monkeypatch, capsys):
    env = tmp_path / "conf" / ".env"
    monkeypatch.setenv("SMDC_ENV", str(env))
    monkeypatch.setenv("YOUTUBE_API_KEY", "")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: f" {KEY} ")
    seen = []
    monkeypatch.setattr(cli, "check_key", lambda name, key, budget: seen.append(key))
    assert cli.main(["--data", str(tmp_path / "d"), "init"]) == 0
    assert seen == [KEY] and f"YOUTUBE_API_KEY={KEY}" in env.read_text()
    out = capsys.readouterr().out
    assert KEY not in out and "next: smdc dashboard" in out


def test_init_refuses_a_bad_key_and_writes_nothing(tmp_path, monkeypatch, capsys):
    env = tmp_path / "conf" / ".env"
    monkeypatch.setenv("SMDC_ENV", str(env))
    monkeypatch.setenv("YOUTUBE_API_KEY", "")
    data = ["--data", str(tmp_path / "d"), "init"]
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "too short")
    assert cli.main(data) == 2 and not env.exists()
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: KEY)

    def refuse(name, key, budget):
        raise ValueError("YouTube says this API key is not valid")
    monkeypatch.setattr(cli, "check_key", refuse)
    assert cli.main(data) == 2 and not env.exists()
    captured = capsys.readouterr()
    assert "not valid" in captured.err and KEY not in captured.out + captured.err


def test_doctor_says_what_is_wrong_and_what_to_do(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SMDC_ENV", str(tmp_path / "conf" / ".env"))
    monkeypatch.setenv("YOUTUBE_API_KEY", "")
    data = ["--data", str(tmp_path / "d"), "doctor"]
    assert cli.main(data) == 1
    out = capsys.readouterr().out
    assert "ERROR no YouTube API key" in out and "smdc init" in out
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    assert cli.main(data) == 0
    assert "ok    YouTube API key found" in capsys.readouterr().out
    monkeypatch.setattr(cli, "check_key", lambda *a: (_ for _ in ()).throw(ValueError("not valid")))
    assert cli.main(data + ["--online"]) == 1
    assert "YouTube refuses the key" in capsys.readouterr().out


def test_doctor_names_a_run_past_its_date(tmp_path, monkeypatch, capsys):
    fake = FakeYouTube({"aaaaaaaaaaa": make_video("aaaaaaaaaaa", 20), "bbbbbbbbbbb": make_video("bbbbbbbbbbb", 5)})
    monkeypatch.setattr(cli, "get_source", lambda name, b: YouTubeSource("KEY", b, get=fake))
    monkeypatch.setenv("YOUTUBE_API_KEY", KEY)
    (tmp_path / "run.toml").write_text(TOML)
    data = ["--data", str(tmp_path / "d")]
    assert cli.main(data + ["run", str(tmp_path / "run.toml")]) == 0
    store = cli.Ctx(tmp_path / "d").store
    with store.tx() as db:
        db.execute("UPDATE snapshots SET finished_at = '2020-01-01T00:00:00Z'")
    capsys.readouterr()
    assert cli.main(data + ["doctor"]) == 0
    assert "overdue: cli-demo" in capsys.readouterr().out
