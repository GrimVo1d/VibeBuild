# VibeBuild

[![Python 3.12+](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

A command-line tool that builds an RPM package in Koji together with the build dependencies Koji does not have yet.

`koji build` fails as soon as one `BuildRequires` is missing from the build tag. VibeBuild reads the BuildRequires from the SRPM header, asks your Koji which of them are missing, downloads their SRPMs from Fedora Koji, does the same for their dependencies, and submits the whole chain level by level, dependencies first.

- Turns BuildRequires into package names: virtual provides (`python3dist(requests)`, `pkgconfig(glib-2.0)`, `perl(File::Path)`), RPM macros and RPM-to-SRPM names (`python3-requests` → `python-requests`). An optional ML model covers names the rules do not know.
- Verifies every downloaded SRPM against the digest Fedora Koji recorded for the build.
- Regenerates the build repo between levels itself, so it works on a hub without `kojira`.
- Skips packages that are already built in the destination tag, so an interrupted chain can simply be run again.

## Requirements

- Python 3.12 or newer, Linux.
- `rpm` to read SRPM headers, and the Koji Python library (`python3-koji` on Fedora).
- `rpmbuild` and `bwrap` (bubblewrap) for `--patch-specs`; Python 3.12.11+ or 3.13.4+ there, older releases have `tarfile` filter bypasses. Where bwrap works, `rpm` also reads every SRPM inside it.
- A Koji hub you can build on, with a client certificate. Fedora Koji is used anonymously, to download SRPMs.

## Installation

```bash
git clone https://github.com/GrimVo1d/VibeBuild.git
cd VibeBuild
python3 -m venv --system-site-packages .venv   # sees python3-koji and rpm from the system
. .venv/bin/activate
pip install .            # or ".[ml]" for the ML name resolver
```

## Configuration

VibeBuild reads the `[koji]` section of `~/.koji/config`, then `/etc/koji.conf`, the same files the `koji` CLI uses:

```ini
[koji]
server = https://koji.internal/kojihub
cert = ~/.koji/client.pem
serverca = ~/.koji/serverca.crt
target = f44
build_tag = f44-build
```

`--server`, `--cert`, `--serverca` and `--build-tag` override the file. `target` is the default build target when the command line names none. Without a `server`, VibeBuild talks to Fedora Koji.

## Usage

```
vibebuild [OPTIONS] [TARGET] SRPM
vibebuild train [OPTIONS]
```

`SRPM` is a path to a `.src.rpm` or a package name. A name is downloaded from Fedora Koji first: the latest build of the release that matches the target (`f44` → Fedora 44), or `--fedora-release`, or rawhide. Downloads are cached in `~/.cache/vibebuild/srpms`.

```bash
# Build a package and everything it needs; the target comes from ~/.koji/config
vibebuild python-requests

# Explicit target, local SRPM
vibebuild f44 my-package-1.0-1.fc44.src.rpm

# What is missing in the build tag, without building
vibebuild --analyze-only f44 python-requests

# The build plan, level by level, without submitting anything
vibebuild --dry-run python-requests

# Only download the SRPM
vibebuild --download-only python-requests

# Scratch build of the target; its missing dependencies are still regular builds
vibebuild --scratch python-requests

# Only this package, no dependency resolution
vibebuild --no-deps my-package.src.rpm
```

Other options:

| Option | Effect |
| --- | --- |
| `--build-all-deps` | Build every dependency locally, even if the tag's external repos provide it. On a real package the graph quickly reaches the toolchain and grows to hundreds of packages. |
| `--max-deps N` | Stop before building anything if more than N dependencies need building (default 100). |
| `--patch-specs` | Rebuild each SRPM with `%check` removed before submitting it, and retry once a build that fails on unpackaged files. Off by default: rebuilding runs `rpmbuild -bs`, which executes the spec's macros. It runs under bubblewrap; if bwrap cannot sandbox it, the build stops before anything is submitted. |
| `--allow-unsandboxed` | With `--patch-specs`, run `rpmbuild` without a sandbox, as the current user, where bwrap is missing or cannot create namespaces. |
| `--nowait` | Submit the target build and do not wait for it. |
| `--no-name-resolution` | Use BuildRequires as they are. |
| `--no-ml`, `--ml-model PATH` | Rules only, or a different ML model. |
| `-v`, `-q`, `--log-format json` | Log level and format. |

`vibebuild --help` lists all of them.

After a build VibeBuild prints a summary: how many packages were built, already built or failed, what this run built and tagged, the toolchains found in the BuildRequires, and every task with its status (`✓` built, `=` already built, `✗` failed, with the reason Koji gives). Already built packages are listed apart: nothing was submitted for them. A target left unbuilt because a dependency failed is listed separately. When a dry run finds nothing to build, it says how many BuildRequires are already available.

Exit codes: `0` on success, `1` when a build fails or Koji, the SRPM or the download is not usable, `2` on invalid arguments.

## ML fallback

Some BuildRequires do not map to a package name by any rule. For those VibeBuild can ask a nearest-neighbour model (character n-gram TF-IDF + KNN, scikit-learn) trained on the provides of a Fedora release. The rules handle most names; the model is only consulted when they fail.

The model is not part of the package. Train it once:

```bash
pip install ".[ml]"
vibebuild train --release 44                          # ~/.local/share/vibebuild/model.npz
vibebuild train --release 44 --save-data f44.json     # also keep the collected pairs
vibebuild train --data f44.json --output model.npz    # retrain without downloading
```

Training data come from the Fedora Everything repodata, checked by sha256 through the Fedora metalink. On Fedora 44 that is about 355,000 provide → package pairs and a 16 MB model; on a held-out set it names the right source package first in 65% of cases. The model is saved as plain NumPy arrays, without pickle. Without scikit-learn or a model VibeBuild works with the rules alone.

## Local Koji stand

`dev/koji-server` runs a complete Koji in Docker: PostgreSQL, hub with the web UI, a builder with mock, and a client container that runs VibeBuild straight from the working tree. The build tag gets the Fedora release and updates repos as external repos.

```bash
cd dev/koji-server
make setup                               # .env with generated secrets, certificates, all containers
docker compose exec vibebuild-dev bash   # the client
vibebuild --dry-run python-requests      # inside the client
```

The web UI is at <https://localhost:8443/koji> (published on 127.0.0.1 only). The stand has no `kojira`: VibeBuild creates repos itself, and `make regen-repo` does it by hand. The first repo after `make setup` takes a few minutes. `make init` re-runs the hub setup and is safe to repeat, `make stop` stops the containers, `make clean` also removes the volumes, certificates and `.env`. The Fedora release is `FEDORA_VERSION` in `.env`.

Each container mounts only the certificates it uses; the CA key stays on the host, and the client sees the checkout without the stand's `ssl/` and `.env`. The client container runs with Docker's seccomp profile and `/proc` masking turned off, so bubblewrap can sandbox rpm and `--patch-specs` inside it; to make up for that it runs as an ordinary user, not root, and mounts the working tree read-only, so an SRPM under test cannot touch the checkout or its git hooks. The hub has e-mail notifications disabled, since the stand has no SMTP server.

## Deploying Koji

`ansible/` installs Koji on a single Fedora 44 machine: PostgreSQL, a CA with server and client certificates, hub, web UI, a builder and the tags and target VibeBuild expects.

```bash
cd ansible
ansible-galaxy collection install -r requirements.yml
# inventory/hosts.ini: replace YOUR_SERVER_IP (and ansible_user if it is not fedora)
# group_vars/all.yml:  set koji_hub_fqdn
ansible-playbook -i inventory/hosts.ini playbook.yml
```

Passwords and secrets are generated on the first run into `ansible/credentials/`, which git ignores. Root on the server gets a `~/.koji/config` with the admin certificate. `kojira` is not installed; VibeBuild regenerates repos itself.

Everything lands on one machine, the builder included. Builds run untrusted SRPMs in mock, and membership in the `mock` group is as good as root, so a build that escapes its chroot reaches the CA key, the admin certificate and the database; the CA can sign a certificate for any user. That is acceptable for a personal Koji. For a shared one, put the builder on its own host and move the CA key off the hub once the certificates are issued.

## Development

```bash
python3 -m venv --system-site-packages .venv && . .venv/bin/activate
pip install -e ".[dev,ml]"
pre-commit install
pytest                   # unit tests with coverage; Koji, rpm and network are mocked
pre-commit run --all-files
```

Integration tests run against the local stand, inside the client container:

```bash
cd dev/koji-server
docker compose exec vibebuild-dev pytest -m integration
```

## Documentation

- [Getting started](docs/GETTING_STARTED.md): RPM, SRPM and Koji concepts and a first build step by step.
- [Architecture](docs/ARCHITECTURE.md): modules, the build flow, performance and the ML resolver.
