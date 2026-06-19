# VibeBuild

**Koji extension for automatic dependency resolution and building**

[![Python 3.10+](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Conventional Commits](https://img.shields.io/badge/Conventional%20Commits-1.0.0-yellow.svg)](https://www.conventionalcommits.org/)

VibeBuild extends Koji with automatic dependency resolution. When building a package, VibeBuild finds the missing BuildRequires, downloads their SRPMs from Fedora and builds the whole dependency chain in the right order.

## Features

- **Automatic dependency analysis** — parses SRPM/spec files to extract BuildRequires
- **Smart package name resolution** — automatically converts virtual provides (`python3dist(requests)`, `pkgconfig(glib-2.0)`, `perl(File::Path)`) and RPM macros (`%{python3_pkgversion}`) into real package names
- **ML name resolution** — an optional scikit-learn model (TF-IDF + KNN) as a fallback when the rules do not match
- **SRPM download** — automatically downloads missing packages from Fedora Koji with smart SRPM name mapping (for example, `python3-requests` -> `python-requests`)
- **DAG construction** — determines the build order from dependencies
- **Build orchestration** — sequential builds that wait for the repository to regenerate
- **CLI** — a convenient command-line tool with flexible options

## Quick start

### Installation

Clone the repository and install in editable mode:

```bash
cd vibebuild
pip install -e ".[dev,ml]"   # development + full ML
pip install -e ".[dev]"      # without ML
pip install -e ".[ml]"       # ML only, no dev tools
pip install -e .             # minimum
```

### Usage

Basic form: `vibebuild [OPTIONS] SRPM` or `vibebuild [OPTIONS] TARGET SRPM`.
SRPM can be a path to a `.src.rpm` file or a **package name** (for example, `python3`).
If TARGET is not given, it is taken from the `target` key in `~/.koji/config [koji]`.

```bash
# One command: target from ~/.koji/config, download the SRPM by name and build
vibebuild python-requests
vibebuild python3

# Explicit target (or if the target is not in the config)
vibebuild fedora-target python-requests
vibebuild fedora-target my-package-1.0-1.fc40.src.rpm

# Scratch build (no tagging)
vibebuild --scratch fedora-target my-package.src.rpm

# Build without dependency resolution (a single package only)
vibebuild --no-deps fedora-target my-package.src.rpm

# Dependency analysis without building (one argument: path to an SRPM)
vibebuild --analyze-only my-package.src.rpm

# Download an SRPM from Fedora by package name (requires the koji CLI)
vibebuild --download-only python-requests

# Dry run — show the build order and what will be built
vibebuild --dry-run my-package.src.rpm

# Name resolution options
vibebuild --no-ml fedora-target my-package.src.rpm              # rules only
vibebuild --no-name-resolution fedora-target my-package.src.rpm # raw names
vibebuild --ml-model /path/to/model.joblib fedora-target my-package.src.rpm
```

A step-by-step demo (download, analysis, dry-run, build) is in the "First build: step by step" section of [QUICKSTART.md](docs/QUICKSTART.md#7-first-build-step-by-step).

### Verification

After installing, you can check that everything works:

```bash
vibebuild --version
vibebuild --help          # short list of the main options
vibebuild --help-all      # full list of all options
```

If `[dev]` is installed, run the tests from the project root:

```bash
pytest
```

To check dependency resolution on a real package (requires the `koji` CLI and access to Fedora Koji):

```bash
vibebuild --download-only python-requests
vibebuild --analyze-only python-requests-*.src.rpm   # use the downloaded file
vibebuild --dry-run python-requests          # target from ~/.koji/config
vibebuild --dry-run fedora-43 python-requests  # explicit target
```

Without `koji`, use any `.src.rpm` you have for `--analyze-only` and `--dry-run`.

### Using your own Koji server

If you already have a Koji, `vibebuild` reads `~/.koji/config` (and `/etc/koji.conf`) to get `server`, `weburl`, `cert` and `serverca`, so it is often enough to pass `--server` to override. All options: `vibebuild --help-all`.

```bash
# After setting up ~/.koji/config with target = my-target:
vibebuild my-package.src.rpm

vibebuild --server https://koji.internal/kojihub my-target my-package.src.rpm
# or with explicit certificates:
vibebuild --server https://koji.internal/kojihub --cert ~/.koji/client.pem \
  --serverca ~/.koji/serverca.crt --build-tag my-build my-target my-package.src.rpm
```

## Local server (Docker)

The repository includes a ready-made Docker environment for local development and testing:

```bash
cd dev/koji-server
make setup    # Full setup: certificates, DB, Hub, Builder, client
```

After setup, Koji is available at https://localhost:8443/koji.


## Koji deployment

The repository includes an Ansible playbook for automatic Koji deployment on Fedora:

```bash
cd ansible

# Configure the inventory (set YOUR_VPS_IP and ansible_user)
vim inventory/hosts.ini

# Configure the variables (FQDN, passwords, etc.)
vim group_vars/all.yml

# Run the playbook
ansible-playbook -i inventory/hosts.ini playbook.yml
```

## How it works

```
┌─────────────────┐
│  vibebuild CLI  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐     ┌─────────────────┐
│   Analyzer      │────▶│  Parse SRPM     │
│                 │     │  Extract deps   │
└────────┬────────┘     └─────────────────┘
         │
         ▼
┌─────────────────┐     ┌─────────────────┐
│   Name resolver │────▶│ Macro expansion │
│ (rules + ML)    │     │ Name resolution │
└────────┬────────┘     └─────────────────┘
         │
         ▼
┌─────────────────┐     ┌─────────────────┐
│   Dependency    │────▶│  Koji check     │
│   resolver      │     │  Build DAG      │
└────────┬────────┘     └─────────────────┘
         │
         ▼
┌─────────────────┐     ┌─────────────────┐
│   Fetcher       │────▶│  Download       │
│                 │     │  from Fedora    │
└────────┬────────┘     └─────────────────┘
         │
         ▼
┌─────────────────┐     ┌─────────────────┐
│   Builder       │────▶│  koji build     │
│                 │     │  wait-repo      │
└─────────────────┘     └─────────────────┘
```

1. **Analyzer** — extracts BuildRequires from the SRPM/spec file, expands RPM macros
2. **Name resolver** — converts virtual provides and macro-based names into real RPM package names (rules + optional ML)
3. **Dependency resolver** — checks which dependencies are missing in Koji and builds the dependency graph
4. **Fetcher** — downloads SRPMs for the missing packages from Fedora with smart name mapping
5. **Builder** — builds the packages in the right order, waiting for the repository to regenerate between builds

## Training the ML model (optional)

VibeBuild includes scripts for training the ML package name resolution model:

```bash
# 1. Collect training data from the Fedora repositories
python scripts/collect_training_data.py --output data/training_data.json

# 2. Train the model (aliases from vibebuild/data/alias_training.json are mixed in automatically)
python scripts/train_model.py --input data/training_data.json --output vibebuild/data/model.joblib
```

The model uses TF-IDF character n-grams with K-Nearest Neighbors to predict real package names from virtual dependency strings. Training automatically mixes in the aliases from `vibebuild/data/alias_training.json` (for example, `python3` → `python3.12`), so that commands like `vibebuild --download-only python3` work when the model is installed. Use `--ml-model` to point to a custom model.

## Requirements

- **Python** 3.9+
- **koji** CLI — required for `--download-only` (downloading SRPMs from Fedora Koji) and for building. Without it these features are unavailable. Install: `sudo dnf install koji` (Fedora) or the equivalent for your distribution.
- **rpm-build**, **rpm2cpio** (for unpacking and building SRPMs; on Fedora: `dnf install rpm-build`)
- Access to a Koji server (for example, Fedora Koji for downloading; your own for building)

**Optional (ML name resolution):** `scikit-learn >= 1.3`, `joblib >= 1.3` — install: `pip install -e ".[ml]"`.

## Documentation

- [QUICKSTART.md](docs/QUICKSTART.md) — introduction from scratch: RPM/SRPM/Koji concepts, modules, first build step by step
- [ARCHITECTURE.md](docs/ARCHITECTURE.md) — system architecture
