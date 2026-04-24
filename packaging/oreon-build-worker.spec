%global _prefix /usr
%global _enable_debug_packages 0
%global _debugsource_packages 0
%global _debuginfo_subpackages 0
%global debug_package %{nil}

Name:           oreon-build-worker
Version:        1.0.0
Release:        52%{?dist}
Summary:        Oreon Build Service worker daemon
License:        GPLv3
URL:            https://github.com/oreon/oreon-build-service
Source0:        oreon-build-service-%{version}.tar.gz
Source1:        oreon-worker.env.example

BuildRequires:  python3-devel >= 3.11
BuildRequires:  python3-pip
BuildRequires:  python3-setuptools
BuildRequires:  python3-wheel
BuildRequires:  gcc
BuildRequires:  gcc-c++
BuildRequires:  openssl-devel
BuildRequires:  libffi-devel
BuildRequires:  libpq-devel
BuildRequires:  krb5-devel
BuildRequires:  openldap-devel

Requires:       python3 >= 3.11
Requires:       mock
Requires:       python3-pydantic
Requires:       rpmdevtools
Requires:       pigz
Requires:       rpmrebuild
Requires:       pesign
Requires:       git
Requires:       kf6-rpm-macros

%description
Worker daemon for Oreon Build Service.

%prep
%setup -q -n oreon_build_service-%{version}

%build
# Nothing

%install
mkdir -p %{buildroot}%{_prefix}/lib/oreon-build-worker
mkdir -p %{buildroot}%{_bindir}
mkdir -p %{buildroot}%{_sysconfdir}/oreon-build-worker
mkdir -p %{buildroot}%{_unitdir}

# Prefer wheels so pip does not recurse building hatchling from sdist. Force sdist for packages
# that ship manylinux wheels with bundled libcrypto/krb5/ldap (rpm autoreq then requires missing SONAMEs).
# bcrypt stays wheel-only to avoid Rust in the build chroot.
python3 -m pip install --no-compile --ignore-installed --prefer-binary \
  --only-binary bcrypt \
  --no-binary cryptography,cffi,psycopg2-binary \
  --target %{buildroot}%{_prefix}/lib/oreon-build-worker .

# Wrapper script: set PYTHONPATH and run worker (%{_prefix} expands to /usr)
cat > %{buildroot}%{_bindir}/oreon-worker << WRAPPER
#!/bin/sh
export PYTHONPATH="%{_prefix}/lib/oreon-build-worker"
exec python3 -m oreon_build.worker.main "\$@"
WRAPPER
chmod 755 %{buildroot}%{_bindir}/oreon-worker

# Config: env example (Source1 copied to SOURCES/ as oreon-worker.env.example when building)
install -m 644 %{SOURCE1} %{buildroot}%{_sysconfdir}/oreon-build-worker/oreon-worker.env.example

# systemd unit
cat > %{buildroot}%{_unitdir}/oreon-worker.service << 'UNIT'
[Unit]
Description=Oreon Build Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=oreon-build
Group=oreon-build
EnvironmentFile=-/etc/oreon-build-worker/oreon-worker.env
ExecStart=/usr/bin/oreon-worker
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

%pre
getent group oreon-build >/dev/null 2>&1 || groupadd -r oreon-build
getent passwd oreon-build >/dev/null 2>&1 || useradd -r -g oreon-build -s /sbin/nologin -d /var/lib/oreon-build-worker oreon-build

%post
# After install: drop pip-vendored pydantic trees so the system python3-pydantic package is used.
# (%post runs as root; same effect as the sudo rm commands below.)
rm -rf %{_prefix}/lib/oreon-build-worker/pydantic
rm -rf %{_prefix}/lib/oreon-build-worker/pydantic-*
rm -rf %{_prefix}/lib/oreon-build-worker/pydantic_core
rm -rf %{_prefix}/lib/oreon-build-worker/pydantic_core-*

%files
%{_bindir}/oreon-worker
%dir %{_prefix}/lib/oreon-build-worker
%{_prefix}/lib/oreon-build-worker/*
%config(noreplace) %{_sysconfdir}/oreon-build-worker/oreon-worker.env.example
%{_unitdir}/oreon-worker.service

%changelog
* Thu Mar 26 2026 Oreon HQ <packages@oreonhq.com> - 1.0.0-25
- pip prefer-binary plus no-binary cryptography,cffi,psycopg2-binary for system SSL/LDAP/KRB linkage

* Thu Mar 26 2026 Oreon HQ <packages@oreonhq.com> - 1.0.0-24
- pip install use --prefer-binary to avoid transitive sdist build failure in mock/install roots