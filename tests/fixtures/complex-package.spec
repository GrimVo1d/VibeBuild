Name:           complex-package
Version:        2.0.0
Release:        1%{?dist}
Summary:        Complex test package for VibeBuild

License:        GPLv2+
URL:            https://example.internal/complex-package
Source0:        https://example.internal/%{name}-%{version}.tar.gz

BuildRequires:  python3-setuptools, python3-wheel
BuildRequires:  gcc, gcc-c++
BuildRequires:  cmake >= 3.14

%description
A complex test package with comma-separated BuildRequires.

%prep
%autosetup

%build
%cmake
%cmake_build

%install
%cmake_install

%files
%license LICENSE
%doc README.md
