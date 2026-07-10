# Getting started

This page is for anyone who has not worked with RPM packaging or Koji before. It explains the few ideas VibeBuild is built on, the problem it solves, and walks through a first build on the local Koji stand. Installation, options and configuration are in the [README](../README.md); how the code works is in [ARCHITECTURE.md](ARCHITECTURE.md).

## Contents

1. [Concepts](#1-concepts)
2. [The problem](#2-the-problem)
3. [First build, step by step](#3-first-build-step-by-step)
4. [Glossary](#4-glossary)

## 1. Concepts

### RPM

RPM is the package format of Fedora, RHEL, CentOS Stream, AlmaLinux and Rocky Linux. An `.rpm` file holds compiled programs, libraries and configuration; `dnf install` installs it. The file name carries the package's identity:

```
python3-requests-2.33.1-1.fc44.noarch.rpm
^                ^      ^ ^    ^
name             |      | |    architecture
           version      | dist tag (Fedora 44)
                  release
```

Name, version and release together are the **NVR**, `python3-requests-2.33.1-1.fc44`, which identifies a build.

### SRPM

A source RPM (`.src.rpm`) is what binary RPMs are built from. It contains:

- the **spec file**, the build recipe;
- the upstream **sources**, usually a tarball;
- **patches** applied on top of them.

One SRPM often produces several binary RPMs with different names. The SRPM `python-requests` builds `python3-requests`; `glib2` builds `glib2`, `glib2-devel` and others. Koji builds SRPMs, so to build something missing VibeBuild needs the SRPM name, not the binary one.

### Spec file

The recipe inside an SRPM:

```spec
Name:           python-requests
Version:        2.33.1
Release:        1%{?dist}

BuildRequires:  python3-devel
BuildRequires:  python3dist(idna)
BuildRequires:  python3dist(urllib3)

Requires:       python3dist(charset-normalizer)

%build
%pyproject_wheel

%install
%pyproject_install
```

- **BuildRequires** must be installed to *build* the package: compilers, `-devel` libraries, test frameworks.
- **Requires** are installed together with the package when a user *installs* it.

VibeBuild works only with BuildRequires. It reads them from the SRPM header, where `rpm` has already expanded the spec's macros.

### Koji

Koji is the build system Fedora uses for its packages. You hand it an SRPM; it builds the package in a clean environment and keeps the result.

```
                    ┌─────────────┐
                    │  Koji web   │  web UI
                    └──────┬──────┘
┌──────────┐        ┌──────┴──────┐        ┌──────────────┐
│  client  │───────>│  Koji hub   │<───────│ Koji builder │
└──────────┘        └──────┬──────┘        └──────────────┘
 koji, vibebuild           │                 runs builds in mock
                    ┌──────┴──────┐
                    │ PostgreSQL  │  packages, builds, tags, tasks
                    └─────────────┘
```

- The **hub** is the central server. Clients talk to it over XML-RPC.
- A **builder** (`kojid`) takes tasks from the hub and builds in **mock**, a fresh chroot with only the BuildRequires installed.
- The **web UI** shows tasks, builds, tags and logs.

A build is a **task**. It has subtasks: one rebuilds the SRPM, and one per architecture builds the binary RPMs. When it succeeds, the build is **tagged** into the destination tag.

### Tags, targets and repos

Koji groups builds with **tags**. A **target** names two of them:

```
target f44
  ├── build tag        f44-build   what the buildroot is installed from
  └── destination tag  f44         where a finished build is tagged
```

The build tag usually inherits the destination tag, so a package built into `f44` is available to later builds. But not at once: builders install BuildRequires from a **repo**, a dnf repository Koji generates from the build tag. A new build becomes visible only after the repo is regenerated. In Fedora a service called `kojira` does that; VibeBuild starts the regeneration itself and waits for it.

A tag can also have **external repos**, ordinary dnf repositories such as Fedora's own. Their packages are installable in the buildroot without being built in this Koji. That is how a small Koji builds a few packages on top of a whole Fedora release.

### Virtual provides

BuildRequires are often not package names:

```spec
BuildRequires:  python3dist(requests)   # provided by python3-requests
BuildRequires:  pkgconfig(glib-2.0)     # provided by glib2-devel
BuildRequires:  perl(File::Path)        # provided by perl-File-Path
```

These are **virtual provides**: a package declares that it provides a name, and dnf finds it by that name. To ask Koji about a dependency or download its SRPM, VibeBuild has to turn `python3dist(requests)` into `python3-requests` and then into the SRPM `python-requests`. Rules cover the common cases; an optional ML model guesses the rest.

### Dependency graph

The packages to build and their BuildRequires form a directed acyclic graph (DAG): an edge goes from a package to each dependency it needs, and there are no cycles.

```
        my-app
       /   |   \
    lib-a lib-b lib-c
      |   /  \
    lib-d    lib-e
```

Sorting the graph gives the build order, in **levels**:

- level 1: `lib-c`, `lib-d`, `lib-e` (need nothing that is missing);
- level 2: `lib-a`, `lib-b`;
- then the target, `my-app`.

Packages of one level are independent, so they are submitted together. Between levels the repo is regenerated, so level 2 can install what level 1 built.

## 2. The problem

`koji build` builds one SRPM. If a BuildRequires is in neither the build tag nor its external repos, the buildroot cannot be installed and the task fails:

```
$ koji build f44 python-requests-2.33.1-1.fc44.src.rpm
...
FAILED: BuildError: error building package (arch noarch), mock exited with status 30; see root.log for more information
```

and the task's `root.log` names the culprit: `No matching package to install: 'python3dist(idna)'`.

Fixing that by hand means:

1. work out which BuildRequires are missing and what they are called as SRPMs;
2. find and download each SRPM, the right version for your release;
3. do the same for *their* missing BuildRequires, recursively;
4. order everything so dependencies come first;
5. build level by level, regenerating the repo and waiting in between;
6. finally build the package you wanted.

A missing dependency often has missing dependencies of its own. VibeBuild does all of this from one command:

```bash
vibebuild f44 python-requests
```

It downloads SRPMs from Fedora Koji, checks each against the digest Fedora recorded for that build, skips what is already built, and prints a summary of what was built and tagged.

## 3. First build, step by step

The easiest place to try VibeBuild is the Docker stand in `dev/koji-server`: a complete Koji with a builder, where Fedora 44 is an external repo of the build tag. You need Docker; everything else runs in containers.

### Step 1. Start the stand

```bash
cd dev/koji-server
make setup
```

This generates secrets and certificates, starts PostgreSQL, the hub with the web UI, the builder and a client container, and creates the `f44` target. The first repo is generated in the background and takes a few minutes. Watch it from the client:

```bash
docker compose exec vibebuild-dev bash
koji list-tasks          # wait until the newRepo task is closed
koji list-targets
```

The client container has VibeBuild installed from your working tree and a `~/.koji/config` with the admin certificate and `target = f44`. The rest of this section runs inside it.

### Step 2. Look before building

`--analyze-only` reads an SRPM's BuildRequires and asks Koji which are missing. Given a package name, it downloads the SRPM from Fedora first:

```bash
vibebuild --analyze-only f44 python-requests
```

On a fresh stand the answer is "All dependencies available": everything comes from the Fedora 44 external repo. VibeBuild only builds dependencies that your Koji owns, that is, packages registered in the tag but not yet built there, or built in a version that is too old. (`--build-all-deps` drops that assumption and builds everything, which on a real package quickly reaches the toolchain itself.)

So let the stand own one dependency. Register `python-idna` in the tag without building it:

```bash
koji add-pkg --owner kojiadmin f44 python-idna
vibebuild --analyze-only f44 python-requests
```

Now `python-idna` is listed as missing. `--dry-run` shows the whole plan without submitting anything:

```bash
vibebuild --dry-run python-requests
```

```
DRY RUN - showing what would be built:

Target package: python-requests-2.33.1-1.fc44

Dependencies to build (1):
  Level 1: python-idna
Then: python-requests-2.33.1-1.fc44
```

### Step 3. Build

```bash
vibebuild --patch-specs python-requests
```

VibeBuild submits `python-idna`, waits for it, regenerates the `f44-build` repo, then builds and tags `python-requests`. On the stand this takes about 20 minutes, a good part of it in repo regeneration.

`--patch-specs` removes the `%check` section before submitting. Without it the `python-idna` part works the same, but `python-requests` fails in its own tests: four of them expect a connection to an unroutable address to time out, and the stand's Docker network refuses it at once. That is the kind of build `--patch-specs` is for. The client container has no bubblewrap, so VibeBuild warns that it parses the spec without a sandbox.

Run the same command again: `python-idna` is no longer missing, and `python-requests` is reported as already built (`=`). Nothing is submitted.

### Step 4. Look at the result

Open <https://localhost:8443/koji>, accept the stand's self-signed certificate:

- **Tasks**: every build and newRepo task, with logs. A failed build's `root.log` shows what the buildroot could not install, `build.log` shows compiler and test output.
- **Builds**: the finished builds.
- **Tags** → `f44`: what is tagged, and the package list.

From the client, `koji list-tagged f44` shows the same.

### Step 5. When something goes wrong

```bash
vibebuild -v python-requests        # debug logging
make logs                           # on the host, in dev/koji-server: all containers
docker compose logs koji-builder    # one container
make init                           # re-run the hub setup, safe to repeat
make regen-repo                     # regenerate the build repo by hand
make clean && make setup            # start over
```

## 4. Glossary

| Term | Meaning |
| --- | --- |
| BuildRequires | What must be installed to build a package |
| Build tag | The tag a target's buildroots are installed from |
| Builder | A machine running `kojid`, which builds packages in mock |
| Buildroot | The clean environment a build runs in |
| DAG | Directed acyclic graph; here, packages and the dependencies they need |
| Destination tag | The tag a target's finished builds are tagged into |
| External repo | A dnf repository attached to a tag; its packages can be installed without being built in this Koji |
| Hub | The central Koji server, an XML-RPC API over PostgreSQL |
| kojira | The Koji service that regenerates repos automatically; VibeBuild does not need it |
| Mock | The tool that creates a fresh chroot for each build |
| NVR | Name-version-release, e.g. `python-requests-2.33.1-1.fc44` |
| Repo | The dnf repository Koji generates from a build tag |
| Requires | What is installed together with a package |
| Scratch build | A build that is kept but never tagged |
| Spec file | The build recipe inside an SRPM |
| SRPM | Source RPM, the input of a Koji build |
| Tag | A named set of builds and packages in Koji |
| Target | A pair of build tag and destination tag |
| Task | A unit of work on the hub, such as a build or a newRepo |
| Virtual provide | A name a package provides besides its own, e.g. `python3dist(requests)` |
