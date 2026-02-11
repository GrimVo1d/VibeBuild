Name:           test-package
Version:        1.0.0
Release:        1%{?dist}
Summary:        Test package for VibeBuild

License:        MIT
URL:            https://example.internal/test-package
Source0:        https://example.internal/%{name}-%{version}.tar.gz

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
