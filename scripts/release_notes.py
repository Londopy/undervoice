"""Write the GitHub release page, SHA256SUMS.txt and a torrent per download for a release.

    python scripts/release_notes.py v0.2.0 dist --prev v0.1.0 --out body.md

Everything in dist/ goes on the release. Exits 1 when CHANGELOG.md has no notes for the
version: better a failed release than one with empty notes.
"""

import argparse
import hashlib
import os
import re
import sys
import time
import urllib.parse

try:
    import segno  # optional: QR codes for the magnet links
except ImportError:
    segno = None

APP = "undervoice"
REPO = "Londopy/undervoice"
INTRO = (
    "**undervoice** hides a slow data channel (callsign, grid square and a short status) under "
    "ordinary FM voice for amateur radio. See the [README](https://github.com/{repo}#readme) "
    "for setup."
)

# (regex on the file name, who it's for, notes). The first match wins, and the tables follow
# this order: the Windows downloads, then the Python packages.
KINDS = [
    (
        r"-setup-x64\.exe$",
        "Windows 10/11, 64-bit",
        "**installer (recommended)**, per-user, no admin prompt",
    ),
    (r"-x64\.msi$", "Windows 10/11, 64-bit", "MSI; per-user by default, all-users for IT"),
    (r"-x64-portable\.exe$", "Windows 10/11, 64-bit", "one exe, nothing to install"),
    (r"-portable\.zip$", "Windows 10/11, 64-bit", "the same exe; settings stay next to it"),
    (
        r"^install\.ps1$",
        "Windows PowerShell",
        "script install, picks the right build and checks it",
    ),
    (r"^install\.cmd$", "Windows", "double-click version of install.ps1"),
    (r"\.whl$", "Python, any OS", "what `pip install undervoice` gets from PyPI"),
    (r"\.tar\.gz$", "Python, any OS", "source"),
]
# in SHA256SUMS.txt, but not rows of their own in the download table
HIDDEN = re.compile(r"(\.torrent|\.magnet\.png|^SHA256SUMS\.txt)$")
# no torrents for these: tiny, and the scripts fetch the latest release anyway
NO_TORRENT = re.compile(r"(\.torrent|\.png|\.txt|^install\.(ps1|cmd))$")

# public trackers help magnet links find peers; the web seed covers no-peer cases
TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]
PIECE = 1 << 20  # 1 MiB pieces
HEADING = re.compile(r"^## +\[?([^\]\s]+)\]?[^\n]*$", re.M)


def bencode(x: object) -> bytes:
    if isinstance(x, bool):
        raise TypeError("no booleans in bencoding")
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, bytes):
        return b"%d:" % len(x) + x
    if isinstance(x, list):
        return b"l" + b"".join(bencode(i) for i in x) + b"e"
    if isinstance(x, dict):
        return b"d" + b"".join(bencode(k) + bencode(x[k]) for k in sorted(x)) + b"e"
    raise TypeError(type(x))


def make_torrent(path: str, name: str, url: str, comment: str) -> tuple[bytes, str, int]:
    """Single-file torrent with `url` as its web seed. Returns (bytes, infohash, size).

    One torrent per file on purpose: GitHub redirects every download to its CDN, and per-file
    redirects only work cleanly when each torrent is one file.
    """
    pieces, size = [], 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(PIECE), b""):
            pieces.append(hashlib.sha1(chunk).digest())
            size += len(chunk)
    info = {"name": name, "length": size, "piece length": PIECE, "pieces": b"".join(pieces)}
    infohash = hashlib.sha1(bencode(info)).hexdigest()
    meta = {
        "info": info,
        "url-list": [url],
        "announce": TRACKERS[0],
        "announce-list": [[t] for t in TRACKERS],
        "creation date": int(time.time()),
        "created by": "scripts/release_notes.py",
        "comment": comment,
    }
    return bencode(meta), infohash, size


def magnet_link(infohash: str, name: str, size: int, url: str) -> str:
    parts = [f"xt=urn:btih:{infohash}", f"dn={urllib.parse.quote(name)}", f"xl={size}"]
    parts += [f"tr={urllib.parse.quote(t, safe='')}" for t in TRACKERS]
    parts.append(f"ws={urllib.parse.quote(url, safe='')}")
    return "magnet:?" + "&".join(parts)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def kind(name: str) -> tuple[int, str, str]:
    """(place in KINDS, who it's for, notes) for a file name."""
    for i, (pattern, label, note) in enumerate(KINDS):
        if re.search(pattern, name):
            return i, label, note
    return len(KINDS), "Other", ""


def rank(name: str) -> tuple[int, str]:
    return kind(name)[0], name


def find(names: list[str], pattern: str) -> str | None:
    return next((n for n in names if re.search(pattern, n)), None)


def changelog_section(text: str, version: str) -> tuple[str, str] | None:
    """(date, body) of the `## [version]` section, or None when it is missing or empty.

    The body is copied verbatim rather than taken from `patchnotes ... show`, whose text output
    indents bullets, which a release page renders as a code block.
    """
    matches = list(HEADING.finditer(text))
    for i, m in enumerate(matches):
        if m.group(1).lstrip("vV") != version:
            continue
        date = re.search(r"\d{4}-\d{2}-\d{2}", m.group(0))
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end]
        # drop the compare-link footnotes that end a Keep a Changelog file, and a trailing "---"
        body = re.sub(r"^\[[^\]]+\]:\s*\S+\s*$", "", body, flags=re.M)
        body = re.sub(r"\n-{3,}\s*$", "", body.rstrip()).strip()
        return ((date.group(0) if date else ""), body) if body else None
    return None


def listing(folder: str) -> list[str]:
    return sorted(
        f
        for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f)) and f != "SHA256SUMS.txt"
    )


def add_torrents(folder: str, download: str, tag: str) -> list[tuple[str, str, str, str]]:
    """Write a .torrent and, with segno, a magnet QR code next to each download.

    Returns (file, torrent file, magnet, QR file or "") per download.
    """
    out = []
    for f in listing(folder):
        if NO_TORRENT.search(f):
            continue
        url = f"{download}/{f}"
        comment = f"{APP} {tag}: {url} is the web seed"
        data, infohash, size = make_torrent(os.path.join(folder, f), f, url, comment)
        with open(os.path.join(folder, f + ".torrent"), "wb") as fh:
            fh.write(data)
        magnet = magnet_link(infohash, f, size, url)
        qr = ""
        if segno is not None:
            qr = f + ".magnet.png"
            segno.make(magnet, error="m").save(os.path.join(folder, qr), scale=3, border=2)
        out.append((f, f + ".torrent", magnet, qr))
    return sorted(out, key=lambda t: rank(t[0]))


def write_sums(folder: str) -> list[tuple[str, str, int]]:
    """Write SHA256SUMS.txt over every file, torrents and QR codes included.

    Returns (file, digest, size) per file.
    """
    sums = []
    for f in listing(folder):
        path = os.path.join(folder, f)
        sums.append((f, sha256(path), os.path.getsize(path)))
    with open(os.path.join(folder, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\n") as fh:
        fh.writelines(f"{digest}  {f}\n" for f, digest, _ in sums)
    return sums


def downloads(sums: list[tuple[str, str, int]], download: str) -> list[str]:
    out = ["## Downloads", "", "| File | For | Size | Notes |", "|---|---|---|---|"]
    for f, _, size in sorted(sums, key=lambda row: rank(row[0])):
        if HIDDEN.search(f):
            continue
        _, label, note = kind(f)
        out.append(f"| [`{f}`]({download}/{f}) | {label} | {human(size)} | {note} |")
    return out + [""]


def install(names: list[str], download: str, repo: str, version: str) -> list[str]:
    def link(name: str) -> str:
        return f"[`{name}`]({download}/{name})"

    setup = find(names, r"-setup-x64\.exe$")
    msi = find(names, r"-x64\.msi$")
    exe = find(names, r"-x64-portable\.exe$")
    zipped = find(names, r"-portable\.zip$")
    out = ["## Install", ""]
    if setup:
        out += [
            f"**Installer (recommended):** download {link(setup)} and run it. It installs for "
            "your user only, so there is no admin prompt. The binaries are not code-signed: if "
            'SmartScreen says "Windows protected your PC", click **More info**, then '
            "**Run anyway**.",
            "",
        ]
    if msi:
        out += [
            f"**MSI:** {link(msi)}. Per-user by default. For every user on the PC (admin): "
            f'`msiexec /i {msi} ALLUSERS=2 MSIINSTALLPERUSER=""`. Add `DESKTOPSHORTCUT=0` to '
            "skip the desktop icon.",
            "",
        ]
    portable = []
    if exe:
        portable.append(f"{link(exe)} is the whole app in one exe")
    if zipped:
        portable.append(f"{link(zipped)} has the same exe in a folder that keeps its settings")
    if portable:
        out += ["**Portable:** " + "; ".join(portable) + ".", ""]
    out += [
        "**PowerShell:** picks the right build for your PC, checks it, and installs it:",
        "",
        "```powershell",
        f"irm https://github.com/{repo}/releases/latest/download/install.ps1 | iex",
        "```",
        "",
        "**Python**, on any OS: installs the `undervoice` command and the window, which "
        "`undervoice-gui` opens.",
        "",
        "```",
        f'pip install "undervoice[live]=={version}"',
        "undervoice-gui",
        "```",
        "",
    ]
    return out


def verify(names: list[str], sums: list[tuple[str, str, int]]) -> list[str]:
    example = find(names, r"-setup-x64\.exe$") or "FILE"
    out = [
        "## Verify a download",
        "",
        "`SHA256SUMS.txt` lists the SHA-256 of every file on this release. In PowerShell, next "
        "to the download:",
        "",
        "```powershell",
        f"(Get-FileHash .\\{example} -Algorithm SHA256).Hash.ToLower()",
        "```",
        "",
        "and compare it with that file's line below. On Linux or macOS: "
        "`sha256sum -c SHA256SUMS.txt --ignore-missing`.",
        "",
        "<details><summary>Checksums</summary>",
        "",
        "```",
    ]
    out += [f"{digest}  {f}" for f, digest, _ in sums]
    return out + ["```", "", "</details>", ""]


def torrents_section(torrents: list[tuple[str, str, str, str]], download: str) -> list[str]:
    if not torrents:
        return []
    out = [
        "## Torrents",
        "",
        "Every download is also a `.torrent` with its GitHub download as the web seed: a "
        "BitTorrent client gets it from GitHub when nobody is seeding, and from peers when they "
        "are. The magnet links carry the same web seed, but need one peer to fetch the file "
        "list first.",
        "",
        "| File | Torrent | Magnet (QR) |",
        "|---|---|---|",
    ]
    for f, tfile, _, qr in torrents:
        cell = ""
        if qr:
            cell = (
                f'<img src="{download}/{qr}" width="88" alt="QR code of the magnet link for {f}">'
            )
        out.append(f"| `{f}` | [`{tfile}`]({download}/{tfile}) | {cell} |")
    out += ["", "<details><summary>Magnet links</summary>", "", "```"]
    for f, _, magnet, _ in torrents:
        out += [f"# {f}", magnet, ""]
    return out + ["```", "", "</details>", ""]


def footer(repo: str, tag: str, prev: str) -> list[str]:
    if prev:
        changes = f"https://github.com/{repo}/compare/{prev}...{tag}"
    else:
        changes = f"https://github.com/{repo}/commits/{tag}"
    return [
        "---",
        "",
        f"**Full changelog:** {changes}",
        "",
        f"[README](https://github.com/{repo}#readme) · "
        f"[Changelog](https://github.com/{repo}/blob/main/CHANGELOG.md) · "
        f"[Report a problem](https://github.com/{repo}/issues/new/choose)",
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Build the release page")
    ap.add_argument("tag")
    ap.add_argument("assets")
    ap.add_argument("--prev", default="", help="previous tag, for the compare link")
    ap.add_argument("--changelog", default="CHANGELOG.md")
    ap.add_argument("--out", default="body.md")
    ap.add_argument("--repo", default=REPO)
    a = ap.parse_args()
    tag, version = a.tag, a.tag.lstrip("vV")
    # asset links must be absolute: a relative link in a release body resolves under
    # /releases/tag/ and 404s
    download = f"https://github.com/{a.repo}/releases/download/{tag}"

    with open(a.changelog, encoding="utf-8") as fh:
        section = changelog_section(fh.read(), version)
    if section is None:
        print(f"release_notes: {a.changelog} has no notes for {version}", file=sys.stderr)
        return 1
    date, changes = section

    torrents = add_torrents(a.assets, download, tag)
    sums = write_sums(a.assets)
    names = [f for f, _, _ in sums]

    out = [INTRO.format(repo=a.repo), ""]
    out += [f"## What's new in {version}" + (f" ({date})" if date else ""), "", changes, ""]
    out += downloads(sums, download)
    out += install(names, download, a.repo, version)
    out += verify(names, sums)
    out += torrents_section(torrents, download)
    out += footer(a.repo, tag, a.prev)

    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(out) + "\n")
    title = os.path.join(os.path.dirname(os.path.abspath(a.out)), "title.txt")
    with open(title, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{APP} {tag}\n")
    qr_note = "" if segno else ", no QR codes: segno not installed"
    print(
        f"wrote {a.out}, title.txt and SHA256SUMS.txt "
        f"({len(sums)} files, {len(torrents)} torrents{qr_note})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
