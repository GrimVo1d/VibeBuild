# QuickStart — VibeBuild from Scratch

A document for anyone who opened the project and does not understand what is going on here. It explains from zero: what RPM, SRPM and Koji are, why VibeBuild exists, how each module works and how to use all of it. No previous experience with RPM packages is required.

## Contents

1. [Basic concepts](#1-basic-concepts)
2. [What problem VibeBuild solves](#2-what-problem-vibebuild-solves)
3. [Modules — what each one does](#3-modules--what-each-one-does)
4. [How it all works together](#4-how-it-all-works-together)
5. [CLI — all commands with examples](#5-cli--all-commands-with-examples)
6. [Configuration](#6-configuration)
7. [First build: step by step](#7-first-build-step-by-step)
8. [Glossary](#8-glossary)

---

## 1. Basic concepts

### RPM

RPM (Red Hat Package Manager) is the package format of Red Hat family Linux distributions: Fedora, RHEL, CentOS, AlmaLinux, Rocky Linux.

Roughly: an RPM is a `.rpm` file that holds compiled programs, libraries and configuration. The command `dnf install python3` installs an RPM package.

```
python3-3.12.1-1.fc42.x86_64.rpm
  ^        ^     ^   ^      ^
 name   version  | fedora architecture
              release
```

### SRPM

SRPM (Source RPM) is a **source** RPM package. A binary RPM is built from it.

Inside an SRPM:

- **Spec file** — the build recipe (the main part)
- **Source code** — an archive with the sources (tar.gz, zip)
- **Patches** — if something needs patching

```
python3-3.12.1-1.fc42.src.rpm     <-- SRPM (note the .src.)
python3-3.12.1-1.fc42.x86_64.rpm  <-- binary RPM (already built)
```

Why is the SRPM needed? So that anyone can **rebuild** a package from source. It is the foundation of the whole Fedora packaging infrastructure.

### Spec file

The package build recipe. A text file with the `.spec` extension:

```spec
Name:    python-requests
Version: 2.31.0
Release: 1%{?dist}

# Dependencies for the BUILD (this is the most important part for us):
BuildRequires: python3-devel
BuildRequires: python3-setuptools
BuildRequires: python3dist(pytest)
BuildRequires: python3dist(urllib3)

# Dependencies for RUNNING (this is different):
Requires: python3-urllib3
Requires: python3-charset-normalizer

%description
HTTP client for Python.

%build
%py3_build

%install
%py3_install
```

The key difference:

- **BuildRequires** — needed only for the BUILD. Compilers, development libraries, test frameworks. Without them the package will not build.
- **Requires** — needed for RUNNING. What the end user gets as dependencies on installation.

VibeBuild works specifically with **BuildRequires**.

### Koji

Koji is a centralized RPM package build system. It is like CI/CD, but made specifically for packages.

```
                    ┌─────────────┐
                    │  Koji Web   │  <-- web interface
                    └──────┬──────┘
                           │
┌──────────┐        ┌──────┴──────┐        ┌──────────────┐
│ Client   │───────>│  Koji Hub   │───────>│ Koji Builder │
└──────────┘        └──────┬──────┘        └──────────────┘
                           │                  builds packages
                    ┌──────┴──────┐
                    │ PostgreSQL  │
                    └─────────────┘
                    database: packages, builds, tags, tasks
```

- **Koji Hub** — the central server, accepts build tasks
- **Koji Builder** — the machine that actually builds packages (in an isolated mock environment)
- **Koji Web** — the web front end for viewing results
- **PostgreSQL** — the database holding all the information

When `koji build f42-candidate my-package.src.rpm` runs:

1. The hub accepts the task
2. The builder downloads the SRPM
3. The builder creates a clean environment (mock) with the required dependencies
4. The builder builds the package
5. The result is saved and tagged

### Tags and targets

Koji organizes packages with **tags**. A tag is a label attached to a set of packages.

```
f42-candidate      <-- candidates for inclusion in Fedora 42
  └─ f42-build     <-- build tag, the builder takes dependencies from it
```

A **target** is a pair: "where to put the result" + "where to take dependencies from":

```
Target:    f42-candidate
  ├── destination tag: f42-candidate  (where the finished package goes)
  └── build tag:       f42-build      (where dependencies come from)
```

When building for `f42-candidate`, the builder looks in `f42-build` to check that all BuildRequires are there. If not, the build fails.

### Virtual Provides

In spec files, dependencies are often NOT written as real package names:

```spec
BuildRequires: python3dist(requests)     # the package is called python3-requests
BuildRequires: pkgconfig(glib-2.0)       # this is glib2-devel
BuildRequires: perl(File::Path)          # this is perl-File-Path
```

These are **virtual provides**. RPM lets a package "provide" arbitrary names. The package `python3-requests` provides `python3dist(requests)`.

The problem: to **download** a package you need to know its real name, not the virtual one. VibeBuild can convert virtual names to real ones (see the `name_resolver.py` module).

### DAG

DAG (Directed Acyclic Graph) is a directed graph with no cycles.

Say you need to build `my-app`. It needs `lib-a` and `lib-b`. `lib-a` needs `lib-d`. `lib-b` needs `lib-d` and `lib-e`.

```
        my-app
       /   |   \
    lib-a  lib-b  lib-c
      |    / \
    lib-d  lib-e
```

The graph is directed (arrows point from the dependent to the dependency) and acyclic (no cycles).

The **build order** is determined like this:

- First: `lib-c`, `lib-d`, `lib-e` (depend on nothing) — Level 0
- Then: `lib-a`, `lib-b` (depend on Level 0) — Level 1
- Last: `my-app` — Level 2

Within one level, packages can be built **in parallel**.

---

## 2. What problem VibeBuild solves

### Standard koji build

```bash
$ koji build f42-candidate python-requests-2.31.0-1.fc42.src.rpm

# The builder starts the build...
# Creates the mock environment...
# Tries to install BuildRequires...

ERROR: No matching package to install: python3dist(urllib3)
ERROR: No matching package to install: python3dist(charset-normalizer)
ERROR: Not all dependencies satisfied
BUILD FAILED
```

The builder found dependencies in the spec file, but those packages are not in the `f42-build` tag.

### Without VibeBuild — by hand

1. Work out which dependencies are missing
2. Find the SRPM for each missing one
3. Check whether THEY have missing dependencies
4. Repeat recursively until you reach the bottom
5. Determine the correct build order
6. Build everything in turn, waiting for repository regeneration between levels
7. Finally build the target package

For a single package this can mean 5–20 dependencies and several hours of work.

### With VibeBuild — one command

```bash
$ vibebuild f42-candidate python-requests-2.31.0-1.fc42.src.rpm

Analyzing python-requests-2.31.0-1.fc42.src.rpm...
Found 12 BuildRequires
Checking availability in f42-build...
Missing: 3 packages
  - python3-urllib3
  - python3-charset-normalizer
  - python3-idna

Downloading SRPMs from Fedora...
  Downloaded: python-urllib3-2.1.0-1.fc42.src.rpm
  Downloaded: python-charset-normalizer-3.3.0-1.fc42.src.rpm
  Downloaded: python-idna-3.6-1.fc42.src.rpm

Building dependency graph...
Build chain: 2 levels, 4 packages

Level 0: python-charset-normalizer, python-idna, python-urllib3
  [building...] [building...] [building...]
  [complete]    [complete]    [complete]

Waiting for repo regeneration...

Level 1 (target): python-requests
  [building...]
  [complete]

BUILD COMPLETE
  Built: 4 packages
  Failed: 0
  Time: 12m 34s
```

---

## 3. Modules — what each one does

```
vibebuild/
  ├── cli.py             -- entry point, argument parsing
  ├── __main__.py        -- launch via `python -m vibebuild`
  ├── analyzer.py        -- SRPM parsing, BuildRequires extraction
  ├── name_resolver.py   -- converting virtual names to real ones
  ├── ml_resolver.py     -- ML fallback for names (optional)
  ├── resolver.py        -- building the dependency graph
  ├── fetcher.py         -- downloading SRPMs from Fedora
  ├── builder.py         -- build orchestration
  └── exceptions.py      -- custom exceptions
```

### analyzer.py

Parses an SRPM and extracts package information, primarily the list of BuildRequires.

**Steps:**

1. Input is a `.src.rpm` file
2. Extract the spec file via `rpm2cpio | cpio`
3. Parse the spec file line by line
4. Collect macros (`%global`, `%define`)
5. Expand macros in BuildRequires (`%{python3_pkgversion}` → `3`)
6. Return structured data

**Key classes:**

```python
@dataclass
class BuildRequirement:
    name: str                        # python3dist(requests)
    version: Optional[str] = None    # 2.28.0
    operator: Optional[str] = None   # >=

@dataclass
class PackageInfo:
    name: str                              # python-requests
    version: str                           # 2.31.0
    release: str                           # 1.fc42
    build_requires: list[BuildRequirement]
    source_urls: list[str]
```

**Usage:**

```python
from vibebuild.analyzer import get_build_requires, get_package_info_from_srpm

deps = get_build_requires("my-package.src.rpm")
# ["python3-devel", "python3dist(urllib3)", ...]

info = get_package_info_from_srpm("my-package.src.rpm")
# PackageInfo(name="python-requests", version="2.31.0", ...)
```

### name_resolver.py

Turns virtual dependency names into real RPM names, and then into SRPM names.

**Conversion chain:**

```
python3dist(requests)  -->  python3-requests  -->  python-requests (SRPM)
   virtual                 real RPM              source package
```

**Pipeline:**

```
Input name
    │
    ├─> Cache (already resolved?)
    │
    ├─> Macro expansion (%{python3_pkgversion} → 3)
    │
    ├─> 9 regex patterns for virtual provides:
    │      python3dist(X) → python3-X
    │      pkgconfig(X)   → X-devel
    │      perl(X::Y)     → perl-X-Y
    │      rubygem(X)     → rubygem-X
    │      npm(X)         → nodejs-X
    │      cmake(X)       → cmake-x
    │      tex(X)         → texlive-X
    │      golang(X/Y)    → golang-X-Y
    │      mvn(X:Y)       → Y
    │
    ├─> Boolean deps handling: (python3dist(X) if ...)
    │
    └─> ML fallback (if the rules did not cope)
```

**Subpackage mapping (`resolve_srpm_name`):**

When an RPM package is a **subpackage** of another SRPM:

```
python3-libs      --> SRPM "python3.13" (not "python3-libs"!)
python3-devel     --> SRPM "python3.13"
gcc-c++           --> SRPM "gcc"
perl-File-Path    --> SRPM "perl"
glibc-devel       --> SRPM "glibc"
```

`name_resolver.py` hardcodes about 380 such mappings in `SUBPACKAGE_TO_SRPM`.

### ml_resolver.py

An ML model as a fallback when the rules in `name_resolver.py` did not cope.

**When it is needed:** for rare virtual provides not covered by the regex patterns.

**How it works:**

1. **TF-IDF** — turns a package name into a numeric vector. The name is split into char n-grams of 2–5 characters and their "importance" is computed.
2. **KNN** — finds the nearest neighbors by cosine distance.
3. The model is trained on "virtual name → RPM package" mappings collected from Fedora.

The ML model is **optional**. It requires `scikit-learn`. Without it VibeBuild works with rules only.

```python
from vibebuild.ml_resolver import MLPackageResolver

resolver = MLPackageResolver(model_path="model.joblib")
if resolver.is_available():
    result = resolver.predict("some-weird-provide")
    # {"rpm_name": "some-package", "srpm_name": "some-package"}
```

### resolver.py

Builds the DAG, checks which packages exist in Koji, determines the build order.

**Steps:**

1. Gets the target SRPM and its BuildRequires from `analyzer`
2. For each BuildRequires checks whether it exists in Koji (`koji list-tagged`)
3. Adds the missing ones to the graph as "needs building"
4. Recursively processes dependencies of dependencies
5. Topological sort
6. Grouping by level

**Key classes:**

```python
@dataclass
class DependencyNode:
    name: str
    srpm_path: Optional[str] = None
    package_info: Optional[PackageInfo] = None
    dependencies: list[str] = []
    is_available: bool = False     # already in Koji?
    build_order: int = -1

class KojiClient:
    """Wrapper around the koji CLI tool."""

class DependencyResolver:
    """Builds the graph and computes the build order."""
```

**Example:**

```
Input: my-app.src.rpm (depends on lib-a, lib-b, lib-c)
       lib-a depends on lib-d
       lib-b depends on lib-d, lib-e
       lib-c, lib-d, lib-e -- depend on nothing

Graph:
    my-app --> lib-a --> lib-d
           --> lib-b --> lib-d
                     --> lib-e
           --> lib-c

Topological sort: lib-d, lib-e, lib-c, lib-a, lib-b, my-app

Levels:
    Level 0: [lib-c, lib-d, lib-e]  -- in parallel
    Level 1: [lib-a, lib-b]         -- in parallel
    Level 2: [my-app]               -- target
```

### fetcher.py

Downloads SRPM files from Fedora for packages that are missing in the local Koji.

**Sources (by priority):**

1. **Fedora Koji** (`koji.fedoraproject.org`) over XML-RPC — finds the latest build for the needed Fedora version, downloads the SRPM from `kojipkgs.fedoraproject.org`
2. **src.fedoraproject.org** over HTTP — downloads the spec + sources, rebuilds the SRPM locally with `rpmbuild -bs`

**Features:**

- Caching of downloaded SRPMs
- Auto-detection of the Fedora version from the tag name (`f42-candidate` → fedora 42)
- Fallback to previous Fedora versions
- Name mapping via `name_resolver`

```python
fetcher = SRPMFetcher(
    download_dir="/tmp/srpms",
    fedora_release="42",
    name_resolver=resolver,
)
srpm_path = fetcher.download_srpm("python-requests")
# → "/tmp/srpms/python-requests-2.31.0-1.fc42.src.rpm"
```

### builder.py

Orchestrates the whole build process. The main module, it ties everything together.

**Full `build_with_deps` cycle:**

```
1. wait-repo         -- make sure the repo is ready
2. analyze SRPM      -- extract BuildRequires (analyzer)
3. resolve names     -- convert names (name_resolver)
4. find missing      -- check what exists in Koji (resolver)
5. download missing  -- download the missing SRPMs (fetcher)
6. build DAG         -- build the graph (resolver)
7. get build chain   -- determine the levels

For each level:
    8. submit builds  -- koji build --nowait for all packages of the level
    9. poll builds    -- wait for completion
    10. wait-repo     -- wait for repository regeneration

11. build target     -- build the target package
12. return result    -- BuildResult
```

**Repo regeneration:** once a package is built and tagged, it is NOT available immediately. Koji has to regenerate the repository (`createrepo`):

```
Level 0 built → koji call newRepo f42-build
              → koji wait-repo f42-build --timeout=1800
              → Level 1 now sees the packages from Level 0
```

Without waiting for repo regen, builds of the next level will fail with "dependency not found".

**Key classes:**

```python
class BuildStatus(Enum):
    PENDING, BUILDING, COMPLETE, FAILED, CANCELED

@dataclass
class BuildTask:
    package_name: str
    srpm_path: str
    target: str
    task_id: Optional[int] = None
    status: BuildStatus = BuildStatus.PENDING

@dataclass
class BuildResult:
    success: bool
    tasks: list[BuildTask]
    failed_packages: list[str]
    built_packages: list[str]
    total_time: float

class KojiBuilder:
    def build_with_deps(self, srpm_path: str) -> BuildResult: ...
```

### cli.py

The entry point. Parses arguments, loads the configuration, starts the requested mode.

**Modes:**

| Mode | Flag |
|-------|------|
| Full build | (default) |
| Analysis only | `--analyze-only` |
| Download only | `--download-only` |
| Dry run | `--dry-run` |
| Without dependencies | `--no-deps` |
| Scratch | `--scratch` |

**Configuration loading (cascading):**

```
CLI flags (highest priority)
    ↓
~/.koji/config
    ↓
/etc/koji.conf (fallback)
```

### exceptions.py

```
VibeBuildError                  -- base exception
  ├── InvalidSRPMError
  ├── SpecParseError
  ├── DependencyResolutionError
  │     └── CircularDependencyError
  ├── SRPMNotFoundError
  ├── KojiBuildError
  ├── KojiConnectionError
  └── NameResolutionError
```

All errors are caught with a single `except`:

```python
try:
    result = builder.build_with_deps("my-package.src.rpm")
except VibeBuildError as e:
    print(f"Something went wrong: {e}")
```

---

## 4. How it all works together

Example: `vibebuild f42-candidate python-requests`

```
1. cli.py parses the arguments
   → target = "f42-candidate"
   → package = "python-requests" (a name, not a file)

2. cli.py: "python-requests" -- not a file, download it
   → fetcher.download_srpm("python-requests")
   → /tmp/vibebuild/python-requests-2.31.0-1.fc42.src.rpm

3. analyzer.py parses the SRPM
   → BuildRequires:
     - python3-devel
     - python3-setuptools
     - python3dist(pytest)
     - python3dist(urllib3)
     - python3dist(charset-normalizer)
     - python3dist(idna)
     - python3dist(certifi)
     + 5 more...

4. name_resolver.py converts the names
   → python3dist(urllib3)            → python3-urllib3
   → python3dist(charset-normalizer) → python3-charset-normalizer
   → python3dist(idna)               → python3-idna
   → python3dist(certifi)            → python3-certifi
   → python3dist(pytest)             → python3-pytest

5. resolver.py checks Koji
   → python3-devel              PRESENT
   → python3-setuptools         PRESENT
   → python3-urllib3            MISSING
   → python3-charset-normalizer MISSING
   → python3-idna               MISSING
   → python3-certifi            PRESENT
   → python3-pytest             PRESENT

   Missing: python3-urllib3, python3-charset-normalizer, python3-idna

6. name_resolver.py: resolve_srpm_name for downloading
   → python3-urllib3             → SRPM: python-urllib3
   → python3-charset-normalizer  → SRPM: python-charset-normalizer
   → python3-idna                → SRPM: python-idna

7. fetcher.py downloads 3 SRPMs from Fedora

8. resolver.py builds the DAG (recursively checks dependencies of the downloaded ones)

   Graph:
     python-requests → [python-urllib3, python-charset-normalizer, python-idna]
     python-urllib3 → []
     python-charset-normalizer → []
     python-idna → []

   Levels:
     Level 0: python-urllib3, python-charset-normalizer, python-idna
     Level 1: python-requests (target)

9. builder.py builds Level 0 (3 parallel koji build --nowait)
   → all three finished: COMPLETE

10. builder.py waits for repo regen
    → koji call newRepo f42-build
    → koji wait-repo f42-build --timeout=1800

11. builder.py builds the target
    → koji build f42-candidate python-requests-2.31.0-1.fc42.src.rpm
    → COMPLETE

12. cli.py prints the result:
    BUILD SUMMARY
    Built:  4 packages
    Failed: 0
    Time:   8m 42s
```

---

## 5. CLI — all commands with examples

### Syntax

```bash
# Format 1: target from ~/.koji/config
vibebuild [OPTIONS] SRPM

# Format 2: explicit target
vibebuild [OPTIONS] TARGET SRPM

# The SRPM can be:
#   - a file path:     ./my-package-1.0-1.fc42.src.rpm
#   - a package name:  python-requests (will be downloaded from Fedora)
```

### Modes

```bash
# Full build
vibebuild f42-candidate python-requests

# Dependency analysis only
vibebuild --analyze-only python-requests.src.rpm

# Only download the SRPM from Fedora
vibebuild --download-only python-requests

# Show the build plan
vibebuild --dry-run f42-candidate python-requests
```

### Useful flags

```bash
# Scratch build (not tagged)
vibebuild --scratch f42-candidate python-requests

# Do not wait for the build to finish
vibebuild --nowait f42-candidate python-requests

# Skip dependency resolution
vibebuild --no-deps f42-candidate my-package.src.rpm

# Verbose output
vibebuild --verbose f42-candidate python-requests

# Quiet mode
vibebuild --quiet f42-candidate python-requests
```

### Connection settings

```bash
# Custom Koji server
vibebuild --server https://my-koji/kojihub f42-candidate pkg.src.rpm

# Client certificate
vibebuild --cert ~/.koji/client.pem f42-candidate pkg.src.rpm

# Set the build tag explicitly
vibebuild --build-tag f42-build f42-candidate pkg.src.rpm

# Fedora version for downloading SRPMs
vibebuild --fedora-release 41 f42-candidate pkg.src.rpm
```

### ML model

```bash
# Use a custom model
vibebuild --ml-model ./my-model.joblib f42-candidate pkg.src.rpm

# Disable ML (rule-based only)
vibebuild --no-ml f42-candidate pkg.src.rpm

# Disable all name resolution
vibebuild --no-name-resolution f42-candidate pkg.src.rpm
```

---

## 6. Configuration

### ~/.koji/config

The main config. Lives in the user home directory.

```ini
[koji]
# Koji Hub address (XML-RPC endpoint)
server = https://my-koji.internal/kojihub

# Web interface
weburl = https://my-koji.internal/koji

# Client SSL certificate
cert = ~/.koji/client.pem

# Server CA certificate
serverca = ~/.koji/serverca.crt

# Default target
target = f42-candidate

# Build tag
build_tag = f42-build
```

### Certificates

Koji uses SSL client certificates for authentication:

- **client.pem** — personal certificate (issued by the Koji administrator)
- **serverca.crt** — server CA certificate

```
~/.koji/
  ├── config          -- config (text)
  ├── client.pem      -- personal certificate
  └── serverca.crt    -- server CA
```

### Settings priority

```
CLI flags (--server, --cert, ...)   <-- highest priority
    ↓
~/.koji/config                       <-- user config
    ↓
/etc/koji.conf                       <-- system config (fallback)
```

---

## 7. First build: step by step

The quickest way is to bring up a local Koji in Docker and build a package.

### Step 1. Installation

```bash
# Clone the project repository, then:
pip install -e ".[dev,ml]"
```

System dependencies (Fedora/RHEL):

```bash
sudo dnf install -y koji rpm-build rpm2cpio
```

### Step 2. Local Koji

```bash
cd dev/koji-server
make setup
```

After a few minutes:

- Hub: <https://localhost:8443/kojihub>
- Web: <https://localhost:8443/koji>
- `~/.koji/config` has already been created by the script

Check:

```bash
koji list-tags
koji list-hosts
koji list-targets
```

### Step 3. First build

The simplest example is a package with minimal dependencies:

```bash
# Analyze the package dependencies (no build)
vibebuild --analyze-only python-six

# Show the build plan
vibebuild --dry-run python-six

# Real build
vibebuild python-six
```

VibeBuild will download the SRPM from Fedora itself, work out the missing dependencies and build everything in the right order.

### Step 4. What to look at in the Web UI

Open <https://localhost:8443/koji>:

- **Tasks** — all build tasks
- **Builds** — successfully built packages
- **Tags** — the list of tags and the packages in each tag
- **Hosts** — status of the builders' hosts

When the build finishes, the packages appear in `/mnt/koji/packages/` (inside the container) and are visible in the Web UI.

### Step 5. What to do if it failed

```bash
# Logs of all containers
cd dev/koji-server && make logs

# Logs of a specific container
docker compose logs koji-builder

# Full reset
make clean && make setup
```

---

## 8. Glossary

| Term | What it is |
|--------|--------|
| **RPM** | Red Hat Package Manager — the package format in Fedora/RHEL/CentOS |
| **SRPM** | Source RPM — the source package a binary RPM is built from |
| **spec file** | The package build recipe (.spec), holds metadata, dependencies, instructions |
| **BuildRequires** | Dependencies for BUILDING the package (not for running) |
| **Requires** | Dependencies for RUNNING the package |
| **Koji** | A centralized RPM package build system |
| **Koji Hub** | The central Koji server |
| **Koji Builder** | The machine that actually builds packages |
| **Mock** | A tool that creates a clean build environment (chroot) |
| **Tag** | A label for a group of packages in Koji (for example, `f42-build`) |
| **Target** | A pair: destination tag + build tag |
| **Build tag** | The tag the builder takes dependencies from |
| **Destination tag** | The tag the built package lands in |
| **Virtual provide** | A virtual dependency name (`python3dist(X)`, `pkgconfig(X)`) |
| **DAG** | Directed Acyclic Graph — a directed acyclic graph of dependencies |
| **Topological sort** | A linear ordering of DAG vertices that respects dependencies |
| **Repo regeneration** | Recreating the YUM/DNF repository after new packages are added |
| **NVR** | Name-Version-Release — a unique build identifier (`python-requests-2.31.0-1.fc42`) |
| **Scratch build** | A test build, the result is not tagged |
| **TF-IDF** | Term Frequency — Inverse Document Frequency, a method for turning text into a vector |
| **KNN** | K-Nearest Neighbors — a nearest neighbor search algorithm |
| **XML-RPC** | A remote procedure call protocol, used by the Koji API |
