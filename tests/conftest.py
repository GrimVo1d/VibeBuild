"""
Pytest configuration and fixtures for VibeBuild tests.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock

# Make `import koji` work even without the package installed (macOS dev).
# In the container koji is installed from rpm, on the host it is a stub with a real Exception class
# (`koji.GenericError`) so that `except koji.GenericError` works.
if "koji" not in sys.modules:
    try:
        import koji  # noqa: F401
    except ImportError:
        _koji_stub = MagicMock()
        _koji_stub.GenericError = type("GenericError", (Exception,), {})
        sys.modules["koji"] = _koji_stub

import pytest


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch):
    """Do not sleep in the retry decorator during tests (otherwise the suite takes minutes)."""
    try:
        import vibebuild._retry as _r

        monkeypatch.setattr(_r.time, "sleep", lambda _s: None)
    except ImportError:
        pass


@pytest.fixture
def fixtures_dir():
    """Path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_spec(fixtures_dir):
    """Path to test-package.spec fixture file."""
    return fixtures_dir / "test-package.spec"


@pytest.fixture
def complex_spec(fixtures_dir):
    """Path to complex-package.spec fixture file."""
    return fixtures_dir / "complex-package.spec"


@pytest.fixture
def sample_spec_content():
    """Sample spec file content for testing."""
    return """
Name:           test-package
Version:        1.0
Release:        1%{?dist}
Summary:        Test package for VibeBuild

License:        MIT
URL:            https://example.internal/test-package
Source0:        %{name}-%{version}.tar.gz

BuildRequires:  python3-devel
BuildRequires:  gcc
BuildRequires:  make >= 4.0

%description
A test package for VibeBuild unit tests.

%prep
%autosetup

%build
%make_build

%install
%make_install

%files
%license LICENSE
%doc README.md
"""


@pytest.fixture
def mock_koji_client():
    """Mock KojiClient for testing without real Koji."""
    client = Mock()
    client.server = "https://test.koji.internal/kojihub"
    client.web_url = "https://test.koji.internal/koji"
    client.cert = None
    client.serverca = None
    client.list_packages.return_value = ["python3", "gcc", "make", "glibc"]
    client.package_exists.return_value = True
    client.list_tagged_builds.return_value = {
        "python3": "python3-3.11.0-1.fc40",
        "gcc": "gcc-13.0-1.fc40",
    }
    client.search_package.return_value = []
    client.has_external_repos.return_value = False
    return client


@pytest.fixture
def mock_subprocess_run(mocker):
    """Mock subprocess.run for testing."""
    mock = mocker.patch("subprocess.run")
    mock.return_value.returncode = 0
    mock.return_value.stdout = ""
    mock.return_value.stderr = ""
    return mock


@pytest.fixture(autouse=True)
def mock_koji_session(mocker):
    """Global stub for `vibebuild.builder.koji.ClientSession`, so that any
    test that calls `build_with_deps` / `build_package` does not go to a
    real koji-hub and does not hang in polling loops.

    The defaults are chosen so that the pipeline runs through without hanging:
      - newRepo returns task_id 999
      - getTaskInfo returns state=2 (closed): the task is instantly finished
      - build returns 12345
      - packageListAdd / uploadWrapper / ssl_login / logout — no-op

    A test can override any of these behaviors via
    `mock_koji_session.<method>.return_value = ...` or `.side_effect = ...`.
    """
    session = Mock()
    session.build.return_value = 12345
    session.newRepo.return_value = 999
    session.getTaskInfo.return_value = {"state": 2}  # closed
    session.listTasks.return_value = []
    session.listTaskOutput.return_value = {}
    session.downloadTaskOutput.return_value = b""
    session.packageListAdd.return_value = None
    session.uploadWrapper.return_value = None
    session.logout.return_value = None
    session.ssl_login.return_value = None
    session.cancelTask.return_value = None
    mocker.patch("vibebuild.builder.koji.ClientSession", return_value=session)
    return session


@pytest.fixture
def temp_srpm(tmp_path, sample_spec_content):
    """Create a temporary SRPM-like file for testing."""
    srpm = tmp_path / "test-package-1.0-1.src.rpm"
    srpm.write_text("fake srpm content")
    spec = tmp_path / "test-package.spec"
    spec.write_text(sample_spec_content)
    return srpm


@pytest.fixture
def mock_package_info():
    """Mock PackageInfo for testing."""
    from vibebuild.analyzer import BuildRequirement, PackageInfo

    return PackageInfo(
        name="test-package",
        version="1.0",
        release="1",
        build_requires=[
            BuildRequirement(name="python3-devel"),
            BuildRequirement(name="gcc"),
            BuildRequirement(name="make", version="4.0", operator=">="),
        ],
        source_urls=["https://example.internal/test-package-1.0.tar.gz"],
    )


@pytest.fixture
def mock_build_result():
    """Mock BuildResult for testing."""
    from vibebuild.builder import BuildResult, BuildStatus, BuildTask

    task = BuildTask(
        package_name="test-package",
        srpm_path="/path/to/test.src.rpm",
        target="fedora-target",
        task_id=12345,
        status=BuildStatus.COMPLETE,
        nvr="test-package-1.0-1.fc40",
    )
    return BuildResult(
        success=True,
        tasks=[task],
        built_packages=["test-package"],
        failed_packages=[],
        total_time=60.0,
    )


def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line("markers", "unit: mark test as unit test")
    config.addinivalue_line("markers", "integration: mark test as integration test")
    config.addinivalue_line("markers", "slow: mark test as slow running")


def pytest_collection_modifyitems(config, items):
    # Default classification: a test is unit unless explicitly marked
    # integration or slow. Lets `pytest -m unit` work as the docs promise
    # without forcing every test author to add the marker by hand.
    for item in items:
        explicit = {m.name for m in item.iter_markers()}
        if not explicit & {"integration", "slow"}:
            item.add_marker("unit")
