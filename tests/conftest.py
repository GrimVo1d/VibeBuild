"""
Pytest configuration and fixtures for VibeBuild tests.
"""

import sys
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
    client.serverca = None
    client.list_packages.return_value = ["python3", "gcc", "make", "glibc"]
    client.package_exists.return_value = True
    client.list_tagged_builds.return_value = {
        "python3": "python3-3.11.0-1.fc40",
        "gcc": "gcc-13.0-1.fc40",
    }
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
def mock_koji_session(request, mocker):
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
    Integration tests talk to the real hub and get no stub.
    """
    if request.node.get_closest_marker("integration"):
        return None
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
    session.getLoggedInUser.return_value = {"name": "kojiadmin"}
    session.getLatestBuilds.return_value = []
    session.listTagged.return_value = []
    mocker.patch("vibebuild.koji_client.koji.ClientSession", return_value=session)
    return session
