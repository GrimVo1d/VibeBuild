"""Tests for vibebuild.fetcher module."""

import hashlib
import stat
import struct
from unittest.mock import MagicMock

import koji
import pytest

from vibebuild import fetcher
from vibebuild.exceptions import SRPMNotFoundError, VibeBuildError
from vibebuild.fetcher import SRPMFetcher, default_download_dir, srpm_digest

BUILD = {
    "build_id": 42,
    "name": "PyYAML",
    "version": "6.0.3",
    "release": "6.fc45",
    "nvr": "PyYAML-6.0.3-6.fc45",
}
URL = "https://kojipkgs.fedoraproject.org/packages/PyYAML/6.0.3/6.fc45/src/PyYAML-6.0.3-6.fc45.src.rpm"


@pytest.fixture
def hub(mocker):
    """The anonymous Fedora Koji session: PyYAML is the only package it knows."""
    session = MagicMock()
    session.getBuildTarget.return_value = {"build_tag_name": "f46-build"}
    session.getLatestBuilds.side_effect = lambda tag, package: (
        [BUILD] if package == "PyYAML" else []
    )
    session.getBuild.return_value = BUILD
    session.listRPMs.return_value = [{"arch": "src", "payloadhash": "good"}]
    mocker.patch("vibebuild.fetcher.koji.ClientSession", return_value=session)
    return session


@pytest.fixture
def http(mocker):
    """requests.get streaming a small body."""
    response = MagicMock()
    response.__enter__.return_value = response
    response.iter_content.return_value = [b"srpm ", b"bytes"]
    return mocker.patch("vibebuild.fetcher.requests.get", return_value=response)


@pytest.fixture
def digest(mocker):
    """srpm_digest: every file reads as the digest Koji recorded."""
    return mocker.patch("vibebuild.fetcher.srpm_digest", return_value="good")


class TestDownloadDir:
    def test_default_dir_follows_xdg_cache_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

        assert default_download_dir() == tmp_path / "vibebuild" / "srpms"

    def test_default_dir_is_private(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

        f = SRPMFetcher()

        assert f.download_dir == tmp_path / "vibebuild" / "srpms"
        assert stat.S_IMODE(f.download_dir.stat().st_mode) == 0o700

    def test_explicit_dir_is_created(self, tmp_path):
        f = SRPMFetcher(download_dir=str(tmp_path / "a" / "b"))

        assert f.download_dir.is_dir()


class TestDownloadSrpm:
    def test_downloads_latest_rawhide_build(self, tmp_path, hub, http, digest):
        path = SRPMFetcher(download_dir=str(tmp_path)).download_srpm("PyYAML")

        assert path == str(tmp_path / "PyYAML-6.0.3-6.fc45.src.rpm")
        assert (tmp_path / "PyYAML-6.0.3-6.fc45.src.rpm").read_bytes() == b"srpm bytes"
        hub.getLatestBuilds.assert_called_once_with("f46-build", package="PyYAML")
        hub.listRPMs.assert_called_once_with(buildID=42, arches="src")
        assert http.call_args.args[0] == URL
        assert list(tmp_path.glob(".download-*")) == []

    def test_release_looks_in_updates_first(self, tmp_path, hub, http, digest):
        SRPMFetcher(download_dir=str(tmp_path), fedora_release="44").download_srpm("PyYAML")

        hub.getLatestBuilds.assert_called_once_with("f44-updates", package="PyYAML")

    def test_release_falls_back_to_ga_tag(self, tmp_path, hub, http, digest):
        hub.getLatestBuilds.side_effect = lambda tag, package: [] if "updates" in tag else [BUILD]

        SRPMFetcher(download_dir=str(tmp_path), fedora_release="f44").download_srpm("PyYAML")

        assert [c.args[0] for c in hub.getLatestBuilds.call_args_list] == ["f44-updates", "f44"]

    def test_unknown_tag_is_skipped(self, tmp_path, hub, http, digest):
        def latest(tag, package):
            if "updates" in tag:
                raise koji.GenericError("No such tag")
            return [BUILD]

        hub.getLatestBuilds.side_effect = latest

        SRPMFetcher(download_dir=str(tmp_path), fedora_release="44").download_srpm("PyYAML")

    def test_specific_version(self, tmp_path, hub, http, digest):
        SRPMFetcher(download_dir=str(tmp_path)).download_srpm("PyYAML", version="6.0.3-6.fc45")

        hub.getBuild.assert_called_once_with("PyYAML-6.0.3-6.fc45")
        hub.getLatestBuilds.assert_not_called()

    def test_digest_mismatch_fails_and_leaves_nothing(self, tmp_path, hub, http, digest):
        digest.return_value = "evil"

        with pytest.raises(VibeBuildError, match="does not match Koji"):
            SRPMFetcher(download_dir=str(tmp_path)).download_srpm("PyYAML")

        assert list(tmp_path.iterdir()) == []

    def test_cached_file_is_reused_when_digest_matches(self, tmp_path, hub, http, digest):
        (tmp_path / "PyYAML-6.0.3-6.fc45.src.rpm").write_bytes(b"cached")

        SRPMFetcher(download_dir=str(tmp_path)).download_srpm("PyYAML")

        http.assert_not_called()

    def test_stale_cached_file_is_replaced(self, tmp_path, hub, http, digest):
        cached = tmp_path / "PyYAML-6.0.3-6.fc45.src.rpm"
        cached.write_bytes(b"stale")
        digest.side_effect = lambda path: "stale" if path.read_bytes() == b"stale" else "good"

        SRPMFetcher(download_dir=str(tmp_path)).download_srpm("PyYAML")

        assert cached.read_bytes() == b"srpm bytes"

    def test_symlink_in_cache_is_not_trusted(self, tmp_path, hub, http, digest):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.write_bytes(b"other")
        (tmp_path / "PyYAML-6.0.3-6.fc45.src.rpm").symlink_to(elsewhere)

        SRPMFetcher(download_dir=str(tmp_path)).download_srpm("PyYAML")

        http.assert_called_once()
        assert elsewhere.read_bytes() == b"other"

    def test_repeat_download_uses_memory_cache(self, tmp_path, hub, http, digest):
        f = SRPMFetcher(download_dir=str(tmp_path))
        f.download_srpm("PyYAML")
        f.download_srpm("PyYAML")

        assert hub.listRPMs.call_count == 1
        f.clear_cache()
        f.download_srpm("PyYAML")
        assert hub.listRPMs.call_count == 2

    def test_tries_name_resolver_candidates(self, tmp_path, hub, http, digest):
        resolver = MagicMock()
        resolver.get_download_candidates.return_value = ["python-pyyaml", "PyYAML"]

        path = SRPMFetcher(download_dir=str(tmp_path), name_resolver=resolver).download_srpm(
            "python3-pyyaml"
        )

        assert path.endswith("PyYAML-6.0.3-6.fc45.src.rpm")

    def test_not_found(self, tmp_path, hub, http, digest):
        with pytest.raises(SRPMNotFoundError, match=r"tried: \['nope'\]"):
            SRPMFetcher(download_dir=str(tmp_path)).download_srpm("nope")

    def test_unsafe_name_is_rejected_before_koji(self, tmp_path, hub, http, digest):
        with pytest.raises(SRPMNotFoundError, match="Invalid package name"):
            SRPMFetcher(download_dir=str(tmp_path)).download_srpm("../etc/passwd")

        hub.getLatestBuilds.assert_not_called()

    def test_unsafe_nvr_from_koji_is_rejected(self, tmp_path, hub, http, digest):
        hub.getLatestBuilds.side_effect = None
        hub.getLatestBuilds.return_value = [dict(BUILD, nvr="../../x")]

        with pytest.raises(SRPMNotFoundError, match="Unexpected build name"):
            SRPMFetcher(download_dir=str(tmp_path)).download_srpm("PyYAML")

    def test_build_without_srpm(self, tmp_path, hub, http, digest):
        hub.listRPMs.return_value = []

        with pytest.raises(SRPMNotFoundError, match="has no SRPM"):
            SRPMFetcher(download_dir=str(tmp_path)).download_srpm("PyYAML")

    def test_koji_error_is_not_found(self, tmp_path, hub, http, digest):
        hub.getBuildTarget.side_effect = OSError("Connection refused")

        with pytest.raises(SRPMNotFoundError, match="Koji lookup for PyYAML failed"):
            SRPMFetcher(download_dir=str(tmp_path)).download_srpm("PyYAML")

    def test_rawhide_target_missing(self, tmp_path, hub, http, digest):
        hub.getBuildTarget.return_value = None

        with pytest.raises(SRPMNotFoundError, match="no rawhide target"):
            SRPMFetcher(download_dir=str(tmp_path)).download_srpm("PyYAML")

    def test_always_verifies_tls(self, tmp_path, hub, http, digest, mocker):
        cls = mocker.patch("vibebuild.fetcher.koji.ClientSession", return_value=hub)

        SRPMFetcher(download_dir=str(tmp_path)).download_srpm("PyYAML")

        assert "no_ssl_verify" not in cls.call_args.kwargs["opts"]
        assert "verify" not in http.call_args.kwargs


class TestDownloadFile:
    def test_refuses_plain_http(self, tmp_path, http):
        f = SRPMFetcher(download_dir=str(tmp_path))

        with pytest.raises(VibeBuildError, match="plain HTTP"):
            with open(tmp_path / "out", "wb") as out:
                f._download_file("http://example.internal/x.src.rpm", out)

        http.assert_not_called()

    def test_size_limit(self, tmp_path, http, monkeypatch):
        monkeypatch.setattr(fetcher, "MAX_SRPM_BYTES", 6)
        f = SRPMFetcher(download_dir=str(tmp_path))

        with pytest.raises(VibeBuildError, match="larger than 6 bytes"):
            with open(tmp_path / "out", "wb") as out:
                f._download_file(URL, out)

    def test_transient_error_is_retried(self, tmp_path, http):
        ok = http.return_value
        http.side_effect = [ConnectionError("Connection reset"), ok]
        f = SRPMFetcher(download_dir=str(tmp_path))

        with open(tmp_path / "out", "wb") as out:
            f._download_file(URL, out)

        assert (tmp_path / "out").read_bytes() == b"srpm bytes"
        assert http.call_count == 2


def _rpm(header_and_payload: bytes, signature: bytes = b"sig") -> bytes:
    """A minimal RPM: lead, signature header with one entry, header and payload."""
    lead = b"\xed\xab\xee\xdb" + bytes(92)
    sig = b"\x8e\xad\xe8\x01" + bytes(4) + struct.pack(">II", 1, len(signature))
    sig += bytes(16) + signature
    return lead + sig + bytes(-len(signature) % 8) + header_and_payload


class TestSrpmDigest:
    def test_md5_of_header_and_payload(self, tmp_path):
        path = tmp_path / "x.src.rpm"
        path.write_bytes(_rpm(b"header and payload"))

        assert srpm_digest(path) == hashlib.md5(b"header and payload").hexdigest()

    def test_signature_header_does_not_count(self, tmp_path):
        a, b = tmp_path / "a.src.rpm", tmp_path / "b.src.rpm"
        a.write_bytes(_rpm(b"same", signature=b"one"))
        b.write_bytes(_rpm(b"same", signature=b"another signature"))

        assert srpm_digest(a) == srpm_digest(b)

    def test_appended_bytes_change_the_digest(self, tmp_path):
        path = tmp_path / "x.src.rpm"
        path.write_bytes(_rpm(b"payload"))
        before = srpm_digest(path)
        with path.open("ab") as f:
            f.write(b"x")

        assert srpm_digest(path) != before

    def test_not_an_rpm(self, tmp_path):
        path = tmp_path / "x.src.rpm"
        path.write_bytes(b"<html>404</html>")

        with pytest.raises(VibeBuildError, match="Not an RPM file"):
            srpm_digest(path)

    def test_unreadable(self, tmp_path):
        with pytest.raises(VibeBuildError, match="Cannot read"):
            srpm_digest(tmp_path / "missing.src.rpm")
