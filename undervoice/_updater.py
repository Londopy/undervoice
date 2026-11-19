"""Checks GitHub Releases for a newer undervoice and installs it when the user asks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

REPO = "Londopy/undervoice"
API = f"https://api.github.com/repos/{REPO}/releases"
PAGE = f"https://github.com/{REPO}/releases"
TIMEOUT = 30
VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:-?rc\.?(\d+))?")


class UpdateError(Exception):
    """A check or install failed; the message says why."""


@dataclass(frozen=True)
class Release:
    version: str
    assets: dict[str, str]
    url: str


def current() -> str:
    return version("undervoice")


def order(v: str) -> tuple[int, int, int, int, int]:
    """Sort key: 0.2.0rc1 comes before 0.2.0, which comes before 0.2.1."""
    m = VERSION_RE.fullmatch(v.strip())
    if not m:
        raise UpdateError(f"can't read version {v!r}")
    major, minor, patch, rc = m.groups()
    return int(major), int(minor), int(patch), 0 if rc else 1, int(rc or 0)


def _get(url: str) -> bytes:
    request = urllib.request.Request(
        url, headers={"User-Agent": f"undervoice/{current()}", "Accept": "*/*"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as reply:
            return bytes(reply.read())
    except (urllib.error.URLError, TimeoutError) as e:
        raise UpdateError(f"can't reach GitHub: {e}") from e


def _release(data: dict) -> Release:
    return Release(
        version=data["tag_name"].lstrip("v"),
        assets={a["name"]: a["browser_download_url"] for a in data.get("assets", [])},
        url=data.get("html_url", PAGE),
    )


def newer(channel: str = "stable") -> Release | None:
    """The newest release on the channel if it is newer than this copy, else None. The beta
    channel includes pre-releases."""
    try:
        if channel == "beta":
            found = [_release(d) for d in json.loads(_get(f"{API}?per_page=20")) if not d["draft"]]
        else:
            found = [_release(json.loads(_get(f"{API}/latest")))]
    except (KeyError, TypeError, ValueError) as e:
        raise UpdateError(f"GitHub sent a release list undervoice can't read: {e}") from e
    best = max(found, key=lambda r: order(r.version), default=None)
    if best and order(best.version) > order(current()):
        return best
    return None


def how_installed(exe: Path | None = None) -> str:
    """pip, msi, setup or portable, from what sits next to the running program."""
    if exe is None:
        if not getattr(sys, "frozen", False):
            return "pip"
        exe = Path(sys.executable)
    here = exe.parent
    ini = here / "install.ini"
    if ini.exists() and "source=msi" in ini.read_text(encoding="utf-8", errors="replace"):
        return "msi"
    if (here / "unins000.exe").exists():
        return "setup"
    return "portable"


def asset_for(kind: str, v: str) -> str:
    return {
        "msi": f"undervoice-{v}-x64.msi",
        "setup": f"undervoice-{v}-setup-x64.exe",
        "portable": f"undervoice-v{v}-x64-portable.exe",
    }[kind]


def download(release: Release, name: str, into: Path) -> Path:
    """Fetch one asset and check it against the release's SHA256SUMS.txt; a missing entry or
    a mismatch is an error, and nothing unverified is left on disk."""
    if name not in release.assets:
        raise UpdateError(f"release {release.version} has no {name}")
    if "SHA256SUMS.txt" not in release.assets:
        raise UpdateError(f"release {release.version} publishes no SHA256SUMS.txt")
    sums = _get(release.assets["SHA256SUMS.txt"]).decode("utf-8", errors="replace")
    expected = next(
        (line.split()[0].lower() for line in sums.splitlines() if line.split()[1:] == [name]),
        None,
    )
    if expected is None:
        raise UpdateError(f"SHA256SUMS.txt has no entry for {name}")
    data = _get(release.assets[name])
    if hashlib.sha256(data).hexdigest() != expected:
        raise UpdateError(f"{name} does not match its published SHA-256; not installing it")
    path = into / name
    path.write_bytes(data)
    return path


def _writable(folder: Path) -> bool:
    try:
        with tempfile.TemporaryFile(dir=folder):
            return True
    except OSError:
        return False


def _then_restart(command: str, exe: Path) -> None:
    # cmd waits for the installer, then starts the app again even if the user cancels UAC
    line = f'start "" /wait {command} & start "" "{exe}" --restarted'
    subprocess.Popen(
        ["cmd", "/d", "/s", "/c", line], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )


def install(release: Release) -> None:
    """Download, verify and start installing the release. The caller should quit right after,
    so the installer can replace the running files."""
    kind = how_installed()
    if kind == "pip":
        raise UpdateError("this copy came from pip: update it with pip install -U undervoice[live]")
    exe = Path(sys.executable)
    path = download(release, asset_for(kind, release.version), Path(tempfile.mkdtemp()))
    shared = not _writable(exe.parent)
    if kind == "portable":
        # Windows lets a running exe be renamed but not overwritten
        old = exe.with_name(exe.name + ".old")
        old.unlink(missing_ok=True)
        os.replace(exe, old)
        os.replace(path, exe)
        subprocess.Popen([str(exe), "--restarted"])
    elif kind == "setup":
        scope = "/ALLUSERS" if shared else "/CURRENTUSER"
        flags = f"/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS {scope}"
        _then_restart(f'"{path}" {flags}', exe)
    else:
        scope = 'ALLUSERS=2 MSIINSTALLPERUSER=""' if shared else ""
        _then_restart(f'msiexec /i "{path}" {scope} /passive', exe)


def tidy() -> None:
    """Remove the exe a portable update set aside last time."""
    if getattr(sys, "frozen", False):
        Path(sys.executable + ".old").unlink(missing_ok=True)
