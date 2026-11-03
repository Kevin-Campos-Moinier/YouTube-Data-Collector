"""Installs smdc on first use, then starts the dashboard. Works on Windows, macOS, Linux.

You normally don't run this directly: double-click `dashboard.bat` (Windows) or
`dashboard.command` (macOS), or run `./dashboard.sh` (Linux). Those find a Python >= 3.11
and call this script. Arguments are passed to `smdc dashboard` (e.g. --port 8800).

Environment variables:
    SMDC_VENV: where the Python environment goes (default ~/.venvs/smdc).
    SMDC_DATA: where runs are stored (default ~/smdc-data).
Both default to folders outside the code folder: an environment breaks when its folder is
synced or moved, and a database written by two computers through a synced folder (OneDrive,
Dropbox, iCloud) gets corrupted.
"""

import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = Path(os.environ.get("SMDC_VENV") or Path.home() / ".venvs" / "smdc")
BIN = VENV / ("Scripts" if os.name == "nt" else "bin")


def exe(name: str) -> Path:
    return BIN / (name + (".exe" if os.name == "nt" else ""))


def install() -> None:
    """Creates the environment and installs the package when missing or outdated."""
    stamp = VENV / ".smdc-installed"
    want = hashlib.sha256((HERE / "pyproject.toml").read_bytes()).hexdigest()
    alive = exe("python").exists() and subprocess.run([str(exe("python")), "-c", "import pip"],
                                                      capture_output=True).returncode == 0
    if alive and exe("smdc").exists() and stamp.exists() and stamp.read_text() == want:
        return
    if not alive:
        # Missing, or built for a Python that is no longer installed (after a system upgrade):
        # the environment is disposable, rebuild it in place.
        print(f"Creating the Python environment in {VENV} ...")
        try:
            venv.create(VENV, with_pip=True, clear=exe("python").exists() or (VENV / "pyvenv.cfg").exists())
        except (OSError, subprocess.CalledProcessError) as e:
            sys.exit(f"Could not create the environment ({e}).\n"
                     "On Debian/Ubuntu, install the venv module first: sudo apt install python3-venv")
    print("Installing smdc and its dependencies (first launch or update, a few minutes)...")
    pip = [str(exe("python")), "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    subprocess.run(pip + ["--upgrade", "pip"], check=True)
    subprocess.run(pip + ["-e", f"{HERE}[all]"], check=True)
    stamp.write_text(want)


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # keep our messages in order with pip's
    if sys.version_info < (3, 11):
        print(f"Python 3.11 or newer is required (this is {sys.version.split()[0]}).")
        return 1
    install()  # the dashboard asks for the API key itself, on its first page
    os.chdir(HERE)  # so the dashboard still finds a .env left here by an older version
    proc = subprocess.Popen([str(exe("smdc")), "dashboard", *sys.argv[1:]])
    try:
        return proc.wait()
    except KeyboardInterrupt:  # Ctrl-C reaches the dashboard too; let it pause cleanly
        return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
