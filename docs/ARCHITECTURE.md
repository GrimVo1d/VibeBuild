# Architecture

How VibeBuild turns one SRPM into a chain of Koji builds. The concepts (tags, targets, repos, virtual provides) are explained in [GETTING_STARTED.md](GETTING_STARTED.md); installation and options in the [README](../README.md).

## Contents

- [Overview](#overview)
- [Modules](#modules)
- [The build flow](#the-build-flow)
- [What counts as missing](#what-counts-as-missing)
- [Package names](#package-names)
- [Build order](#build-order)
- [Talking to Koji](#talking-to-koji)
- [SRPMs: download and patching](#srpms-download-and-patching)
- [ML resolver](#ml-resolver)
- [Performance](#performance)
- [Errors, exit codes and logs](#errors-exit-codes-and-logs)

## Overview

```
                         vibebuild CLI
                              │
     ┌──────────────┬─────────┴────────┬──────────────────┐
     ▼              ▼                  ▼                  ▼
  analyzer    name_resolver        resolver            builder
  (rpm -qp)   (+ ml_resolver)   (graph, order)   (submit, wait, newRepo)
                    │                  │                  │
                    │            koji_client ◄────────────┤
                    ▼                  │                  ▼
                 fetcher               │             spec_patcher
                    │                  │          (--patch-specs only)
                    ▼                  ▼
          Fedora Koji + kojipkgs   your Koji hub ──► builders (mock)
          (SRPMs, anonymous)       (XML-RPC, client certificate)
```

Two Koji hubs are involved. Your hub, from `~/.koji/config` or `--server`, is asked what the build tag has and receives the builds. Fedora Koji is only read: it tells VibeBuild which build is the latest for a release and what its SRPM's digest is, and `kojipkgs.fedoraproject.org` serves the file.

## Modules

| Module | Role |
| --- | --- |
| `cli.py` | Argument parsing, `~/.koji/config` loading, the four modes (build, `--analyze-only`, `--download-only`, `--dry-run`), the build summary. `vibebuild train` is dispatched to `training.py` before parsing. |
| `analyzer.py` | Reads name, version, release and BuildRequires from the SRPM header with `rpm -qp --qf`. The header has the spec's macros already expanded; `rpmlib(...)` and file dependencies are dropped. |
| `name_resolver.py` | Rule-based names: rich dependencies, macros, virtual provides, RPM → SRPM candidates. Falls back to the ML resolver. |
| `ml_resolver.py` | Optional nearest-neighbour model for names the rules cannot handle. |
| `training.py` | `vibebuild train`: Fedora repodata → provide/package pairs → model, with a held-out evaluation. |
| `resolver.py` | Decides which BuildRequires are missing from the build tag, builds the dependency graph and the build levels. |
| `koji_client.py` | `KojiClient`, anonymous and cached, for questions; `KojiSession`, logged in, for uploads, builds, task polling and repos. |
| `builder.py` | `KojiBuilder` runs a whole build: repo check, levels, retries, the target, the result. |
| `fetcher.py` | Downloads SRPMs from Fedora Koji and verifies them. |
| `spec_patcher.py` | With `--patch-specs`: unpacks an SRPM, edits the spec, rebuilds the SRPM in a sandbox. |
| `toolchain.py` | Classifies BuildRequires by language and build system for the summary. |
| `_retry.py` | Exponential backoff for transient download errors. |
| `_logging.py` | JSON log formatter for `--log-format json`. |
| `exceptions.py` | The exception hierarchy. |

## The build flow

`vibebuild f44 python-requests` runs `KojiBuilder.build_with_deps`:

1. **Get the SRPM.** A path is used as is; a name is downloaded from Fedora Koji, from the release that matches the target (`f44` → `f44-updates`, then `f44`), `--fedora-release`, or rawhide.
2. **Look up the target** on your hub: its build tag and destination tag.
3. **Log in once.** `KojiSession` holds a single logged-in session for the whole run.
4. **Make the build repo current.** If the build tag or anything it inherits changed since its last repo, start `newRepo` and wait for it.
5. **Resolve.** Read the target's BuildRequires, find the missing ones, download their SRPMs, read *their* BuildRequires, and so on, until nothing new is missing. The result is a list of levels.
6. **Build the levels.** Submit every package of a level, poll until all finish, retry unpackaged-file failures (with `--patch-specs`). If anything was built, regenerate the repo before the next level.
7. **Build the target**, if every dependency succeeded, the same way, waiting for it unless `--nowait`.
8. **Report.** What was tagged into the destination tag, which toolchains the BuildRequires use, and every task with its state.

`--dry-run` stops after step 5 and prints the levels. `--analyze-only` only does the first round of step 5, for the target itself, without downloading. `--no-deps` skips steps 4 to 6.

## What counts as missing

`DependencyResolver.find_missing_deps` decides, for each BuildRequires, whether it has to be built:

1. The name is resolved (`python3dist(idna)` → `python3-idna`).
2. If the package is in the build tag's package list (inheritance included) but has no build there, it is missing. Being listed means "this Koji owns it".
3. If it has a build, a version constraint from the spec (`>= 3.7`) is checked against the latest build; a build that is too old makes it missing.
4. Otherwise the SRPM name candidates are tried the same way (`python3-idna` → `python-idna`).
5. If the build tag has external repos, a dependency this Koji does not own is assumed to come from them and is not built. `--build-all-deps` turns this off and builds every dependency locally.

Without external repos every dependency without a build is missing. The root package is always analysed, even when a build of it exists, because a new version can need dependencies the old one did not. Lookups for all names of one package go to the hub in a single multicall.

## Package names

`PackageNameResolver.resolve` runs a pipeline and caches the answer per run:

1. **Rich dependencies:** `(python3dist(tomli) if python3-devel < 3.11)` → `python3dist(tomli)`.
2. **Macros:** a small table of macros that do not change between releases (`%{python3_pkgversion}`, `%{_bindir}`, ...). Version macros are left alone rather than guessed.
3. **Virtual provides:** nine patterns.
4. **ML fallback**, only for names that still look like a provide, i.e. contain `(`.
5. Otherwise the name as it is.

| BuildRequires | Resolved name | SRPM candidates |
| --- | --- | --- |
| `python3dist(requests)` | `python3-requests` | `python-requests`, `requests`, `python3-requests` |
| `perl(File::Path)` | `perl-File-Path` | `perl-File-Path` |
| `rubygem(bundler)` | `rubygem-bundler` | `rubygem-bundler` |
| `npm(typescript)` | `nodejs-typescript` | `nodejs-typescript` |
| `mvn(org.apache:commons-lang)` | `commons-lang` | `commons-lang` |
| `pkgconfig(glib-2.0)` | `glib-2.0-devel` | `glib-2.0`, `glib-2.0-devel` |
| `gcc-c++` | `gcc-c++` | `gcc`, `gcc-c++` |

`resolve_srpm_name` turns a binary name into SRPM candidates, most likely first: a table of about 230 known subpackages (`gcc-c++` → `gcc`), then suffix and prefix rules (`-devel`, `-libs`, `python3-`). The `pkgconfig`, `cmake` and `golang` patterns are rough: `pkgconfig(glib-2.0)` is really provided by `glib2-devel`. When downloading, the ML prediction is tried before these rules, which is where the model helps most.

## Build order

`resolver.py` keeps a node per package with the dependencies that have to be built. `topological_sort` is Kahn's algorithm; if some packages can never be scheduled, there is a cycle and it raises `CircularDependencyError`. `get_build_chain` gives every package a level, one more than its highest dependency, and returns the levels without the root, which the builder builds last.

Within a level packages do not depend on each other, so they are submitted together and Koji runs them in parallel on its builders. A failed package does not stop the later levels: whatever builds is tagged, and the next run skips it. The target is built only when all its dependencies succeeded.

SRPMs of the missing dependencies of one package are downloaded in parallel, five at a time, and the graph is then walked in a fixed order, so the plan does not depend on which download finished first.

## Talking to Koji

Everything goes through the Koji Python library over XML-RPC; the `koji` command is not used.

- **`KojiClient`** is anonymous. It answers questions about tags, builds and targets, and caches latest-build lookups for the run. The builder clears the cache after a newRepo, when new builds may have appeared. `prefetch_latest_builds` asks for many packages in one multicall.
- **`KojiSession`** logs in with the client certificate. The hub serialises calls per user and refuses a second login in a row with `AuthLockError`, so one run uses one session for everything. `submit` adds the package to the destination tag's package list, uploads the SRPM and starts the build. If Koji reports that the NVR is already built, nothing is submitted and the task is marked already built; the builder also checks the destination tag for the NVR before uploading.
- **Repos.** There may be no `kojira` on the hub, so VibeBuild starts `newRepo` itself and waits for the task. `repo_is_current` compares the repo's creation event with changes to the build tag and its parents, so an unchanged tag is not regenerated.
- **Timeouts.** Every hub call has a 300-second timeout (`HUB_TIMEOUT`; the library's default is 12 hours). Anonymous calls are retried by the library. A build is waited for up to 2 hours, a newRepo up to 30 minutes.
- **Unpackaged files.** When a build fails, the tail of `build.log` of the task and its subtasks is read. If it contains "Installed (but unpackaged) file(s) found" and `--patch-specs` is on, the SRPM is rebuilt with `%define _unpackaged_files_terminate_build 0` and submitted once more.

## SRPMs: download and patching

`SRPMFetcher` looks the build up on Fedora Koji (`getLatestBuilds` in `f<N>-updates`, then `f<N>`; rawhide's build tag for rawhide), downloads its SRPM from `kojipkgs.fedoraproject.org` over HTTPS and checks it against the build's `payloadhash`. That is the MD5 Koji recorded for the RPM header and payload. VibeBuild computes it from the file's bytes, not from the RPM's own signature header, which only says what the file claims. A file that does not match is never moved into place.

Downloads land in `~/.cache/vibebuild/srpms` (`$XDG_CACHE_HOME`, `--download-dir`). A cached file is reused only if it still matches the digest of the current latest build. Package names and NVRs from Koji are checked against a strict pattern before they become paths or URLs, and a download larger than 4 GiB is aborted.

`--patch-specs` rebuilds SRPMs before submitting them. `spec_patcher` unpacks the SRPM with `rpm2archive` and Python's `tarfile` data filter, capped at 8 GiB, removes the `%check` section, optionally adds the unpackaged-files macro, and runs `rpmbuild -bs`. Parsing a spec executes its `%(...)` and `%{lua:...}` macros, so `rpmbuild` runs under bubblewrap: no network, a read-only `/usr`, no `$HOME`, only the work directory writable. Where bwrap is missing or cannot create namespaces, it runs without a sandbox and logs a warning. That is why the option is off by default; without it SRPMs go to Koji untouched.

## ML resolver

`MLPackageResolver` maps a dependency string to an RPM and an SRPM name by similarity to provides it has seen.

- **Data.** `vibebuild train --release N` follows the Fedora metalink to a mirror's `repomd.xml`, checks it by the metalink's sha256, then downloads `primary.xml` checked by the sha256 in `repomd.xml`. Every virtual provide of a binary package (`python3dist(...)`, `pkgconfig(...)`, `perl(...)`, ...), and every prefixed alias such as `python3-foo` on a package named otherwise, becomes a pair with its RPM and SRPM name. Shared-library and file provides are skipped.
- **Model.** TF-IDF over character 2- to 5-grams (20,000 features) and cosine nearest neighbours (k = 10). The nearest provide decides the SRPM if its distance is at most 0.3; among the neighbours from that SRPM the most canonical RPM name wins (`python3-X` over `python3.13-X`, then the shorter one). Anything further away gets no answer.
- **Storage.** `~/.local/share/vibebuild/model.npz` holds plain arrays (names, vocabulary, idf weights) and is loaded with `allow_pickle=False`, so a model file cannot run code. The TF-IDF matrix is recomputed on load, about 10 seconds for a full release, which keeps the file at 16 MB instead of about 280 MB.
- **Loading.** scikit-learn is imported and the model loaded only on the first prediction, so commands that never need ML do not pay for them. Without scikit-learn or a model file the resolver reports itself unavailable and the rules work alone.

Metrics from `vibebuild train --release 44` (x86_64, July 2026): 355,260 pairs, 5% held out, 500 of those evaluated. The model answered for 97.2% of them; when it answered, the SRPM was right in 64.6% and the exact RPM name in 28.6%.

Limitations:

- The held-out provides come from packages the model has seen through their other provides, so these numbers are optimistic for really unknown names.
- The exact RPM name is right much less often than the SRPM. The SRPM is what gets downloaded, so it is the answer that matters.
- A model describes one release. Train it for the release you build against.
- The rules already resolve the common prefixes, so the model matters for a minority of names. How much it adds over the rules on real packages has not been measured yet.

## Performance

- **Lazy imports.** scikit-learn and the model load on first use; `vibebuild --help` does not touch them.
- **Fewer hub calls.** Latest-build lookups are cached per run and fetched in multicalls; the repo is regenerated only when the tag changed.
- **Parallel downloads** of dependency SRPMs, and a verified SRPM cache across runs.
- **Adaptive polling.** Tasks are polled every 2 seconds for the first 30 seconds, every 10 seconds up to 3 minutes, then every 30 seconds.
- **Idempotent runs.** An NVR already in the destination tag is not rebuilt, so an interrupted chain can simply be run again.
- **Retries.** Downloads retry transient network errors three times with exponential backoff; programming errors are not retried. Anonymous hub calls are retried by the Koji library.

## Errors, exit codes and logs

```
VibeBuildError
├── InvalidSRPMError            not an SRPM, or rpm cannot read it
├── SRPMNotFoundError           no such file, or no build in Fedora Koji
├── DependencyResolutionError
│   └── CircularDependencyError
├── KojiBuildError              submission refused by the hub
└── KojiConnectionError         the hub cannot be reached
```

The CLI exits with 0 on success, 1 on any failure, and 2 on invalid arguments. A failed build is not an exception: it is a task in the result with its state and a short error, and makes the exit code 1.

Logs go to stderr; the summary goes to stdout. `-v` adds debug messages, `-q` keeps only warnings and errors, and `--log-format json` writes one JSON object per line for log collectors.
