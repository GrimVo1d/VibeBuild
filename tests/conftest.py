"""Pytest configuration and fixtures for VibeBuild tests."""

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
def mock_subprocess_run(mocker):
    """Mock subprocess.run for testing."""
    mock = mocker.patch("subprocess.run")
    mock.return_value.returncode = 0
    mock.return_value.stdout = ""
    mock.return_value.stderr = ""
    return mock


def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line("markers", "unit: mark test as unit test")
    config.addinivalue_line("markers", "integration: mark test as integration test")
    config.addinivalue_line("markers", "slow: mark test as slow running")
