"""Text processing, target parsing, retries, config."""

import io
import os
import urllib.error

import pytest

from smdc import config, export, net, retention, sources, text
from smdc.privacy import load_secret, pseudonymize, redact_mentions
from smdc.sources.youtube import classify_target, explain_http_error, parse_video_id


def test_non_latin_scripts_survive_emoji_removal():
    # The original regex (U+24C2..U+1F251) erased these entirely.
    for s in ("这个游戏看起来很棒", "게임 정말 재밌어요", "ゲームが楽しみ", "لعبة رائعة"):
        assert text.clean(s) == s


def test_emoji_and_symbols_removed_as_word_breaks():
    assert text.clean("great🔥game 👍🏽 ❤️ 1️⃣") == "great game 1"
    assert text.clean("🇫🇷🇫🇷") == ""


def test_punctuation_unicode_and_contractions():
    assert text.clean("don't «stop» — ok… l’homme!!") == "don't stop ok l'homme"
    assert text.clean("'quoted'") == "quoted"


def test_normalize_keeps_everything_on_one_line():
    assert text.normalize("a\r\nb\n\n  c d") == "a b c d"


def test_short_english_fallback_matches_uppercase_abbreviations():
    # Keys like 'GG' or 'IDK' never matched in the original (tokens were lowercased).
    assert text.is_english_short_text("gg wp")
    assert text.is_english_short_text("IDK man")
    assert not text.is_english_short_text("GG")          # abbreviation alone: ambiguous
    assert text.is_english_short_text("so woke")


def test_process_reasons():
    cfg = config.Process()
    out = text.process(["This game looks great", "c'est vraiment nul ce jeu", "🔥🔥", "10/10",
                        "lol ok", "@@someone yes you"], cfg)
    reasons = [r[4] for r in out]
    assert reasons == [None, "other_language", "empty_after_cleaning", "undetected", None, None]
    assert out[5][0] == "@user yes you"


def test_no_language_filter_keeps_nonempty():
    out = text.process(["c'est nul", "🔥"], config.Process(languages=[]))
    assert [r[4] for r in out] == [None, "empty_after_cleaning"]


@pytest.mark.parametrize("s", [
    "Gs069dndIYk", "https://www.youtube.com/watch?v=Gs069dndIYk&t=10s", "youtu.be/Gs069dndIYk",
    "https://m.youtube.com/shorts/Gs069dndIYk", "https://www.youtube.com/live/Gs069dndIYk?si=x",
    "https://www.youtube-nocookie.com/embed/Gs069dndIYk"])
def test_parse_video_id(s):
    assert parse_video_id(s) == "Gs069dndIYk"


def test_parse_video_id_rejects():
    assert parse_video_id("https://example.com/watch?v=Gs069dndIYk") is None
    assert parse_video_id("too-short") is None


def test_pseudonymize_stable_and_keyed():
    a = pseudonymize(b"s1", "youtube", "UCabc")
    assert a == pseudonymize(b"s1", "youtube", "UCabc") and len(a) == 16
    assert a != pseudonymize(b"s2", "youtube", "UCabc")
    assert pseudonymize(b"s1", "youtube", None) is None
    assert redact_mentions("@a.b-c hi, mail me@x.org") == "@user hi, mail me@x.org"


def test_config_validation(tmp_path):
    with pytest.raises(ValueError, match="unknown key"):
        config.from_dict({"name": "x", "targets": ["a"], "collect": {"max_item": 3}})
    with pytest.raises(ValueError, match="order"):
        config.from_dict({"name": "x", "targets": ["a"], "collect": {"order": "time_oldest"}})
    cfg = config.from_dict({"name": "x", "targets": ["a"], "collect": {"since": "2024-05-01"}})
    assert cfg.collect.since == "2024-05-01T00:00:00Z"
    p = tmp_path / "r.toml"
    p.write_text(config.dump_toml(cfg))
    assert config.load(p) == cfg


def test_http_retries_transient_then_succeeds(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b"{}"))
        return io.BytesIO(b'{"ok": 1}')

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(net.time, "sleep", lambda s: None)
    assert net.get_json("https://x/y", {"key": "SECRET"}) == {"ok": 1}
    assert len(calls) == 3


def test_http_non_retryable_reason_and_no_key_leak(monkeypatch):
    body = b'{"error": {"code": 403, "errors": [{"reason": "commentsDisabled"}]}}'

    def fake_urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(body))

    monkeypatch.setattr(net.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(net.HttpError) as e:
        net.get_json("https://x/y", {"key": "SECRET"})
    assert e.value.reason == "commentsDisabled" and "SECRET" not in str(e.value)


def test_excel_csv_defuses_formulas(tmp_path):
    rows = [{"text": "=HYPERLINK(\"http://x\")", "n": -3}, {"text": "@user hi", "n": 1}, {"text": "ok", "n": 2}]
    export._csv(tmp_path / "x.csv", rows, ["text", "n"], excel=True)
    body = (tmp_path / "x.csv").read_text(encoding="utf-8-sig")
    assert "'=HYPERLINK" in body and "'@user hi" in body and ";-3" in body and "\nok;2" in body
    export._csv(tmp_path / "y.csv", rows, ["text", "n"], excel=False)  # analysis CSV: untouched
    assert (tmp_path / "y.csv").read_text(encoding="utf-8").splitlines()[1].startswith('"=HYPERLINK')


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_secret_and_data_folder_are_private(tmp_path, monkeypatch):
    monkeypatch.delenv("SMDC_SECRET", raising=False)
    load_secret(tmp_path / "data")
    assert (tmp_path / "data").stat().st_mode & 0o077 == 0
    assert (tmp_path / "data" / ".secret").stat().st_mode & 0o077 == 0


def test_user_env_path(monkeypatch, tmp_path):
    monkeypatch.setenv("SMDC_ENV", str(tmp_path / "k.env"))
    assert config.user_env_path() == tmp_path / "k.env"
    monkeypatch.delenv("SMDC_ENV")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert config.user_env_path() == tmp_path / "cfg" / "smdc" / ".env"


def test_load_dotenv_first_file_wins(monkeypatch, tmp_path):
    user, old = tmp_path / "user.env", tmp_path / "old.env"
    old.write_text("YOUTUBE_API_KEY=old\n")
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    config.load_dotenv(user, old)               # no user file yet: the old one is read
    assert os.environ["YOUTUBE_API_KEY"] == "old"
    monkeypatch.delenv("YOUTUBE_API_KEY")
    user.write_text("YOUTUBE_API_KEY=new\n")
    config.load_dotenv(user, old)
    assert os.environ["YOUTUBE_API_KEY"] == "new"


@pytest.mark.parametrize("line,want", [
    ("aaaaaaaaaaa", ("video", "aaaaaaaaaaa")),
    ("W2Wnvvj33Wo   # a comment", ("video", "W2Wnvvj33Wo")),
    ("https://youtu.be/bbbbbbbbbbb", ("video", "bbbbbbbbbbb")),
    ("channel:@PlayStation", ("channel", "@PlayStation")),
    ("@PlayStation", ("channel", "@PlayStation")),
    ("https://www.youtube.com/@name", ("channel", "@name")),
    ("https://www.youtube.com/channel/UCabc", ("channel", "UCabc")),
    ("youtube.com/playlist?list=PLx", ("playlist", "PLx")),
    ("playlist:PL1", ("playlist", "PL1")),
    ("search:star wars", ("search", "star wars")),
    ("# just a note", (None, None)),
    ("   ", (None, None)),
    ("search:", ("invalid", None)),
    ("channel:", ("invalid", None)),
    ("playlist:", ("invalid", None)),
    ("not a video !!", ("invalid", None)),
    ("https://example.com/x", ("invalid", None)),
    ("http://[bad", ("invalid", None)),
    ("https://www.youtube.com/channel", ("invalid", None)),
])
def test_classify_target_needs_no_key_and_no_network(line, want):
    assert classify_target(line) == want


def test_classify_targets_says_which_line_is_wrong():
    rows = sources.classify_targets("youtube", ["aaaaaaaaaaa", "", "oops", "search:x"], allow_search=False)
    assert [r["kind"] for r in rows] == ["video", None, None, "search"]
    assert "'oops'" in rows[2]["error"] and "allow search" in rows[3]["error"] and rows[0]["error"] is None


def test_validate_targets_refuses_a_bad_line():
    cfg = config.from_dict({"name": "x", "targets": ["aaaaaaaaaaa", "oops"]})
    with pytest.raises(ValueError, match="oops"):
        sources.validate_targets(cfg)


# A real answer from the API to a made-up key.
BAD_KEY = {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "API key not valid.",
                     "errors": [{"reason": "badRequest"}], "details": [{"reason": "API_KEY_INVALID"}]}}


def test_errors_say_what_to_do():
    assert "not valid" in explain_http_error(net.HttpError(400, "badRequest", BAD_KEY))
    assert "enable it" in explain_http_error(net.HttpError(403, "accessNotConfigured"))
    assert "restrictions" in explain_http_error(net.HttpError(403, "ipRefererBlocked"))
    assert "midnight" in explain_http_error(net.HttpError(403, "quotaExceeded"))
    assert "internet" in explain_http_error(net.HttpError(0, "URLError"))
    assert "418" in explain_http_error(net.HttpError(418, "teapot"))


def test_missing_key_message_gives_the_way_out(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="dashboard"):
        sources.get_source("youtube", None)


def test_save_api_key_keeps_other_lines_and_is_private(tmp_path, monkeypatch):
    env = tmp_path / "conf" / ".env"
    monkeypatch.setenv("SMDC_ENV", str(env))
    monkeypatch.setenv("YOUTUBE_API_KEY", "")
    env.parent.mkdir()
    env.write_text("# a note\nYOUTUBE_API_KEY=old\nSMDC_SECRET=s3\n")
    assert config.save_api_key("newkey") == env
    lines = env.read_text().splitlines()
    assert "YOUTUBE_API_KEY=newkey" in lines and "YOUTUBE_API_KEY=old" not in lines
    assert "# a note" in lines and "SMDC_SECRET=s3" in lines
    assert os.environ["YOUTUBE_API_KEY"] == "newkey"
    if os.name == "posix":
        assert env.stat().st_mode & 0o777 == 0o600
    assert not env.with_name(".env.tmp").exists()


def test_delete_by_is_thirty_days_after_the_snapshot():
    assert retention.delete_by("2026-01-01T10:00:00Z") == "2026-01-31"
    assert retention.delete_by(None) is None
