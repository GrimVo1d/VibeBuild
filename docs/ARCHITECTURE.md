# VibeBuild Architecture

VibeBuild is an extension of the Koji build system that automates dependency resolution when building RPM packages.

The standard `koji build` command builds a single package and requires all BuildRequires to already be available in the Koji repository. If a dependency is missing, the build fails. VibeBuild solves this: it analyzes the SRPM, recursively downloads the missing dependencies from Fedora, builds a dependency DAG and builds the packages in the right order.

## Contents

- [Top-Level Architecture](#top-level-architecture)
- [Application Layers](#application-layers)
- [Data Flow](#data-flow)
- [Package Name Resolution](#package-name-resolution)
- [DAG Construction and Build Order](#dag-construction-and-build-order)
- [Koji Tags and Targets](#koji-tags-and-targets)
- [Exception Hierarchy](#exception-hierarchy)
- [Conventions and Caching](#conventions-and-caching)

---

## Top-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                       User (CLI)                             │
└─────────────────────────────┬───────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      VibeBuild                               │
│  ┌──────────┐  ┌───────────────┐  ┌──────────┐  ┌────────┐ │
│  │ analyzer │  │ name_resolver │  │ resolver │  │ builder│ │
│  │          │  │ + ml_resolver │  │          │  │        │ │
│  └──────────┘  └───────────────┘  └──────────┘  └────────┘ │
│                ┌──────────┐                                  │
│                │ fetcher  │                                  │
│                └──────────┘                                  │
└─────────────────┬───────────────────────────────────────────┘
                  │
        ┌─────────┼──────────────┐
        ▼         ▼              ▼
  ┌─────────┐ ┌───────────┐ ┌──────────────────┐
  │Koji Hub │ │Fedora Koji│ │src.fedoraproject │
  │(local)  │ │(external) │ │(SRPM source)     │
  └────┬────┘ └───────────┘ └──────────────────┘
       ▼
  ┌─────────┐
  │ kojid   │
  │ + mock  │
  └─────────┘
```

---

## Application Layers

The `vibebuild/` package contains eight modules. The entry point is `cli.py:main` (console script `vibebuild`).

### `analyzer.py` — SRPM analysis

Parses SRPMs and spec files, extracts metadata.

**Key elements:**

- `BuildRequirement` — dataclass: name, version, comparison operator
- `PackageInfo` — dataclass: name, version, release, build_requires, source_urls
- `SpecAnalyzer` — spec file parser
- `get_build_requires(srpm_path) -> list[str]` — extracts only the BuildRequires list via `rpm -qp --requires`
- `get_package_info_from_srpm(srpm_path) -> PackageInfo` — unpacks the SRPM via `rpm2cpio | cpio`, parses the .spec

**Spec parsing algorithm:**

1. Read the file line by line
2. Extract the fields `Name:`, `Version:`, `Release:`, `BuildRequires:`, `Source:`
3. Expand RPM macros (`%{name}`, `%{version}`)
4. Parse BuildRequires with operators (`>=`, `<=`, `>`, `<`, `=`)

### `name_resolver.py` — rule-based name resolution

Spec files often contain virtual dependency names that do not match the real RPMs. `PackageNameResolver` implements the pipeline:

```
Input → Cache → Macro expansion → Provides patterns → ML fallback → Result
```

**Components:**

- `SYSTEM_MACROS` — a table of 18 RPM system macros (`%{python3_pkgversion}` → `3`, `%{_bindir}` → `/usr/bin`, etc.)
- `PROVIDE_PATTERNS` — 9 compiled regex patterns with transformer functions: `python(\d*)dist(X)` → `python{N}-{X}`, `pkgconfig(X)` → `{X}-devel`, `perl(X::Y)` → `perl-X-Y`, `rubygem`, `npm`, `cmake`, `tex`, `golang`, `mvn`
- `SUBPACKAGE_TO_SRPM` — a hardcoded subpackage → SRPM mapping (≈380 entries) (`perl-File-Path` → `perl`, `python3-libs` → `python3.13`, `gcc-c++` → `gcc`)
- `resolve_srpm_name(rpm_name) -> list[str]` — generates SRPM name variants to download

### `ml_resolver.py` — ML fallback (optional)

Runs when the rule-based patterns could not handle a virtual provide.

**Model architecture:**

```
input → TF-IDF (char n-grams 2-5, max 50k features)
     → KNN (cosine distance, k=5)
     → distance ≤ 0.3 → (rpm_name, srpm_name)
     → distance > 0.3 → None (low confidence)
```

- `MLPackageResolver` — a wrapper around the scikit-learn model
- Gating via the `HAS_SKLEARN` flag: without `scikit-learn` the module is silently disabled
- The model is saved to `vibebuild/data/model.joblib` (~5–15 MB)
- Predictions are cached in `~/.cache/vibebuild/ml_name_cache.json`

Training uses `scripts/collect_training_data.py` (parses `primary.xml` from the Fedora repositories) and `scripts/train_model.py`.

### `resolver.py` — building the dependency DAG

**Key classes:**

- `KojiClient` — Koji client via the CLI (`subprocess`). Methods: `list_packages(tag)`, `package_exists(pkg, tag)`, `list_tagged_builds(tag)`, `search_package(pattern)`
- `DependencyNode` — graph node: `name`, `srpm_path`, `dependencies`, `is_available`, `build_order`
- `DependencyResolver` — the main class:
  - `find_missing_deps(deps, tag)` — filters out the dependencies missing in Koji
  - `build_dependency_graph(pkg, srpm, srpm_resolver)` — recursive traversal
  - `topological_sort()` — Kahn algorithm; detects cycles
  - `get_build_chain()` — groups nodes by level (`build_order`)

`find_missing_deps()` normalizes names via `name_resolver.resolve()` before checking in Koji.

**Graph construction algorithm:**

1. Start with the root package
2. For each package:
   - If it is in the Koji tag → `is_available = True`
   - Otherwise: extract BuildRequires, find the missing ones, process recursively
3. Topological sort
4. Group by level: packages without dependencies are level 0; a package level = max(level of dependencies) + 1; packages on the same level are built in parallel

### `fetcher.py` — SRPM download

**Sources (by priority):**

| # | Source | Method |
|---|---|---|
| 1 | Fedora Koji (`koji.fedoraproject.org`) | `koji download-build --type=src` |
| 2 | `src.fedoraproject.org` | Download spec + sources, local build via `rpmbuild -bs` |

**Key classes:**

- `SRPMSource` — source configuration (name, URL, priority)
- `SRPMFetcher` — downloader with caching and optional disabling of SSL verification

If a `name_resolver` is passed, `download_srpm()` uses `resolve_srpm_name()` to try SRPM name variants: for example, `python3-requests` is tried both as `python-requests` and as `python3-requests`.

### `builder.py` — build orchestration

**Key elements:**

- `BuildStatus` — enum: `PENDING`, `BUILDING`, `COMPLETE`, `FAILED`, `CANCELED`
- `BuildTask` — a build task: `package_name`, `srpm_path`, `target`, `task_id`, `status`
- `BuildResult` — the outcome: `success`, `tasks`, `built_packages`, `failed_packages`, `total_time`
- `KojiBuilder` — the orchestrator; holds `KojiClient`, `DependencyResolver`, `SRPMFetcher`

**The `build_with_deps(srpm_path)` method — the main function:**

1. Parse the SRPM (`get_package_info_from_srpm`)
2. Build the DAG (`build_dependency_graph`, recursively downloading the missing ones)
3. Get the build chain (`get_build_chain`)
4. For each level:
   - `koji build --nowait` for all packages of the level
   - Polling until completion
   - `koji wait-repo` — wait for the repository to regenerate
5. Build the target package
6. Return `BuildResult`

Without waiting for `wait-repo`, the builds of the next level would fail with a dependency not found error.

### `cli.py` — CLI

Argument parsing, cascading configuration loading:

```
CLI flags (highest priority)
    ↓
~/.koji/config
    ↓
/etc/koji.conf (fallback)
```

Creates `PackageNameResolver`, `SRPMFetcher`, `DependencyResolver`, `KojiBuilder` and dispatches to the right mode (full build, `--analyze-only`, `--download-only`, `--dry-run`, `--no-deps`, `--scratch`).

### `__main__.py`

Allows running via `python -m vibebuild`, delegates to `cli.main`.

### `exceptions.py`

See the section [Exception Hierarchy](#exception-hierarchy).

---

## Data Flow

```
SRPM file                                                        RPM packages
    │                                                                   ▲
    ▼                                                                   │
analyzer ──► PackageInfo ──► name_resolver ──► normalized names        │
                                  │                                     │
                                  ▼                                     │
                              resolver ◄── KojiClient (tag check)       │
                                  │                                     │
                                  ├─► find_missing ──► fetcher ──► SRPM │
                                  │                       │             │
                                  │           ◄───────────┘             │
                                  ▼                                     │
                              DAG → topological sort → build chain      │
                                  │                                     │
                                  ▼                                     │
                              builder ──► koji build (per level)───────┘
                                  │           │
                                  │           ▼
                                  └─► wait-repo between levels
```

---

## Package Name Resolution

Example transformations:

| In the spec file | Real RPM name | SRPM name |
|---|---|---|
| `python3dist(requests)` | `python3-requests` | `python-requests` |
| `python3dist(setuptools)` | `python3-setuptools` | `python-setuptools` |
| `%{python3_pkgversion}-devel` | `python3-devel` | `python3` |
| `pkgconfig(glib-2.0)` | `glib-2.0-devel` | `glib-2.0` |
| `perl(File::Path)` | `perl-File-Path` | `perl` |
| `cmake(Qt5Core)` | `cmake-qt5core` | — |
| `npm(typescript)` | `nodejs-typescript` | `nodejs-typescript` |
| `rubygem(bundler)` | `rubygem-bundler` | `rubygem-bundler` |
| `golang(github.com/foo/bar)` | `golang-github.com-foo-bar` | — |
| `mvn(org.apache:commons-lang)` | `commons-lang` | `commons-lang` |

**`name_resolver` integration points:**

- `analyzer` — better macro expansion via `SYSTEM_MACROS`
- `resolver` — name normalization before checking in Koji
- `fetcher` — trying SRPM variants via `resolve_srpm_name()`
- `builder` — creates the resolver and passes it to all components

The ML resolver is optional (`pip install -e ".[ml]"`); without `scikit-learn` it silently degrades.

---

## DAG Construction and Build Order

Example: we build `my-app`, which depends on `lib-foo`, `lib-bar`, `lib-baz`. `lib-foo` depends on `lib-base`, `lib-baz` on `lib-core`. `lib-bar`, `lib-base`, `lib-core` are already in Koji.

```
       my-app
      /   |   \
  lib-foo lib-bar(ok) lib-baz
     │              │
  lib-base(ok)   lib-core(ok)
```

**Topological sort (Kahn algorithm):**

1. Compute the in-degree of every node
2. Nodes with in-degree = 0 → queue
3. Take nodes from the queue, decrease the in-degree of their dependents
4. Cycle detection: if not all nodes were processed → `CircularDependencyError`

**Grouping by level:**

- Level 0: `lib-foo`, `lib-baz` (in parallel)
- `koji wait-repo`
- Level 1: `my-app`

---

## Koji Tags and Targets

```
fedora-target (Build Target)
    ├── build_tag: fedora-build  (BuildRoot, inherits from dest)
    └── dest_tag:  fedora-dest   (storage for finished packages)

fedora-build → external Fedora repositories (base + updates)
```

**Build process:**

1. The SRPM is uploaded to Koji
2. Mock creates a chroot from the `fedora-build` packages
3. The package is built in an isolated environment
4. The result is tagged into `fedora-dest`
5. The `fedora-build` repository is regenerated

---

## Exception Hierarchy

The root is `VibeBuildError`. All VibeBuild exceptions inherit from it, so they can be caught with a single `except`.

```
VibeBuildError
├── InvalidSRPMError          — invalid SRPM
├── SpecParseError            — spec parsing error
├── DependencyResolutionError — dependency resolution error
│   └── CircularDependencyError
├── SRPMNotFoundError         — SRPM not found in any source
├── KojiBuildError            — build error in Koji
├── KojiConnectionError       — Koji Hub connection error
└── NameResolutionError       — package name resolution error
```

---

## Conventions and Caching

**Python:**

- Minimum version is 3.9. Use `dict[]`/`list[]` (PEP 585), no walrus operator in code where 3.9 compatibility matters.
- Tests mock `koji`/`subprocess`/`requests` — a real Koji server is not needed for unit tests.

**Configuration:**

- Loaded from `~/.koji/config` (primary), `/etc/koji.conf` (fallback)
- CLI flags override the configs

**Caches:**

| Cache | Where |
|---|---|
| Packages available in a Koji tag | Memory, per session |
| Downloaded SRPMs | the fetcher `download_dir` |
| Dependency graph | Memory, built once |
| Resolved names | Memory, per session |
| ML predictions | `~/.cache/vibebuild/ml_name_cache.json` (persistent) |

**Parallelism:**

- Packages on the same DAG level are built in parallel (`koji build --nowait`)
- SRPM download can run in parallel with analysis

**Security:**

- SSL client certificates for authentication in Koji
- CA verification for HTTPS
- All builds are isolated in a mock chroot
