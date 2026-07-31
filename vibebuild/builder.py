"""
Koji builder - orchestrates package builds with dependency resolution.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from vibebuild.analyzer import get_build_requires, get_package_info_from_srpm
from vibebuild.exceptions import KojiBuildError, SRPMNotFoundError
from vibebuild.fetcher import SRPMFetcher
from vibebuild.koji_client import KojiClient, KojiSession, poll_interval, task_state_name
from vibebuild.name_resolver import PackageNameResolver
from vibebuild.resolver import DEFAULT_MAX_PACKAGES, DependencyResolver
from vibebuild.spec_patcher import check_can_patch, cleanup_patched, patch_srpm
from vibebuild.toolchain import detect_toolchains

logger = logging.getLogger(__name__)


class BuildStatus(Enum):
    """Status of a build task."""

    PENDING = "pending"
    BUILDING = "building"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELED = "canceled"
    ALREADY_BUILT = "already_built"  # the NVR is already built, nothing submitted


# Final Koji task states and what they mean for a build.
_FINAL_STATUS = {
    "closed": BuildStatus.COMPLETE,
    "failed": BuildStatus.FAILED,
    "canceled": BuildStatus.CANCELED,
}

# A waiting build logs a progress line when its state changes, and at least this often (s)
PROGRESS_EVERY = 300


def _elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s"


@dataclass
class BuildTask:
    """Represents a single build task."""

    package_name: str
    srpm_path: str
    target: str
    task_id: int | None = None
    status: BuildStatus = BuildStatus.PENDING
    error_message: str | None = None
    nvr: str | None = None


@dataclass
class BuildResult:
    """Result of a vibebuild operation."""

    success: bool
    tasks: list[BuildTask] = field(default_factory=list)
    failed_packages: list[str] = field(default_factory=list)
    built_packages: list[str] = field(default_factory=list)
    already_built: list[str] = field(default_factory=list)  # already in Koji, nothing submitted
    total_time: float = 0.0
    toolchains: list[str] = field(default_factory=list)
    tagged_target: str | None = None
    tagged_deps: list[str] = field(default_factory=list)
    skipped_packages: list[str] = field(default_factory=list)  # not built: a dependency failed


class KojiBuilder:
    """
    Orchestrates Koji builds with automatic dependency resolution.

    1. Analyzes the SRPM to find its BuildRequires
    2. Checks which dependencies are missing in the build tag
    3. Downloads their SRPMs from Fedora, recursively
    4. Builds them level by level, regenerating the repo in between
    5. Finally builds the target package
    """

    def __init__(
        self,
        koji_server: str,
        target: str,
        build_tag: str,
        dest_tag: str | None = None,
        cert: str | None = None,
        serverca: str | None = None,
        no_ssl_verify: bool = False,
        scratch: bool = False,
        nowait: bool = False,
        build_all_deps: bool = False,
        patch_specs: bool = False,
        allow_unsandboxed: bool = False,
        max_deps: int = DEFAULT_MAX_PACKAGES,
        name_resolver: PackageNameResolver | None = None,
        download_dir: str | None = None,
        fedora_release: str = "rawhide",
    ):
        self.target = target
        self.build_tag = build_tag
        self.dest_tag = dest_tag or target  # tag the target's builds land in
        self.scratch = scratch
        self.nowait = nowait
        self.patch_specs = patch_specs  # strip %check / allow unpackaged files via local rpmbuild
        self.allow_unsandboxed = allow_unsandboxed  # patch without bwrap where it cannot sandbox
        self._repo_checked = False  # repo freshness checked in this run

        self.koji_client = KojiClient(
            server=koji_server, serverca=serverca, no_ssl_verify=no_ssl_verify
        )
        self.hub = KojiSession(
            server=koji_server, cert=cert, serverca=serverca, no_ssl_verify=no_ssl_verify
        )
        self.resolver = DependencyResolver(
            koji_client=self.koji_client,
            koji_tag=build_tag,
            name_resolver=name_resolver,
            build_all_deps=build_all_deps,
            max_packages=max_deps,
        )
        self.fetcher = SRPMFetcher(
            download_dir=download_dir,
            fedora_release=fedora_release,
            name_resolver=name_resolver,
        )

    def _submit_build(
        self, srpm_path: str, add_unpackaged_macro: bool = False, scratch: bool = False
    ) -> BuildTask:
        """
        Submit a single package build to Koji without waiting for it.

        Args:
            srpm_path: Path to SRPM file
            add_unpackaged_macro: inject `_unpackaged_files_terminate_build 0`.
                Only on retry; on the first attempt always False.
            scratch: submit a scratch build (never tagged). Only for the target:
                dependencies have to land in the tag for the target to use them.

        Returns:
            BuildTask with status BUILDING, or ALREADY_BUILT if the NVR is built
        """
        srpm = Path(srpm_path)
        if not srpm.exists():
            raise SRPMNotFoundError(f"SRPM not found: {srpm}")

        # With patch_specs the spec loses %check (upstream testsuites may need network
        # or process limits our buildroot lacks). The macro
        # `_unpackaged_files_terminate_build 0` is added only on retry.
        patched: str | None = None
        if self.patch_specs:
            try:
                patched = patch_srpm(
                    str(srpm),
                    add_unpackaged_macro=add_unpackaged_macro,
                    allow_unsandboxed=self.allow_unsandboxed,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "spec patching skipped for %s: %s — using original SRPM", srpm.name, exc
                )
        try:
            return self._submit_srpm(srpm, Path(patched) if patched else srpm, scratch)
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

        if not scratch and self._already_built(package_info.name, package_info.nvr):
            task.status = BuildStatus.ALREADY_BUILT
            return task

        logger.info("Starting build: %s", package_info.nvr)
        try:
            task.task_id = self.hub.submit(
                upload, package_info.name, self.target, self.dest_tag, scratch
            )
        except KojiBuildError as exc:
            task.status = BuildStatus.FAILED
            task.error_message = str(exc)
            raise
        except Exception as exc:  # noqa: BLE001
            task.status = BuildStatus.FAILED
            task.error_message = str(exc)
            raise KojiBuildError(f"Build failed: {exc}") from exc

        if task.task_id is None:
            logger.info("  %s is already built in Koji", package_info.nvr)
            task.status = BuildStatus.ALREADY_BUILT
        else:
            logger.info("Build submitted: task_id=%s", task.task_id)
            task.status = BuildStatus.BUILDING
        return task

    def _already_built(self, package: str, nvr: str) -> bool:
        """True if the destination tag already has this NVR as the latest build."""
        try:
            latest = self.koji_client.latest_build(package, self.dest_tag)
        except Exception as exc:  # noqa: BLE001
            logger.debug("already-built check skipped: %s", exc)
            return False
        if latest and latest.get("nvr") == nvr:
            logger.info("  %s already built in %s, skipping", nvr, self.dest_tag)
            return True
        return False

    def build_package(self, srpm_path: str, wait: bool = True) -> BuildTask:
        """
        Submit a single package build to Koji, waiting for it unless nowait is set.

        A build failing on "Installed (but unpackaged) file(s) found" is retried
        once with `_unpackaged_files_terminate_build 0` (needs patch_specs).

        Returns:
            The final task: the original one or the retry
        """
        with self.hub:
            task = self._submit_build(srpm_path, scratch=self.scratch)
            if not task.task_id or not wait or self.nowait:
                return task

            self._wait_for(task)
            if task.status != BuildStatus.FAILED or not self._can_retry_unpackaged(task):
                return task

            logger.info("  retrying %s with _unpackaged_files_terminate_build 0", task.package_name)
            retry = self._submit_build(srpm_path, add_unpackaged_macro=True, scratch=self.scratch)
            if retry.task_id:
                self._wait_for(retry)
            return retry

    def _wait_for(self, task: BuildTask) -> None:
        """Poll a submitted task to the end, keeping Koji's reason if it failed."""
        if task.task_id is None:
            return
        task.status = self._poll_build(task.task_id, task.nvr or task.package_name)
        if task.status == BuildStatus.FAILED:
            task.error_message = self.hub.task_error(task.task_id) or f"Task {task.task_id} failed"

    # rpmbuild's error about files the spec does not package, from build.log of
    # the buildArch subtask. Errors are at the end, so only the tail is read.
    _UNPACKAGED_FILES_MARKER = "Installed (but unpackaged) file(s) found"
    _UNPACKAGED_FILES_LOG_TAIL_BYTES = 200_000

    def _task_has_unpackaged_files_error(self, task_id: int) -> bool:
        """Check build.log of the task and its subtasks for the unpackaged-files error.

        Any error reading the logs counts as "no": the failure stays an ordinary one.
        """
        task_ids = [task_id] + [int(s["id"]) for s in self.hub.subtasks(task_id) if s.get("id")]
        for tid in task_ids:
            log = self.hub.task_log_tail(tid, "build.log", self._UNPACKAGED_FILES_LOG_TAIL_BYTES)
            if log and self._UNPACKAGED_FILES_MARKER in log:
                logger.info("  task %s failed on unpackaged files", tid)
                return True
        return False

    def _poll_build(
        self, task_id: int, nvr: str, timeout: int = 7200, interval: int = 30
    ) -> BuildStatus:
        """Wait for one build task, logging its progress (subtasks and their states).

        A line is logged when the progress changes, and at least every PROGRESS_EVERY seconds.
        """
        start = time.time()
        last_progress = ""
        last_logged = 0.0

        while time.time() - start < timeout:
            elapsed = time.time() - start
            state = self.hub.task_state(task_id)
            if state is None:
                logger.warning("  [%s] could not get the state of task %s", nvr, task_id)
                time.sleep(poll_interval(elapsed, interval))
                continue

            subtasks = [
                f"{sub.get('method', '?')} [{task_state_name(sub.get('state'))}]"
                for sub in self.hub.subtasks(task_id)[:3]
            ]
            progress = ", ".join(subtasks) or state
            if progress != last_progress or elapsed - last_logged >= PROGRESS_EVERY:
                logger.info("  [%s] %s — %s", nvr, _elapsed(elapsed), progress)
                last_progress, last_logged = progress, elapsed

            status = _FINAL_STATUS.get(state)
            if status == BuildStatus.FAILED:
                logger.error("  [%s] Build FAILED (task %s)", nvr, task_id)
            if status:
                return status
            time.sleep(poll_interval(elapsed, interval))

        logger.error("  [%s] Build timed out after %ss", nvr, timeout)
        return BuildStatus.FAILED

    def _poll_builds(self, tasks: list[BuildTask], timeout: int = 7200, interval: int = 30) -> None:
        """Wait for several build tasks at once, setting their statuses.

        Logs what is still pending when that changes, and at least every PROGRESS_EVERY seconds.
        """
        pending = {t.task_id: t for t in tasks if t.task_id}
        start = time.time()
        last_names = ""
        last_logged = 0.0

        while pending and time.time() - start < timeout:
            for task_id, task in list(pending.items()):
                status = _FINAL_STATUS.get(self.hub.task_state(task_id) or "")
                if not status:
                    continue
                task.status = status
                if status == BuildStatus.FAILED:
                    logger.error("  [%s] Build FAILED (task %s)", task.package_name, task_id)
                    task.error_message = (
                        self.hub.task_error(task_id) or f"Build task {task_id} failed"
                    )
                del pending[task_id]

            if pending:
                elapsed = time.time() - start
                names = ", ".join(t.package_name for t in pending.values())
                if names != last_names or elapsed - last_logged >= PROGRESS_EVERY:
                    logger.info("  [%s] Waiting for: %s", _elapsed(elapsed), names)
                    last_names, last_logged = names, elapsed
                time.sleep(poll_interval(elapsed, interval))

        for task in pending.values():
            task.status = BuildStatus.FAILED
            task.error_message = f"Build timed out after {timeout}s"
            logger.error("  [%s] Build timed out after %ss", task.package_name, timeout)

    def _prepare_repo(self, stale: bool) -> None:
        """Make the build tag repo usable before submitting builds.

        stale: this run tagged something since the repo was last regenerated. The
        first time in a run, a repo that is not current is regenerated as well.
        """
        if stale:
            self.wait_for_repo()
        elif not self._repo_checked:
            if self.hub.repo_is_current(self.build_tag):
                logger.info("Repo for %s is current", self.build_tag)
            else:
                self.wait_for_repo()
        self._repo_checked = True

    def wait_for_repo(self, timeout: int = 1800) -> bool:
        """Regenerate the build tag repo and wait for it. True if it is ready."""
        ready = self.hub.regenerate_repo(self.build_tag, timeout=timeout)
        self.koji_client.clear_cache()
        return ready

    def resolve_build_chain(self, srpm_path: str) -> list[list[str]]:
        """
        Find the dependencies of the SRPM that have to be built first.

        Downloads the SRPM of every missing dependency (recursively) from Fedora.

        Returns:
            Packages to build, level by level; packages of a level do not depend
            on each other
        """
        package_info = get_package_info_from_srpm(srpm_path)
        tried: set[str] = set()

        def download(pkg_name: str) -> str | None:
            if pkg_name in tried:
                return None
            tried.add(pkg_name)
            try:
                logger.info("Downloading dependency: %s", pkg_name)
                path = self.fetcher.download_srpm(pkg_name)
                logger.info("Downloaded: %s -> %s", pkg_name, path)
                return path
            except Exception as e:  # noqa: BLE001
                logger.warning("Could not download SRPM for %s: %s", pkg_name, e)
                return None

        self.resolver.build_dependency_graph(package_info.name, srpm_path, srpm_resolver=download)
        return self.resolver.get_build_chain()

    def build_with_deps(self, srpm_path: str, resolve_deps: bool = True) -> BuildResult:
        """
        Build the package after the dependencies missing from the build tag.

        Args:
            srpm_path: Path to SRPM file to build
            resolve_deps: False builds only the package itself

        Returns:
            BuildResult with all build information
        """
        if not Path(srpm_path).exists():
            raise SRPMNotFoundError(f"SRPM not found: {srpm_path}")
        if self.patch_specs:
            check_can_patch(self.allow_unsandboxed)
        logger.info("Starting vibebuild for: %s", srpm_path)
        with self.hub:
            return self._build_with_deps(str(srpm_path), resolve_deps)

    def _build_with_deps(self, srpm_path: str, resolve_deps: bool) -> BuildResult:
        start_time = time.time()
        result = BuildResult(success=True)
        package_info = get_package_info_from_srpm(srpm_path)
        logger.info("Package: %s", package_info.nvr)
        self._repo_checked = False

        # Set when something was tagged since the last newRepo: the next level (or
        # the target) needs a fresh repo to see it.
        repo_stale = False
        if resolve_deps:
            logger.info("Analyzing dependencies...")
            repo_stale = self._build_levels(self.resolve_build_chain(srpm_path), result)

        if not result.success:
            logger.error("Not building %s: a dependency failed", package_info.nvr)
            result.skipped_packages.append(package_info.name)
        elif not self.scratch and self._already_built(package_info.name, package_info.nvr):
            self._record(
                result,
                BuildTask(
                    package_name=package_info.name,
                    srpm_path=srpm_path,
                    target=self.target,
                    nvr=package_info.nvr,
                    status=BuildStatus.ALREADY_BUILT,
                ),
            )
        else:
            if resolve_deps:
                self._prepare_repo(repo_stale)
            logger.info("Building target package: %s", package_info.nvr)
            try:
                self._record(result, self.build_package(srpm_path, wait=True))
            except Exception as e:  # noqa: BLE001
                logger.error("Failed to build target package: %s", e)
                result.failed_packages.append(package_info.name)
                result.success = False

        result.toolchains = self._collect_toolchains(package_info.build_requires)
        self._populate_tagged_builds(result, package_info.name)
        result.total_time = time.time() - start_time

        logger.info("VibeBuild complete in %.1fs", result.total_time)
        logger.info(
            "Built: %d, already built: %d, failed: %d",
            len(result.built_packages),
            len(result.already_built),
            len(result.failed_packages),
        )
        return result

    def _build_levels(self, build_chain: list[list[str]], result: BuildResult) -> bool:
        """Build the dependency levels in order. Returns True if the repo is now stale.

        A failed package does not stop the later levels: whatever builds is tagged,
        and the next run skips it.
        """
        if not build_chain:
            logger.info("No dependencies to build, proceeding with target package")
            return False

        total = sum(len(level) for level in build_chain)
        logger.info("Found %d packages to build in %d levels", total, len(build_chain))
        if self.scratch:
            logger.info("Dependencies are regular builds: scratch builds are never tagged")

        repo_stale = False
        for level_idx, level in enumerate(build_chain):
            logger.info("Building level %d/%d: %s", level_idx + 1, len(build_chain), level)
            srpms = {}
            for pkg_name in level:
                node = self.resolver.dependency_graph.get(pkg_name)
                if not node or not node.srpm_path:
                    logger.warning("Skipping %s: no SRPM available", pkg_name)
                    continue
                srpms[pkg_name] = node.srpm_path
            if not srpms:
                continue
            self._prepare_repo(repo_stale)
            repo_stale = False

            level_tasks = []
            for pkg_name, srpm in srpms.items():
                try:
                    task = self._submit_build(srpm)
                except Exception as e:  # noqa: BLE001
                    logger.error("Failed to submit %s: %s", pkg_name, e)
                    result.failed_packages.append(pkg_name)
                    result.success = False
                    continue
                level_tasks.append(task)

            self._poll_builds(level_tasks)
            self._retry_unpackaged_failures(level_tasks)
            for task in level_tasks:
                self._record(result, task)
                repo_stale = repo_stale or task.status == BuildStatus.COMPLETE
        return repo_stale

    def _record(self, result: BuildResult, task: BuildTask) -> None:
        """Add a finished (or, with nowait, submitted) task to the result."""
        result.tasks.append(task)
        ok = task.status == BuildStatus.COMPLETE or (
            task.status == BuildStatus.BUILDING and self.nowait
        )
        if task.status == BuildStatus.ALREADY_BUILT:
            result.already_built.append(task.package_name)
        elif ok:
            result.built_packages.append(task.package_name)
        else:
            result.failed_packages.append(task.package_name)
            result.success = False

    def _can_retry_unpackaged(self, task: BuildTask) -> bool:
        """True if a failed task hit unpackaged files and may be retried with the macro."""
        if not task.task_id or not self._task_has_unpackaged_files_error(task.task_id):
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
        """Retry once every failed task of the level that hit unpackaged files.

        Updates the task in place with the retry's task_id, status and NVR, so the
        result reports the build that actually ran.
        """
        retried: list[BuildTask] = []
        for task in level_tasks:
            if task.status != BuildStatus.FAILED or not self._can_retry_unpackaged(task):
                continue
            logger.info("  retrying %s with _unpackaged_files_terminate_build 0", task.package_name)
            try:
                new_task = self._submit_build(task.srpm_path, add_unpackaged_macro=True)
            except Exception as exc:  # noqa: BLE001
                logger.error("  retry submit failed for %s: %s", task.package_name, exc)
                continue
            task.task_id = new_task.task_id
            task.nvr = new_task.nvr
            task.status = new_task.status
            task.error_message = None
            retried.append(task)
        self._poll_builds(retried)

    def _collect_toolchains(self, root_build_requires) -> list[str]:
        """Classify the BuildRequires of the target and every dependency by toolchain."""
        names = [br.name for br in root_build_requires]
        for node in self.resolver.dependency_graph.values():
            if not node.srpm_path:
                continue
            try:
                names.extend(br.name for br in get_build_requires(node.srpm_path))
            except Exception as exc:  # noqa: BLE001
                logger.debug("get_build_requires(%s) failed: %s", node.srpm_path, exc)
        return detect_toolchains(names)

    def _populate_tagged_builds(self, result: BuildResult, target_name: str) -> None:
        """Report which packages built in this run are now the latest in the destination tag.

        Already built packages are not listed: this run did not tag them. Neither is a
        target still building with nowait: the tag holds its previous build.
        """
        try:
            tagged = self.koji_client.list_tagged_builds(self.dest_tag)
        except Exception as exc:  # noqa: BLE001
            logger.warning("list-tagged for %s failed: %s", self.dest_tag, exc)
            return
        complete = {t.package_name for t in result.tasks if t.status == BuildStatus.COMPLETE}
        built = [pkg for pkg in result.built_packages if pkg in complete]
        if target_name in built and not self.scratch:
            result.tagged_target = tagged.get(target_name)
            if not result.tagged_target:
                logger.warning(
                    "target %s built, but not found in tag %s", target_name, self.dest_tag
                )
        for pkg in built:
            if pkg == target_name:
                continue
            nvr = tagged.get(pkg)
            if nvr:
                result.tagged_deps.append(nvr)
            else:
                logger.warning("dep %s built, but not found in tag %s", pkg, self.dest_tag)
