"""
Koji builder - orchestrates package builds with dependency resolution.
"""

import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import koji  # type: ignore[import-untyped]

from vibebuild.analyzer import get_build_requires, get_package_info_from_srpm
from vibebuild.exceptions import KojiBuildError
from vibebuild.fetcher import SRPMFetcher
from vibebuild.name_resolver import PackageNameResolver
from vibebuild.resolver import DependencyResolver, KojiClient
from vibebuild.spec_patcher import cleanup_patched, patch_srpm
from vibebuild.toolchain import detect_toolchains

logger = logging.getLogger(__name__)


def _adaptive_poll_interval(elapsed_s: float, max_interval: int = 30) -> float:
    """
    Returns the interval between poll requests depending on how long the
    build has been running. Short builds react quickly, long ones save
    requests to Koji.

    Profile:
      0 - 30s   ->  2 seconds
      30 - 180s ->  10 seconds
      > 180s    ->  max_interval (default 30)
    """
    if elapsed_s < 30:
        return min(2.0, max_interval)
    if elapsed_s < 180:
        return min(10.0, max_interval)
    return float(max_interval)


class BuildStatus(Enum):
    """Status of a build task."""

    PENDING = "pending"
    BUILDING = "building"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELED = "canceled"
    ALREADY_BUILT = "already_built"  # idempotency skip


@dataclass
class BuildTask:
    """Represents a single build task."""

    package_name: str
    srpm_path: str
    target: str
    task_id: Optional[int] = None
    status: BuildStatus = BuildStatus.PENDING
    error_message: Optional[str] = None
    nvr: Optional[str] = None


@dataclass
class BuildResult:
    """Result of a vibebuild operation."""

    success: bool
    tasks: list[BuildTask] = field(default_factory=list)
    failed_packages: list[str] = field(default_factory=list)
    built_packages: list[str] = field(default_factory=list)
    total_time: float = 0.0
    toolchains: list[str] = field(default_factory=list)
    tagged_target: Optional[str] = None
    tagged_deps: list[str] = field(default_factory=list)


class KojiBuilder:
    """
    Orchestrates Koji builds with automatic dependency resolution.

    This is the main class that implements the vibebuild functionality:
    1. Analyzes SRPM to find BuildRequires
    2. Checks which dependencies are missing in Koji
    3. Downloads missing SRPMs from Fedora
    4. Builds dependencies in correct order
    5. Waits for repo regeneration between builds
    6. Finally builds the target package
    """

    def __init__(
        self,
        koji_server: str = "https://koji.fedoraproject.org/kojihub",
        koji_web_url: str = "https://koji.fedoraproject.org/koji",
        cert: Optional[str] = None,
        serverca: Optional[str] = None,
        target: str = "fedora-target",
        build_tag: str = "fedora-build",
        scratch: bool = False,
        nowait: bool = False,
        download_dir: Optional[str] = None,
        no_ssl_verify: bool = False,
        no_name_resolution: bool = False,
        no_ml: bool = False,
        ml_model_path: Optional[str] = None,
        fedora_release: str = "rawhide",
        force: bool = False,
        idempotent: bool = False,
        build_all_deps: bool = False,
        dest_tag: Optional[str] = None,
        patch_specs: bool = False,
    ):
        self.koji_server = koji_server
        self.koji_web_url = koji_web_url
        self.cert = cert
        self.serverca = serverca
        self.target = target
        self.dest_tag = dest_tag or target  # tag the target's builds land in
        self.build_tag = build_tag
        self.scratch = scratch
        self.nowait = nowait
        self.no_ssl_verify = no_ssl_verify
        self.force = force  # disable the idempotency skip
        self.idempotent = idempotent  # enable the idempotency pre-check (off by default)
        self.build_all_deps = build_all_deps
        self.patch_specs = patch_specs  # strip %check / allow unpackaged files via local rpmbuild

        self.koji_client = KojiClient(
            server=koji_server,
            web_url=koji_web_url,
            cert=cert,
            serverca=serverca,
            no_ssl_verify=no_ssl_verify,
        )

        # Create name resolver
        self.name_resolver = None
        if not no_name_resolution:
            ml_resolver = None
            if not no_ml:
                try:
                    from vibebuild.ml_resolver import MLPackageResolver

                    ml_resolver = MLPackageResolver(model_path=ml_model_path)
                    if not ml_resolver.is_available():
                        ml_resolver = None
                except ImportError:
                    ml_resolver = None
            self.name_resolver = PackageNameResolver(ml_resolver=ml_resolver)

        self.resolver = DependencyResolver(
            koji_client=self.koji_client,
            koji_tag=build_tag,
            name_resolver=self.name_resolver,
            build_all_deps=build_all_deps,
        )

        self.fetcher = SRPMFetcher(
            download_dir=download_dir,
            fedora_release=fedora_release,
            no_ssl_verify=no_ssl_verify,
            name_resolver=self.name_resolver,
        )

        self._tasks: list[BuildTask] = []
        self._session: Optional["koji.ClientSession"] = None

    def _get_env(self) -> Optional[dict]:
        """Get environment variables for subprocess, with SSL verification disabled if needed."""
        if self.no_ssl_verify:
            env = os.environ.copy()
            env["PYTHONHTTPSVERIFY"] = "0"
            env["REQUESTS_CA_BUNDLE"] = ""
            env["CURL_CA_BUNDLE"] = ""
            return env
        return None

    # ---- single koji session for the whole build_with_deps / build_package -------
    # koji-hub serializes RPCs under one user: two adjacent logins
    # under `kojiadmin` hit `AuthLockError`. So within a single build
    # we keep one logged-in ClientSession and route all calls through it.
    # ----------------------------------------------------------------------

    def _open_session(self) -> "koji.ClientSession":
        """Create and authenticate a persistent ClientSession."""
        opts = {"no_ssl_verify": True} if self.no_ssl_verify else {}
        session = koji.ClientSession(self.koji_server, opts=opts)
        session.ssl_login(self.cert, self.serverca, self.serverca)
        self._session = session
        return session

    def _close_session(self) -> None:
        """Log out and reset the current session (silently, if there is none)."""
        if self._session is None:
            return
        try:
            self._session.logout()
        except Exception:  # noqa: BLE001
            pass
        self._session = None

    def _get_session(self) -> "koji.ClientSession":
        """Return the current session or open a new one on demand."""
        if self._session is None:
            self._open_session()
        return self._session  # type: ignore[return-value]

    def _submit_build(
        self, srpm_path: str, add_unpackaged_macro: bool = False, scratch: bool = False
    ) -> BuildTask:
        """
        Submit a single package build to Koji (no waiting).

        Registers the package with add-pkg, submits with --nowait,
        and returns a BuildTask with status BUILDING.

        Args:
            srpm_path: Path to SRPM file
            add_unpackaged_macro: inject `_unpackaged_files_terminate_build 0`.
                Only on retry; on the first attempt always False.
            scratch: submit a scratch build (never tagged). Only for the target:
                dependencies have to land in the tag for the target to use them.

        Returns:
            BuildTask with task_id and status BUILDING
        """
        srpm_path = Path(srpm_path)
        if not srpm_path.exists():
            raise FileNotFoundError(f"SRPM not found: {srpm_path}")

        # With patch_specs the spec loses %check (upstream testsuites may need network
        # or process limits our buildroot lacks). The macro
        # `_unpackaged_files_terminate_build 0` is added only on retry.
        patched: Optional[str] = None
        if self.patch_specs:
            try:
                patched = patch_srpm(str(srpm_path), add_unpackaged_macro=add_unpackaged_macro)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "spec patching skipped for %s: %s — using original SRPM",
                    srpm_path.name,
                    exc,
                )
        try:
            return self._submit_srpm(srpm_path, Path(patched) if patched else srpm_path, scratch)
        finally:
            if patched:
                cleanup_patched(patched)

    def _submit_srpm(self, srpm_path: Path, upload: Path, scratch: bool) -> BuildTask:
        """Submit `upload` (srpm_path itself or its patched copy) and track it as srpm_path."""
        package_info = get_package_info_from_srpm(str(upload))

        task = BuildTask(
            package_name=package_info.name,
            srpm_path=str(srpm_path),
            target=self.target,
            nvr=package_info.nvr,
        )

        # Idempotency: if the target tag already has a fresh build with this NVR,
        # do not submit again, return ALREADY_BUILT. Enabled via
        # `idempotent=True` in __init__ (the CLI sets it by default).
        # The skip is disabled via self.force.
        if getattr(self, "idempotent", False) and not getattr(self, "force", False) and not scratch:
            try:
                latest = self.koji_client.latest_build(package_info.name, self.dest_tag)
                if latest and latest.get("nvr") == package_info.nvr:
                    logger.info(
                        "  %s already built in %s (NVR=%s), skipping",
                        package_info.name,
                        self.dest_tag,
                        latest["nvr"],
                    )
                    task.status = BuildStatus.ALREADY_BUILT
                    return task
            except Exception as exc:  # noqa: BLE001
                logger.debug("idempotency check skipped: %s", exc)

        logger.info(f"Starting build: {package_info.nvr}")

        try:
            task.task_id = self._submit_via_session(
                upload, package_info.name, self.dest_tag, scratch
            )
        except KojiBuildError:
            task.status = BuildStatus.FAILED
            raise
        except Exception as exc:  # noqa: BLE001
            task.status = BuildStatus.FAILED
            task.error_message = str(exc)
            raise KojiBuildError(f"Build failed: {exc}")

        logger.info(f"Build submitted: task_id={task.task_id}")
        task.status = BuildStatus.BUILDING

        return task

    def _submit_via_session(
        self, srpm_path: Path, package_name: str, dest_tag: str, scratch: bool = False
    ) -> int:
        """Through the persistent session do add-pkg + uploadWrapper + build atomically.

        Takes the current `self._session` (opens a new one if there is none). Does
        not close the session: the lifecycle owner is `build_with_deps` / `build_package`.
        """
        session = self._get_session()
        try:
            session.packageListAdd(dest_tag, package_name, owner="kojiadmin")
        except koji.GenericError as exc:
            if "already" not in str(exc).lower():
                raise KojiBuildError(f"packageListAdd failed: {exc}")

        server_dir = f"cli-build/vibebuild-{uuid.uuid4().hex[:12]}"
        session.uploadWrapper(str(srpm_path), server_dir)
        server_srpm = f"{server_dir}/{srpm_path.name}"

        build_opts: dict = {"scratch": True} if scratch else {}
        try:
            task_id = session.build(server_srpm, self.target, build_opts)
        except koji.GenericError as exc:
            msg = str(exc)
            if "Build already exists" in msg and "state=COMPLETE" in msg:
                # Extract existing task_id from the error message if possible,
                # otherwise return 0 as a sentinel — caller checks for None/0.
                import re as _re

                m = _re.search(r"task_id.*?(\d+)", msg)
                return int(m.group(1)) if m else 0
            raise KojiBuildError(f"session.build failed: {exc}")
        return int(task_id)

    def build_package(self, srpm_path: str, wait: bool = True) -> BuildTask:
        """
        Submit a single package build to Koji.

        On a FAILED result checks the logs for Installed (but unpackaged)
        file(s) found and in that case retries the build once with
        `_unpackaged_files_terminate_build 0`. Returns the final task
        (the original or the retry task).

        Args:
            srpm_path: Path to SRPM file
            wait: Whether to wait for build to complete

        Returns:
            BuildTask with result information
        """
        own_session = self._session is None
        if own_session:
            self._open_session()
        try:
            task = self._submit_build(srpm_path, scratch=self.scratch)

            if not (wait and not self.nowait and task.task_id):
                if wait and not self.nowait:
                    task.status = BuildStatus.COMPLETE
                return task

            task.status = self._poll_build(task.task_id, task.nvr or task.package_name)
            if task.status == BuildStatus.FAILED and self._can_retry_unpackaged(task):
                logger.info(
                    "  retrying %s with _unpackaged_files_terminate_build 0",
                    task.package_name,
                )
                retry_task = self._submit_build(
                    srpm_path, add_unpackaged_macro=True, scratch=self.scratch
                )
                if retry_task.task_id:
                    retry_task.status = self._poll_build(
                        retry_task.task_id, retry_task.nvr or retry_task.package_name
                    )
                return retry_task
            return task
        finally:
            if own_session:
                self._close_session()

    # rpmbuild error marker about Installed (but unpackaged) file(s) found.
    # Appears in build.log of the buildArch subtask. Identical across rpm versions.
    _UNPACKAGED_FILES_MARKER = "Installed (but unpackaged) file(s) found"

    # How many bytes to read from the end of build.log when looking for the marker. Real
    # rpmbuild errors are printed at the end of the log; we read the tail so as not to pull
    # the whole multi-megabyte log.
    _UNPACKAGED_FILES_LOG_TAIL_BYTES = 200_000

    def _task_has_unpackaged_files_error(self, task_id: int) -> bool:
        """Check the task logs (including subtasks) for the unpackaged-files marker.

        Via `listTaskOutput(stat=True)` gets the size of `build.log`, reads
        the tail (the last `_UNPACKAGED_FILES_LOG_TAIL_BYTES` bytes) and looks for
        the marker. A negative offset is not supported by koji-hub (it returns
        `OSError: Invalid argument`), so we compute the offset manually.
        On any error accessing the logs returns False: the builder does not
        retry, treating the refusal as an ordinary build failure.
        """
        session = self._get_session()
        candidate_ids: list[int] = [task_id]
        try:
            subs = session.listTasks(opts={"parent": task_id}) or []
            candidate_ids.extend(int(s["id"]) for s in subs if s.get("id") is not None)
        except Exception as exc:  # noqa: BLE001
            logger.debug("listTasks(parent=%s) failed: %s", task_id, exc)

        for tid in candidate_ids:
            try:
                stats = session.listTaskOutput(tid, stat=True) or {}
            except Exception as exc:  # noqa: BLE001
                logger.debug("listTaskOutput(%s, stat=True) failed: %s", tid, exc)
                continue
            if not isinstance(stats, dict) or "build.log" not in stats:
                continue
            try:
                size = int(stats["build.log"].get("st_size", 0))
            except (TypeError, ValueError):
                size = 0
            offset = max(0, size - self._UNPACKAGED_FILES_LOG_TAIL_BYTES)
            try:
                chunk = session.downloadTaskOutput(tid, "build.log", offset=offset, size=-1)
            except Exception as exc:  # noqa: BLE001
                logger.debug("downloadTaskOutput(%s, build.log) failed: %s", tid, exc)
                continue
            text = (
                chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)
            )
            if self._UNPACKAGED_FILES_MARKER in text:
                logger.info(
                    "  detected unpackaged-files error in task %s build.log — will retry",
                    tid,
                )
                return True
        return False

    # koji task-state numeric codes -> names.
    _KOJI_TASK_STATES = {
        0: "free",
        1: "open",
        2: "closed",
        3: "canceled",
        4: "assigned",
        5: "failed",
    }

    @classmethod
    def _state_name(cls, state_int: Optional[int]) -> str:
        if state_int is None:
            return "unknown"
        return cls._KOJI_TASK_STATES.get(state_int, "unknown")

    def _poll_build(
        self, task_id: int, nvr: str, timeout: int = 7200, interval: int = 30
    ) -> BuildStatus:
        """Poll a build task with a progress log via the persistent ClientSession."""
        session = self._get_session()
        start = time.time()
        last_state = ""

        while time.time() - start < timeout:
            try:
                info = session.getTaskInfo(task_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"  [{nvr}] getTaskInfo failed: {exc}")
                time.sleep(_adaptive_poll_interval(time.time() - start, interval))
                continue

            state = self._state_name(info.get("state") if info else None)

            # Subtasks for detailed progress (best-effort).
            subtasks: list[str] = []
            try:
                subs = session.listTasks(opts={"parent": task_id}) or []
                for sub in subs[:3]:
                    subtasks.append(
                        f"{sub.get('method', '?')} [{self._state_name(sub.get('state'))}]"
                    )
            except Exception:  # noqa: BLE001
                pass

            elapsed = int(time.time() - start)
            minutes, seconds = divmod(elapsed, 60)
            if subtasks:
                progress = f"  [{nvr}] {minutes}m{seconds:02d}s — {', '.join(subtasks)}"
            else:
                progress = f"  [{nvr}] {minutes}m{seconds:02d}s — {state}"
            if progress != last_state:
                logger.info(progress)
                last_state = progress

            if state == "closed":
                return BuildStatus.COMPLETE
            if state == "failed":
                logger.error(f"  [{nvr}] Build FAILED (task {task_id})")
                return BuildStatus.FAILED
            if state == "canceled":
                return BuildStatus.CANCELED

            time.sleep(_adaptive_poll_interval(elapsed, interval))

        logger.error(f"  [{nvr}] Build timed out after {timeout}s")
        return BuildStatus.FAILED

    def _poll_builds(self, tasks: list, timeout: int = 7200, interval: int = 30) -> None:
        """Parallel polling of several tasks through one ClientSession."""
        pending = {t.task_id: t for t in tasks if t.task_id}
        if not pending:
            return

        session = self._get_session()
        start = time.time()

        while pending and time.time() - start < timeout:
            completed_ids = []
            for task_id, task in pending.items():
                try:
                    info = session.getTaskInfo(task_id)
                except Exception:  # noqa: BLE001
                    continue
                state = self._state_name(info.get("state") if info else None)
                if state == "closed":
                    task.status = BuildStatus.COMPLETE
                    completed_ids.append(task_id)
                elif state == "failed":
                    task.status = BuildStatus.FAILED
                    task.error_message = f"Build task {task_id} failed"
                    completed_ids.append(task_id)
                elif state == "canceled":
                    task.status = BuildStatus.CANCELED
                    completed_ids.append(task_id)

            for tid in completed_ids:
                del pending[tid]

            if pending:
                elapsed_s = time.time() - start
                minutes, seconds = divmod(int(elapsed_s), 60)
                names = ", ".join(t.package_name for t in pending.values())
                logger.info(f"  [{minutes}m{seconds:02d}s] Waiting for: {names}")
                time.sleep(_adaptive_poll_interval(elapsed_s, interval))

        for task in pending.values():
            task.status = BuildStatus.FAILED
            task.error_message = f"Build timed out after {timeout}s"
            logger.error(f"  [{task.package_name}] Build timed out after {timeout}s")

    def _repo_is_current(self) -> bool:
        """True if the build tag has a ready repo and nothing it inherits changed since."""
        session = self._get_session()
        try:
            repo = session.getRepo(self.build_tag)
            if not repo:
                return False
            tag_ids = [session.getTag(self.build_tag, strict=True)["id"]]
            tag_ids += [link["parent_id"] for link in session.getFullInheritance(self.build_tag)]
            return not session.tagChangedSinceEvent(repo["create_event"], tag_ids)
        except Exception as exc:  # noqa: BLE001
            logger.debug("repo freshness check for %s failed: %s", self.build_tag, exc)
            return False

    def _ensure_repo_ready(self) -> None:
        """Regenerate the build tag repo unless it is already current, and wait for it."""
        if self._repo_is_current():
            logger.info(f"Repo for {self.build_tag} is current")
            return
        session = self._get_session()
        try:
            task_id = session.newRepo(self.build_tag)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"newRepo {self.build_tag} failed to start: {exc}")
            return
        logger.info(f"Triggered newRepo for {self.build_tag} (task {task_id})")
        logger.info(f"Waiting for repo to be ready: {self.build_tag}")
        state = self._await_task(task_id, timeout=1800)
        if state == "closed":
            logger.info("Repo is ready")
        else:
            logger.warning(f"newRepo ended in state {state}, proceeding anyway")

    def wait_for_repo(self, tag: Optional[str] = None, timeout: int = 1800) -> bool:
        """Trigger newRepo and wait. Returns True on `closed`."""
        tag = tag or self.build_tag
        logger.info(f"Waiting for repo regeneration: {tag}")
        session = self._get_session()
        try:
            task_id = session.newRepo(tag)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"newRepo failed: {exc}")
            return False
        logger.info(f"Triggered newRepo for {tag} (task {task_id})")
        state = self._await_task(task_id, timeout=timeout)
        self.koji_client.clear_cache()
        if state == "closed":
            logger.info("Repo regenerated successfully")
            return True
        logger.warning(f"wait-repo ended in state {state}")
        return False

    def _await_task(self, task_id: int, timeout: int = 1800) -> str:
        """Poll a task until a terminal state through the current session.

        Returns the state name (closed/failed/canceled) or `timeout`.
        """
        session = self._get_session()
        start = time.time()
        while time.time() - start < timeout:
            try:
                info = session.getTaskInfo(task_id)
            except Exception:  # noqa: BLE001
                time.sleep(_adaptive_poll_interval(time.time() - start))
                continue
            state = self._state_name(info.get("state") if info else None)
            if state in ("closed", "failed", "canceled"):
                return state
            time.sleep(_adaptive_poll_interval(time.time() - start))
        return "timeout"

    def build_with_deps(self, srpm_path: str) -> BuildResult:
        """
        Build package with automatic dependency resolution.

        This is the main vibebuild function. It:
        1. Analyzes the SRPM for BuildRequires
        2. Finds which dependencies are missing
        3. Downloads SRPMs for missing deps from Fedora
        4. Recursively resolves all dependencies
        5. Builds everything in correct order

        Args:
            srpm_path: Path to SRPM file to build

        Returns:
            BuildResult with all build information
        """
        start_time = time.time()
        result = BuildResult(success=True)

        srpm_path = Path(srpm_path)
        if not srpm_path.exists():
            raise FileNotFoundError(f"SRPM not found: {srpm_path}")

        logger.info(f"Starting vibebuild for: {srpm_path}")

        # Open one session for the whole build_with_deps: all koji RPCs go
        # through it to avoid AuthLockError from per-user serialization.
        own_session = self._session is None
        if own_session:
            self._open_session()
        try:
            return self._build_with_deps_impl(srpm_path, result, start_time)
        finally:
            if own_session:
                self._close_session()

    def _build_with_deps_impl(
        self, srpm_path: Path, result: BuildResult, start_time: float
    ) -> BuildResult:
        """Body of build_with_deps without session management: we are already logged in on it."""
        # Ensure repo is ready before dependency resolution and builds
        self._ensure_repo_ready()

        package_info = get_package_info_from_srpm(str(srpm_path))
        logger.info(f"Package: {package_info.nvr}")

        logger.info("Analyzing dependencies...")

        _download_seen: set[str] = set()

        def srpm_resolver(pkg_name: str) -> Optional[str]:
            if pkg_name in _download_seen:
                return None
            _download_seen.add(pkg_name)
            try:
                logger.info(f"Downloading dependency: {pkg_name}")
                path = self.fetcher.download_srpm(pkg_name)
                logger.info(f"Downloaded: {pkg_name} -> {path}")
                return path
            except Exception as e:
                logger.warning(f"Could not download SRPM for {pkg_name}: {e}")
                return None

        self.resolver.build_dependency_graph(
            package_info.name, str(srpm_path), srpm_resolver=srpm_resolver
        )

        build_chain = self.resolver.get_build_chain()
        # Set when something was tagged since the last newRepo: the next level (or
        # the target) needs a fresh repo to see it.
        repo_stale = False

        if not build_chain:
            logger.info("No dependencies to build, proceeding with target package")
        else:
            total_deps = sum(len(level) for level in build_chain)
            logger.info(f"Found {total_deps} packages to build in {len(build_chain)} levels")
            if self.scratch:
                logger.info("Dependencies are regular builds: scratch builds are never tagged")

            for level_idx, level in enumerate(build_chain):
                if repo_stale:
                    self.wait_for_repo()
                    repo_stale = False
                logger.info(f"Building level {level_idx + 1}/{len(build_chain)}: {level}")

                # Submit all packages in this level in parallel
                level_tasks = []
                for pkg_name in level:
                    node = self.resolver._dependency_graph.get(pkg_name)
                    if not node or not node.srpm_path:
                        logger.warning(f"Skipping {pkg_name}: no SRPM available")
                        continue

                    try:
                        task = self._submit_build(node.srpm_path)
                        level_tasks.append(task)
                        result.tasks.append(task)
                    except Exception as e:
                        logger.error(f"Failed to submit {pkg_name}: {e}")
                        result.failed_packages.append(pkg_name)

                # Poll all submitted tasks simultaneously
                if level_tasks:
                    self._poll_builds(level_tasks)
                    self._retry_unpackaged_failures(level_tasks)

                    for task in level_tasks:
                        if task.status == BuildStatus.COMPLETE:
                            result.built_packages.append(task.package_name)
                            repo_stale = True
                        else:
                            result.failed_packages.append(task.package_name)
                            result.success = False

        if repo_stale:
            self.wait_for_repo()

        if result.success or not result.failed_packages:
            logger.info(f"Building target package: {package_info.nvr}")

            try:
                task = self.build_package(str(srpm_path), wait=True)
                result.tasks.append(task)

                if task.status == BuildStatus.COMPLETE:
                    result.built_packages.append(package_info.name)
                elif task.status == BuildStatus.BUILDING and self.nowait:
                    # --nowait: build was submitted successfully, not a failure
                    result.built_packages.append(package_info.name)
                else:
                    result.failed_packages.append(package_info.name)
                    result.success = False

            except Exception as e:
                logger.error(f"Failed to build target package: {e}")
                result.failed_packages.append(package_info.name)
                result.success = False

        result.toolchains = self._collect_toolchains(package_info.build_requires)
        self._populate_tagged_builds(result, package_info.name)

        result.total_time = time.time() - start_time

        logger.info(f"VibeBuild complete in {result.total_time:.1f}s")
        logger.info(f"Built: {len(result.built_packages)}, Failed: {len(result.failed_packages)}")

        return result

    def _can_retry_unpackaged(self, task: BuildTask) -> bool:
        """True if a failed task hit unpackaged files and may be retried with the macro."""
        if not self._task_has_unpackaged_files_error(task.task_id):
            return False
        if not self.patch_specs:
            logger.info(
                "  %s failed on unpackaged files; --patch-specs retries it with "
                "_unpackaged_files_terminate_build 0",
                task.package_name,
            )
            return False
        return True

    def _retry_unpackaged_failures(self, level_tasks: list[BuildTask]) -> None:
        """For every FAILED task in the level: if the log has the unpackaged-files marker,
        retry the build once with `_unpackaged_files_terminate_build 0`.

        Mutates the task in place: replaces task_id/status/nvr with the retry result,
        so that `tasks/_populate_tagged_builds` see the actual build.
        """
        retried: list[BuildTask] = []
        for task in level_tasks:
            if task.status != BuildStatus.FAILED or not task.task_id:
                continue
            if not self._can_retry_unpackaged(task):
                continue
            logger.info(
                "  retrying %s with _unpackaged_files_terminate_build 0",
                task.package_name,
            )
            try:
                new_task = self._submit_build(task.srpm_path, add_unpackaged_macro=True)
            except Exception as exc:  # noqa: BLE001
                logger.error("  retry submit failed for %s: %s", task.package_name, exc)
                continue
            task.task_id = new_task.task_id
            task.srpm_path = new_task.srpm_path
            task.nvr = new_task.nvr
            task.status = BuildStatus.BUILDING
            task.error_message = None
            retried.append(task)
        if retried:
            self._poll_builds(retried)

    def _collect_toolchains(self, root_build_requires) -> list[str]:
        """Aggregate BR from root + every built dep, classify via detect_toolchains."""
        names: list[str] = [
            br.name if hasattr(br, "name") else str(br) for br in root_build_requires
        ]
        for node in self.resolver._dependency_graph.values():
            if not node.srpm_path:
                continue
            try:
                names.extend(br.name for br in get_build_requires(node.srpm_path))
            except Exception as exc:
                logger.debug("get_build_requires(%s) failed: %s", node.srpm_path, exc)
        return detect_toolchains(names)

    def _populate_tagged_builds(self, result: BuildResult, target_name: str) -> None:
        """Query koji list-tagged --latest <dest-tag> and split target vs deps."""
        try:
            tagged = self.koji_client.list_tagged_builds(self.dest_tag)
        except Exception as exc:
            logger.warning("list-tagged for %s failed: %s", self.dest_tag, exc)
            return
        result.tagged_target = None if self.scratch else tagged.get(target_name)
        if target_name in result.built_packages and not result.tagged_target and not self.scratch:
            logger.warning("target %s built, but not found in tag %s", target_name, self.dest_tag)
        for pkg in result.built_packages:
            if pkg == target_name:
                continue
            nvr = tagged.get(pkg)
            if nvr:
                result.tagged_deps.append(nvr)
            else:
                logger.warning("dep %s built, but not found in tag %s", pkg, self.dest_tag)

    def build_chain(self, packages: list[tuple[str, str]]) -> BuildResult:
        """
        Build multiple packages in order.

        Args:
            packages: List of (package_name, srpm_path) tuples

        Returns:
            BuildResult with all build information
        """
        start_time = time.time()
        result = BuildResult(success=True)

        for pkg_name, srpm_path in packages:
            try:
                task = self.build_package(srpm_path, wait=True)
                result.tasks.append(task)

                if task.status == BuildStatus.COMPLETE:
                    result.built_packages.append(pkg_name)
                    self.wait_for_repo()
                elif task.status == BuildStatus.BUILDING and self.nowait:
                    result.built_packages.append(pkg_name)
                else:
                    result.failed_packages.append(pkg_name)
                    result.success = False
                    break

            except Exception as e:
                logger.error(f"Failed to build {pkg_name}: {e}")
                result.failed_packages.append(pkg_name)
                result.success = False
                break

        result.total_time = time.time() - start_time
        return result

    def get_build_status(self, task_id: int) -> BuildStatus:
        """Current state of a task through the persistent ClientSession."""
        session = self._get_session()
        try:
            info = session.getTaskInfo(task_id)
        except Exception:  # noqa: BLE001
            return BuildStatus.FAILED
        if not info:
            return BuildStatus.FAILED
        state = self._state_name(info.get("state"))
        if state == "closed":
            return BuildStatus.COMPLETE
        if state == "failed":
            return BuildStatus.FAILED
        if state == "canceled":
            return BuildStatus.CANCELED
        if state in ("open", "free", "assigned"):
            return BuildStatus.BUILDING
        return BuildStatus.PENDING

    def cancel_build(self, task_id: int) -> bool:
        """Cancel a task through the persistent ClientSession."""
        session = self._get_session()
        try:
            session.cancelTask(task_id)
        except Exception:  # noqa: BLE001
            return False
        return True
