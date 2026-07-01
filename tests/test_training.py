"""Tests for vibebuild.training module."""

import gzip
import hashlib
import io
import json

import pytest
import requests

from vibebuild import ml_resolver, training
from vibebuild.exceptions import VibeBuildError

REPOMD = b"""<?xml version="1.0"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <data type="filelists">
    <checksum type="sha256">aaaa</checksum>
    <location href="repodata/filelists.xml.zst"/>
  </data>
  <data type="primary">
    <checksum type="sha256">%s</checksum>
    <location href="repodata/abc-primary.xml.gz"/>
  </data>
</repomd>
"""

PRIMARY = b"""<?xml version="1.0"?>
<metadata xmlns="http://linux.duke.edu/metadata/common"
          xmlns:rpm="http://linux.duke.edu/metadata/rpm" packages="3">
  <package type="rpm">
    <name>python3-requests</name><arch>noarch</arch>
    <format>
      <rpm:sourcerpm>python-requests-2.31.0-1.fc44.src.rpm</rpm:sourcerpm>
      <rpm:provides>
        <rpm:entry name="python3-requests"/>
        <rpm:entry name="python3dist(requests)"/>
        <rpm:entry name="python3dist(requests)"/>
        <rpm:entry name="python3.14-requests"/>
      </rpm:provides>
    </format>
  </package>
  <package type="rpm">
    <name>zlib-ng-compat</name><arch>x86_64</arch>
    <format>
      <rpm:sourcerpm>zlib-ng-2.2.4-1.fc44.src.rpm</rpm:sourcerpm>
      <rpm:provides>
        <rpm:entry name="libz.so.1()(64bit)"/>
        <rpm:entry name="pkgconfig(zlib)"/>
        <rpm:entry name="/usr/lib64/libz.so.1"/>
      </rpm:provides>
    </format>
  </package>
  <package type="rpm">
    <name>hello</name><arch>src</arch>
    <format><rpm:provides><rpm:entry name="hello(src)"/></rpm:provides></format>
  </package>
</metadata>
"""


def _metalink(digest="d" * 64, urls=("https://m1/os/repodata/repomd.xml",)):
    resources = "".join(f'<url protocol="x">{u}</url>' for u in urls)
    return (
        '<metalink xmlns="http://www.metalinker.org/"><files><file name="repomd.xml">'
        f'<verification><hash type="md5">x</hash><hash type="sha256">{digest}</hash>'
        f"</verification><resources>{resources}</resources></file></files></metalink>"
    ).encode()


class TestHelpers:
    @pytest.mark.parametrize(
        ("sourcerpm", "name"),
        [
            ("python-requests-2.31.0-1.fc40.src.rpm", "python-requests"),
            ("zlib-ng-2.2.4-1.fc44.src.rpm", "zlib-ng"),
            ("odd", "odd"),
        ],
    )
    def test_srpm_name(self, sourcerpm, name):
        assert training.srpm_name(sourcerpm) == name

    @pytest.mark.parametrize(
        ("provide", "rpm", "expected"),
        [
            ("python3dist(requests)", "python3-requests", True),
            ("pkgconfig(zlib)", "zlib-devel", True),
            ("python3.14-requests", "python3-requests", False),
            ("perl-Foo", "perl-Foo-Bar", True),
            ("perl-Foo", "perl-Foo", False),
            ("/usr/bin/foo", "foo", False),
            ("libz.so.1()(64bit)", "zlib", False),
            ("", "foo", False),
            ("plainname", "foo", False),
        ],
    )
    def test_is_interesting_provide(self, provide, rpm, expected):
        assert training.is_interesting_provide(provide, rpm) is expected


class TestParsePrimary:
    def test_mappings(self):
        mappings = training.parse_primary(io.BytesIO(PRIMARY))

        assert mappings == [
            {
                "provide": "python3dist(requests)",
                "rpm_name": "python3-requests",
                "srpm_name": "python-requests",
            },
            {"provide": "pkgconfig(zlib)", "rpm_name": "zlib-ng-compat", "srpm_name": "zlib-ng"},
        ]

    def test_gzip_stream(self, tmp_path):
        path = tmp_path / "primary"
        path.write_bytes(gzip.compress(PRIMARY))

        with training._decompressed(path) as stream:
            assert len(training.parse_primary(stream)) == 2

    def test_unknown_compression(self, tmp_path):
        path = tmp_path / "primary"
        path.write_bytes(b"BZh9")

        with pytest.raises(VibeBuildError, match="neither gzip nor zstd"):
            with training._decompressed(path):
                pass


class TestMirrors:
    def test_repomd_mirrors_keeps_https_only(self, mocker):
        urls = ("http://a/repodata/repomd.xml", "https://b/repodata/repomd.xml", "rsync://c/x")
        get = mocker.patch("vibebuild.training._get", return_value=_metalink(urls=urls))

        digest, mirrors = training.repomd_mirrors("44", "x86_64")

        assert digest == "d" * 64
        assert mirrors == ["https://b/repodata/repomd.xml"]
        assert "repo=fedora-44&arch=x86_64" in get.call_args.args[0]

    def test_rawhide_repo(self, mocker):
        get = mocker.patch("vibebuild.training._get", return_value=_metalink())

        training.repomd_mirrors("rawhide", "aarch64")

        assert "repo=rawhide&arch=aarch64" in get.call_args.args[0]

    @pytest.mark.parametrize(
        "content",
        [b"<not xml", _metalink(urls=("http://a/repodata/repomd.xml",)), b"<metalink/>"],
    )
    def test_unusable_metalink(self, mocker, content):
        mocker.patch("vibebuild.training._get", return_value=content)

        with pytest.raises(VibeBuildError):
            training.repomd_mirrors("44", "x86_64")

    def test_fetch_repomd_skips_bad_mirrors(self, mocker):
        good = b"<repomd/>"
        mocker.patch(
            "vibebuild.training.repomd_mirrors",
            return_value=(
                hashlib.sha256(good).hexdigest(),
                [
                    "https://down/os/repodata/repomd.xml",
                    "https://stale/os/repodata/repomd.xml",
                    "https://good/os/repodata/repomd.xml",
                ],
            ),
        )
        mocker.patch(
            "vibebuild.training._get",
            side_effect=[VibeBuildError("down"), b"<repomd>old</repomd>", good],
        )

        assert training.fetch_repomd("44", "x86_64") == ("https://good/os", good)

    def test_fetch_repomd_no_good_mirror(self, mocker):
        mocker.patch(
            "vibebuild.training.repomd_mirrors",
            return_value=("0" * 64, ["https://m/os/repodata/repomd.xml"]),
        )
        mocker.patch("vibebuild.training._get", return_value=b"<repomd/>")

        with pytest.raises(VibeBuildError, match="No mirror"):
            training.fetch_repomd("44", "x86_64")

    def test_get_refuses_http(self):
        with pytest.raises(VibeBuildError, match="plain HTTP"):
            training._get("http://example.internal/x")

    def test_get_wraps_request_errors(self, mocker):
        mocker.patch.object(
            training._session, "get", side_effect=requests.ConnectionError("refused")
        )

        with pytest.raises(VibeBuildError, match="Cannot fetch"):
            training._get("https://example.internal/x")


class TestPrimary:
    def test_primary_location(self):
        assert training.primary_location(REPOMD % b"beef") == (
            "repodata/abc-primary.xml.gz",
            "beef",
        )

    def test_primary_location_missing(self):
        with pytest.raises(VibeBuildError, match="no primary"):
            training.primary_location(b'<repomd xmlns="http://linux.duke.edu/metadata/repo"/>')

    def _serve(self, mocker, body):
        resp = mocker.MagicMock()
        resp.__enter__.return_value = resp
        resp.iter_content.return_value = [body[:10], body[10:]]
        return mocker.patch.object(training._session, "get", return_value=resp)

    def test_download_checks_sha256(self, mocker, tmp_path):
        self._serve(mocker, PRIMARY)
        dest = tmp_path / "primary"

        training.download_primary(
            "https://m/os", "p.xml", hashlib.sha256(PRIMARY).hexdigest(), dest
        )

        assert dest.read_bytes() == PRIMARY

    def test_download_sha256_mismatch(self, mocker, tmp_path):
        self._serve(mocker, PRIMARY)

        with pytest.raises(VibeBuildError, match="sha256 mismatch"):
            training.download_primary("https://m/os", "p.xml", "0" * 64, tmp_path / "p")

    def test_download_size_limit(self, mocker, tmp_path):
        self._serve(mocker, PRIMARY)
        mocker.patch("vibebuild.training.MAX_PRIMARY_BYTES", 16)

        with pytest.raises(VibeBuildError, match="larger than"):
            training.download_primary("https://m/os", "p.xml", "0" * 64, tmp_path / "p")

    def test_download_refuses_http(self, tmp_path):
        with pytest.raises(VibeBuildError, match="plain HTTP"):
            training.download_primary("http://m/os", "p.xml", "0" * 64, tmp_path / "p")


class TestCollect:
    def test_collect(self, mocker):
        body = gzip.compress(PRIMARY)
        mocker.patch(
            "vibebuild.training.fetch_repomd",
            return_value=("https://m/os", REPOMD % hashlib.sha256(body).hexdigest().encode()),
        )

        def download(base_url, href, digest, dest):
            assert (base_url, href) == ("https://m/os", "repodata/abc-primary.xml.gz")
            dest.write_bytes(body)

        mocker.patch("vibebuild.training.download_primary", side_effect=download)

        assert len(training.collect("44", "x86_64")) == 2

    def test_collect_nothing(self, mocker):
        mocker.patch("vibebuild.training.fetch_repomd", return_value=("https://m", REPOMD % b"x"))
        mocker.patch(
            "vibebuild.training.download_primary",
            side_effect=lambda *a: a[3].write_bytes(gzip.compress(b"<metadata/>")),
        )

        with pytest.raises(VibeBuildError, match="no training data"):
            training.collect("44", "x86_64")


class TestTrainingData:
    def test_load(self, tmp_path):
        path = tmp_path / "data.json"
        record = {"provide": "p(x)", "rpm_name": "x", "srpm_name": "x"}
        path.write_text(json.dumps([record]))

        assert training.load_training_data(str(path)) == [record]

    @pytest.mark.parametrize("content", ["{not json", '{"a": 1}', '[{"provide": "x"}]'])
    def test_load_invalid(self, tmp_path, content):
        path = tmp_path / "data.json"
        path.write_text(content)

        with pytest.raises(VibeBuildError):
            training.load_training_data(str(path))

    def test_load_missing(self, tmp_path):
        with pytest.raises(VibeBuildError, match="Cannot read"):
            training.load_training_data(str(tmp_path / "nope.json"))


@pytest.mark.skipif(not ml_resolver.HAS_SKLEARN, reason="scikit-learn missing")
class TestTrain:
    def test_train_evaluate_save(self, tmp_path):
        data = [
            {"provide": f"python3dist(pkg{i})", "rpm_name": f"python3-pkg{i}", "srpm_name": f"p{i}"}
            for i in range(40)
        ]
        output = tmp_path / "model.npz"

        metrics = training.train(data, str(output), test_split=0.25, eval_sample=5)

        assert metrics["train_size"] == 30
        assert metrics["test_size"] == 10
        assert 0.0 <= metrics["coverage"] <= 1.0
        assert output.exists()

    def test_small_data_has_no_test_split(self, tmp_path):
        data = [{"provide": "pkgconfig(zlib)", "rpm_name": "zlib-devel", "srpm_name": "zlib"}]

        metrics = training.train(data, str(tmp_path / "m.npz"))

        assert metrics == {"train_size": 1, "test_size": 0}
