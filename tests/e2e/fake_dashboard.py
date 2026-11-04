"""The real dashboard on the test suite's in-memory YouTube, slowed down so that a run can be paused. For flows.mjs.

Usage: fake_dashboard.py PORT DATA_DIR FAIL_AFTER RESET_FLAG
    FAIL_AFTER: YouTube answers quotaExceeded after this many calls (0: never), until the file RESET_FLAG exists.
A key starting with "bad" is refused. Whether a key is already set comes from the environment, as in real use.
"""
import os
import sys
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parent.parent / "src")]

from fakeyt import FakeYouTube, make_video  # noqa: E402
from smdc import net, pipeline  # noqa: E402
from smdc.budget import Budget  # noqa: E402
from smdc.dashboard import server  # noqa: E402
from smdc.sources.youtube import YouTubeSource  # noqa: E402
from smdc.store import Store  # noqa: E402

port, data, fail_after, flag = int(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), Path(sys.argv[4])
data.mkdir(parents=True, exist_ok=True)
fake = FakeYouTube({"aaaaaaaaaaa": make_video("aaaaaaaaaaa", 120), "bbbbbbbbbbb": make_video("bbbbbbbbbbb", 30),
                    **{f"v{n:010d}": make_video(f"v{n:010d}", 1 + n % 7, replies=lambda i: 0) for n in range(30)},  # a long run
                    "vdisabled00": {**make_video("vdisabled00", 1), "disabled": True}})
calls = [0]
DELAY = float(os.environ.get("FAKE_DELAY", ".12"))  # .03: a run over in a second or two, as a small real one is


def slow(*a, **k):
    time.sleep(DELAY)
    calls[0] += 1
    if fail_after and calls[0] > fail_after and not flag.exists():
        raise net.HttpError(403, "quotaExceeded")
    return fake(*a, **k)


def check_key(name, key, budget):
    if key.startswith("bad"):
        raise ValueError("YouTube says this API key is not valid: check that you copied all of it.")


class Ctx:
    def __init__(self):
        self.data, self.store = data, Store(data / "smdc.sqlite")

    def job_factory(self, run_id):
        b = Budget(self.store, "youtube", 10_000)
        return (lambda stop: pipeline.Job(self.store, run_id, YouTubeSource("KEY", b, get=slow), b, b"s", stop)), b


server.get_source = lambda name, b: YouTubeSource("KEY", b, get=fake)
server.check_key = check_key
ThreadingHTTPServer(("127.0.0.1", port), server.make_handler(server.Api(Ctx()))).serve_forever()
