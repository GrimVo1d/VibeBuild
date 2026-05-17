"""
Pytest configuration and fixtures for VibeBuild tests.
"""

import pytest
from pathlib import Path
from unittest.mock import Mock



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


@pytest.fixture
def mock_subprocess_popen(mocker):
    """Mock subprocess.Popen for analyzer's rpm2cpio | cpio pipeline."""
    mock_popen_cls = mocker.patch("subprocess.Popen")

    def _make_proc(*args, **kwargs):
        proc = Mock()
        proc.returncode = 0
        proc.communicate.return_value = (b"", b"")
        proc.stdout = Mock()
        proc.stdout.close = Mock()
        return proc

    mock_popen_cls.side_effect = _make_proc
    return mock_popen_cls


@pytest.fixture
def mock_requests(mocker):
    """Mock requests library for testing HTTP calls."""
    mocker.patch("vibebuild.fetcher.HAS_REQUESTS", True)
    mock = mocker.patch("vibebuild.fetcher.requests")
    mock.get.return_value.status_code = 200
    mock.get.return_value.text = ""
    mock.get.return_value.json.return_value = {}
    return mock


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
    from vibebuild.analyzer import PackageInfo, BuildRequirement
    return PackageInfo(
        name="test-package",
        version="1.0",
        release="1",
        build_requires=[
            BuildRequirement(name="python3-devel"),
            BuildRequirement(name="gcc"),
            BuildRequirement(name="make", version="4.0", operator=">="),
        ],
        source_urls=["https://example.internal/test-package-1.0.tar.gz"]
    )


@pytest.fixture
def mock_build_result():
    """Mock BuildResult for testing."""
    from vibebuild.builder import BuildResult, BuildTask, BuildStatus
    task = BuildTask(
        package_name="test-package",
        srpm_path="/path/to/test.src.rpm",
        target="fedora-target",
        task_id=12345,
        status=BuildStatus.COMPLETE,
        nvr="test-package-1.0-1.fc40"
    )
    return BuildResult(
        success=True,
        tasks=[task],
        built_packages=["test-package"],
        failed_packages=[],
        total_time=60.0
    )


def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line("markers", "unit: mark test as unit test")
    config.addinivalue_line("markers", "integration: mark test as integration test")
    config.addinivalue_line("markers", "slow: mark test as slow running")
