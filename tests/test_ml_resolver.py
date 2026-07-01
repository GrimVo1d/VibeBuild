"""Tests for vibebuild.ml_resolver module."""

import pickle

import pytest

from vibebuild import ml_resolver
from vibebuild.exceptions import VibeBuildError
from vibebuild.ml_resolver import MLPackageResolver, default_model_path

# Sample training data that mimics real Fedora provides
SAMPLE_TRAINING_DATA = [
    {
        "provide": "python3dist(requests)",
        "rpm_name": "python3-requests",
        "srpm_name": "python-requests",
    },
    {
        "provide": "python3dist(urllib3)",
        "rpm_name": "python3-urllib3",
        "srpm_name": "python-urllib3",
    },
    {
        "provide": "python3dist(setuptools)",
        "rpm_name": "python3-setuptools",
        "srpm_name": "python-setuptools",
    },
    {"provide": "python3dist(pip)", "rpm_name": "python3-pip", "srpm_name": "python-pip"},
    {"provide": "python3dist(numpy)", "rpm_name": "python3-numpy", "srpm_name": "numpy"},
    {"provide": "python3dist(flask)", "rpm_name": "python3-flask", "srpm_name": "python-flask"},
    {"provide": "python3dist(jinja2)", "rpm_name": "python3-jinja2", "srpm_name": "python-jinja2"},
    {"provide": "pkgconfig(glib-2.0)", "rpm_name": "glib2-devel", "srpm_name": "glib2"},
    {"provide": "pkgconfig(gtk+-3.0)", "rpm_name": "gtk3-devel", "srpm_name": "gtk3"},
    {"provide": "pkgconfig(libxml-2.0)", "rpm_name": "libxml2-devel", "srpm_name": "libxml2"},
    {"provide": "pkgconfig(openssl)", "rpm_name": "openssl-devel", "srpm_name": "openssl"},
    {"provide": "pkgconfig(zlib)", "rpm_name": "zlib-devel", "srpm_name": "zlib"},
    {"provide": "perl(strict)", "rpm_name": "perl-interpreter", "srpm_name": "perl"},
    {"provide": "perl(File::Path)", "rpm_name": "perl-File-Path", "srpm_name": "perl-File-Path"},
    {"provide": "perl(JSON::PP)", "rpm_name": "perl-JSON-PP", "srpm_name": "perl-JSON-PP"},
    {"provide": "cmake(Qt5Core)", "rpm_name": "qt5-qtbase-devel", "srpm_name": "qt5-qtbase"},
    {
        "provide": "golang(github.com/stretchr/testify)",
        "rpm_name": "golang-github-stretchr-testify-devel",
        "srpm_name": "golang-github-stretchr-testify",
    },
    {"provide": "rubygem(rake)", "rpm_name": "rubygem-rake", "srpm_name": "rubygem-rake"},
    {"provide": "npm(express)", "rpm_name": "nodejs-express", "srpm_name": "nodejs-express"},
    {"provide": "python3dist(click)", "rpm_name": "python3-click", "srpm_name": "python-click"},
]


needs_sklearn = pytest.mark.skipif(not ml_resolver.HAS_SKLEARN, reason="scikit-learn missing")


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    """The default model location points into tmp_path, never at the real one."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    return tmp_path / "share"


@pytest.fixture
def trained():
    resolver = MLPackageResolver()
    resolver.train(SAMPLE_TRAINING_DATA)
    return resolver


@pytest.fixture
def model_file(trained, tmp_path):
    path = tmp_path / "model.npz"
    trained.save(str(path))
    return path


class TestDefaultModelPath:
    def test_xdg_data_home(self, data_home):
        assert default_model_path() == data_home / "vibebuild" / "model.npz"

    def test_local_share(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XDG_DATA_HOME")
        monkeypatch.setattr("vibebuild.ml_resolver.Path.home", lambda: tmp_path)

        assert default_model_path() == tmp_path / ".local/share/vibebuild/model.npz"


class TestWithoutModel:
    def test_not_available_without_model_file(self, tmp_path):
        resolver = MLPackageResolver(model_path=str(tmp_path / "missing.npz"))

        assert resolver.is_available() is False
        assert resolver.predict("python3dist(requests)") is None

    def test_default_path_is_lazy(self, data_home):
        resolver = MLPackageResolver()

        assert resolver._model_path == data_home / "vibebuild" / "model.npz"
        assert resolver._load_attempted is False

    def test_without_sklearn(self, mocker, tmp_path):
        mocker.patch("vibebuild.ml_resolver.HAS_SKLEARN", False)
        resolver = MLPackageResolver()

        assert resolver.is_available() is False
        with pytest.raises(VibeBuildError, match="scikit-learn"):
            resolver.train(SAMPLE_TRAINING_DATA)
        with pytest.raises(VibeBuildError, match="scikit-learn"):
            resolver.save(str(tmp_path / "m.npz"))
        with pytest.raises(VibeBuildError, match="scikit-learn"):
            resolver.load(str(tmp_path / "m.npz"))


@needs_sklearn
class TestTrainAndPredict:
    def test_empty_data(self):
        with pytest.raises(VibeBuildError, match="empty"):
            MLPackageResolver().train([])

    def test_exact_match(self, trained):
        assert trained.predict("python3dist(requests)") == {
            "rpm_name": "python3-requests",
            "srpm_name": "python-requests",
        }

    @pytest.mark.parametrize(
        ("provide", "srpm"),
        [("pkgconfig(glib-2.0)", "glib2"), ("perl(JSON::PP)", "perl-JSON-PP")],
    )
    def test_matches(self, trained, provide, srpm):
        assert trained.predict(provide)["srpm_name"] == srpm

    def test_garbage_is_below_threshold(self, trained):
        assert trained.predict("zzzzqqqq-xxxx-9999") is None

    def test_prefers_canonical_rpm_name(self):
        resolver = MLPackageResolver()
        resolver.train(
            [
                {"provide": "python3dist(foo)", "rpm_name": "python3.13-foo", "srpm_name": "foo"},
                {"provide": "python3dist(foo)", "rpm_name": "python3-foo", "srpm_name": "foo"},
                *SAMPLE_TRAINING_DATA,
            ]
        )

        assert resolver.predict("python3dist(foo)")["rpm_name"] == "python3-foo"

    def test_predictions_are_cached_in_memory(self, trained, mocker):
        first = trained.predict("python3dist(requests)")
        spy = mocker.spy(trained._nn_model, "kneighbors")

        assert trained.predict("python3dist(requests)") == first
        spy.assert_not_called()


@needs_sklearn
class TestSaveLoad:
    def test_roundtrip(self, trained, model_file):
        loaded = MLPackageResolver()
        loaded.load(str(model_file))

        for entry in SAMPLE_TRAINING_DATA:
            assert loaded.predict(entry["provide"]) == trained.predict(entry["provide"])

    def test_lazy_load_from_default_path(self, trained):
        trained.save(str(default_model_path()))
        resolver = MLPackageResolver()

        assert resolver.predict("pkgconfig(zlib)")["srpm_name"] == "zlib"

    def test_save_creates_parent_dirs(self, trained, tmp_path):
        path = tmp_path / "a" / "b" / "model.npz"
        trained.save(str(path))

        assert path.exists()

    def test_save_untrained(self, tmp_path):
        with pytest.raises(VibeBuildError, match="No model"):
            MLPackageResolver().save(str(tmp_path / "m.npz"))

    def test_load_missing(self, tmp_path):
        with pytest.raises(VibeBuildError, match="not found"):
            MLPackageResolver().load(str(tmp_path / "m.npz"))

    def test_load_refuses_pickled_objects(self, tmp_path):
        import numpy as np

        path = tmp_path / "evil.npz"
        np.savez(path, format=np.array([{"a": 1}], dtype=object))

        with pytest.raises(VibeBuildError, match="Cannot read model"):
            MLPackageResolver().load(str(path))

    def test_load_rejects_pickle_file(self, tmp_path):
        path = tmp_path / "model.npz"
        path.write_bytes(pickle.dumps({"vectorizer": None}))

        with pytest.raises(VibeBuildError, match="Cannot read model"):
            MLPackageResolver().load(str(path))

    def test_load_rejects_other_format(self, model_file):
        import numpy as np

        with np.load(model_file) as model:
            arrays = dict(model)
        arrays["format"] = np.array(99)
        np.savez(model_file, **arrays)

        with pytest.raises(VibeBuildError, match="Unsupported model format"):
            MLPackageResolver().load(str(model_file))

    def test_load_rejects_inconsistent_model(self, model_file):
        import numpy as np

        with np.load(model_file) as model:
            arrays = dict(model)
        arrays["rpm_names"] = arrays["rpm_names"][:-1]
        np.savez(model_file, **arrays)

        with pytest.raises(VibeBuildError, match="Inconsistent"):
            MLPackageResolver().load(str(model_file))

    def test_broken_model_makes_resolver_unavailable(self, tmp_path, caplog):
        path = tmp_path / "model.npz"
        path.write_bytes(b"not a model")

        assert MLPackageResolver(model_path=str(path)).is_available() is False
        assert "Failed to load model" in caplog.text
