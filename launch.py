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


def open_in_editor(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606 (opens Notepad or the default editor)
        elif sys.platform == "darwin":
            subprocess.run(["open", "-t", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError:
        pass


def user_env_path() -> Path:
    """Same as smdc.config.user_env_path (this script runs before smdc is installed)."""
    if os.environ.get("SMDC_ENV"):
        return Path(os.environ["SMDC_ENV"]).expanduser()
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "smdc" / ".env"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "smdc" / ".env"


def has_key(env: Path) -> bool:
    if not env.is_file():
        return False
    for line in env.read_text(encoding="utf-8").splitlines():
        k, _, v = line.partition("=")
        if k.strip() == "YOUTUBE_API_KEY" and v.strip():
            return True
    return False


def api_key_ready() -> bool:
    """True when a YouTube API key is set; otherwise creates/opens the user's .env and explains.

    The .env goes in the user's config folder (not this folder, which may be synced or shared);
    a .env left here by an older version still works.
    """
    if os.environ.get("YOUTUBE_API_KEY", "").strip() or has_key(HERE / ".env"):
        return True
    env = user_env_path()
    if has_key(env):
        return True
    if not env.exists():  # it will hold the key: readable by its owner only
        env.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(env, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write((HERE / ".env.example").read_text(encoding="utf-8"))
    print(f"\nNo YouTube API key yet. Opening {env}:\n"
          "paste your key after YOUTUBE_API_KEY= , save, and launch again.\n"
          "(How to get a key: README.md, section 'Get a YouTube API key'.)")
    open_in_editor(env)
    return False


def main() -> int:
    sys.stdout.reconfigure(line_buffering=True)  # keep our messages in order with pip's
    if sys.version_info < (3, 11):
        print(f"Python 3.11 or newer is required (this is {sys.version.split()[0]}).")
        return 1
    install()
    if not api_key_ready():
        return 1
    os.chdir(HERE)  # so the dashboard still finds a .env left here by an older version
    proc = subprocess.Popen([str(exe("smdc")), "dashboard", *sys.argv[1:]])
    try:
        return proc.wait()
    except KeyboardInterrupt:  # Ctrl-C reaches the dashboard too; let it pause cleanly
        return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
