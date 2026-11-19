"""Write the GitHub release page for a version: python scripts/release_notes.py X.Y.Z dist [PREV_TAG]"""

import hashlib
import re
import sys
from pathlib import Path

REPO = "https://github.com/Londopy/undervoice"


def section(version: str) -> str:
    # copied verbatim: patchnotes' text output indents bullets, which renders as a code block
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    match = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: )", text, re.M | re.S
    )
    if not match or not match.group(1).strip():
        sys.exit(f"CHANGELOG.md has no section for {version}")
    return match.group(1).strip()


def main() -> None:
    version, dist = sys.argv[1], Path(sys.argv[2])
    prev = sys.argv[3] if len(sys.argv) > 3 else ""
    files = sorted(p for p in dist.iterdir() if p.is_file())
    sums = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p in files]
    (dist / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n", encoding="utf-8")

    rows = [
        f"| [{p.name}]({REPO}/releases/download/v{version}/{p.name}) | {p.stat().st_size / 1024:.0f} KB |"
        for p in files
    ]
    changes = f"{REPO}/compare/{prev}...v{version}" if prev else f"{REPO}/commits/v{version}"
    body = "\n".join(
        [
            "**undervoice** hides a slow data channel (callsign, grid square and a short status) "
            f"under ordinary FM voice for amateur radio. See the [README]({REPO}#readme).",
            "",
            f"## What's new in {version}",
            "",
            section(version),
            "",
            "## Install",
            "",
            "```",
            f"pip install undervoice[live]=={version}",
            "```",
            "",
            "## Downloads",
            "",
            "| File | Size |",
            "|---|---|",
            *rows,
            "",
            "Check a download against `SHA256SUMS.txt` with `sha256sum -c SHA256SUMS.txt "
            "--ignore-missing`, or in PowerShell `(Get-FileHash <file>).Hash`.",
            "",
            "<details><summary>SHA-256 checksums</summary>",
            "",
            "```",
            *sums,
            "```",
            "",
            "</details>",
            "",
            "---",
            f"**Full changelog:** {changes}",
            "",
            f"[README]({REPO}#readme) · [Changelog]({REPO}/blob/main/CHANGELOG.md) · "
            f"[Report a problem]({REPO}/issues)",
        ]
    )
    Path("body.md").write_text(body + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
