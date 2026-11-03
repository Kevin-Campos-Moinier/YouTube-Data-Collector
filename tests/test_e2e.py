"""The dashboard's journeys by real clicks in Chrome, on the in-memory YouTube: no key, no quota, no network.

Slow and in need of Chrome and Node 22, so they run only when asked: SMDC_E2E=1 python -m pytest tests/test_e2e.py
"""

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent / "e2e"
CHROME = next(filter(None, map(shutil.which, ("google-chrome", "chromium", "chromium-browser", "chrome"))), None)
pytestmark = pytest.mark.skipif(not (os.environ.get("SMDC_E2E") and CHROME and shutil.which("node")),
                                reason="set SMDC_E2E=1; needs Chrome and Node 22")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_journeys(tmp_path):
    flag = tmp_path / "quota-reset"
    procs, ports = [], {}
    # name, the key set beforehand, YouTube refusing after this many calls
    for name, key, fail_after in (("full", "KEY", 0), ("low", "KEY", 12), ("nokey", "", 0)):
        ports[name] = free_port()
        env = {**os.environ, "SMDC_ENV": str(tmp_path / name / "conf" / ".env"), "YOUTUBE_API_KEY": key}
        procs.append(subprocess.Popen([sys.executable, str(HERE / "fake_dashboard.py"), str(ports[name]),
                                       str(tmp_path / name / "data"), str(fail_after), str(flag)], env=env))
    try:
        for port in ports.values():
            for _ in range(100):
                with socket.socket() as s:
                    if s.connect_ex(("127.0.0.1", port)) == 0:
                        break
                time.sleep(.1)
        env = {**os.environ, "CHROME": CHROME, "RESET_FLAG": str(flag), "ENV_FILE": str(tmp_path / "nokey" / "conf" / ".env"),
               "SHOTS": os.environ.get("SMDC_E2E_SHOTS", "")}
        r = subprocess.run(["node", str(HERE / "flows.mjs"), str(ports["full"]), str(ports["low"]), str(ports["nokey"])],
                           env=env, capture_output=True, text=True, timeout=600)
        print(r.stdout)
        assert r.returncode == 0, "\n".join(ln for ln in r.stdout.splitlines() if not ln.startswith("ok"))
    finally:
        for p in procs:
            p.terminate()
            p.wait()
