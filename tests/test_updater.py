import hashlib
import json

import pytest

from undervoice import _updater
from undervoice._updater import (
    Release,
    UpdateError,
    asset_for,
    download,
    how_installed,
    newer,
    order,
)


def test_versions_order_like_releases():
    assert order("0.2.0rc1") < order("0.2.0") < order("v0.2.1") < order("1.0.0")
    assert order("0.2.0-rc2") > order("0.2.0rc1")
    with pytest.raises(UpdateError):
        order("latest")


def serve(monkeypatch, pages):
    monkeypatch.setattr(_updater, "current", lambda: "0.1.0")
    monkeypatch.setattr(_updater, "_get", lambda url: pages[url])


def release(tag, draft=False, prerelease=False):
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [],
        "html_url": "u",
    }


def test_stable_channel_reports_only_newer(monkeypatch):
    serve(monkeypatch, {f"{_updater.API}/latest": json.dumps(release("v0.2.0")).encode()})
    assert newer().version == "0.2.0"
    serve(monkeypatch, {f"{_updater.API}/latest": json.dumps(release("v0.1.0")).encode()})
    assert newer() is None


def test_beta_channel_takes_the_newest_including_prereleases(monkeypatch):
    listing = [
        release("v0.3.0rc1", prerelease=True),
        release("v0.2.0"),
        release("v9.0.0", draft=True),
    ]
    serve(monkeypatch, {f"{_updater.API}?per_page=20": json.dumps(listing).encode()})
    assert newer("beta").version == "0.3.0rc1"


def test_unreadable_reply_is_an_update_error(monkeypatch):
    serve(monkeypatch, {f"{_updater.API}/latest": b"<html>rate limited</html>"})
    with pytest.raises(UpdateError):
        newer()


def make_release(name, data, listed_hash):
    sums = f"{listed_hash}  {name}\n".encode()
    pages = {"sums": sums, "file": data}
    return Release("0.2.0", {name: "file", "SHA256SUMS.txt": "sums"}, "u"), pages


def test_download_checks_the_published_sha256(monkeypatch, tmp_path):
    name = asset_for("setup", "0.2.0")
    data = b"installer bytes"
    rel, pages = make_release(name, data, hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(_updater, "_get", lambda url: pages[url])
    assert download(rel, name, tmp_path).read_bytes() == data


def test_download_refuses_a_mismatch_and_leaves_nothing(monkeypatch, tmp_path):
    name = asset_for("setup", "0.2.0")
    rel, pages = make_release(name, b"tampered", "0" * 64)
    monkeypatch.setattr(_updater, "_get", lambda url: pages[url])
    with pytest.raises(UpdateError, match="SHA-256"):
        download(rel, name, tmp_path)
    assert not list(tmp_path.iterdir())


def test_download_needs_a_checksum_entry(monkeypatch, tmp_path):
    rel, pages = make_release("something-else.exe", b"x", "0" * 64)
    rel.assets["undervoice-0.2.0-setup-x64.exe"] = "file"
    monkeypatch.setattr(_updater, "_get", lambda url: pages[url])
    with pytest.raises(UpdateError, match="no entry"):
        download(rel, "undervoice-0.2.0-setup-x64.exe", tmp_path)


def test_how_installed_reads_the_install_folder(tmp_path):
    exe = tmp_path / "undervoice.exe"
    assert how_installed(exe) == "portable"
    (tmp_path / "unins000.exe").touch()
    assert how_installed(exe) == "setup"
    (tmp_path / "install.ini").write_text("[install]\nsource=msi\n", encoding="utf-8")
    assert how_installed(exe) == "msi"


def test_asset_names_match_the_release_downloads():
    assert asset_for("setup", "0.2.0") == "undervoice-0.2.0-setup-x64.exe"
    assert asset_for("msi", "0.2.0") == "undervoice-0.2.0-x64.msi"
    assert asset_for("portable", "0.2.0") == "undervoice-v0.2.0-x64-portable.exe"
