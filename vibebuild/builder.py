"""
Koji builder - orchestrates package builds with dependency resolution.
"""

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from vibebuild.analyzer import get_build_requires, get_package_info_from_srpm
from vibebuild.exceptions import KojiBuildError, KojiConnectionError
from vibebuild.fetcher import SRPMFetcher
from vibebuild.name_resolver import PackageNameResolver
from vibebuild.resolver import DependencyResolver, KojiClient
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
    ):
        self.koji_server = koji_server
        self.koji_web_url = koji_web_url
        self.cert = cert
        self.serverca = serverca
        self.target = target
        self.build_tag = build_tag
        self.scratch = scratch
        self.nowait = nowait
        self.no_ssl_verify = no_ssl_verify
        self.force = force  # disable the idempotency skip
        self.idempotent = idempotent  # enable the idempotency pre-check (off by default)

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
        )

        self.fetcher = SRPMFetcher(
            download_dir=download_dir,
            fedora_release=fedora_release,
            no_ssl_verify=no_ssl_verify,
            name_resolver=self.name_resolver,
        )

        self._tasks: list[BuildTask] = []

    def _get_env(self) -> Optional[dict]:
        """Get environment variables for subprocess, with SSL verification disabled if needed."""
        if self.no_ssl_verify:
            env = os.environ.copy()
            env["PYTHONHTTPSVERIFY"] = "0"
            env["REQUESTS_CA_BUNDLE"] = ""
            env["CURL_CA_BUNDLE"] = ""
            return env
        return None

    def _run_koji(self, *args, timeout: int = 60) -> subprocess.CompletedProcess:
        """Run koji command with configured options."""
        cmd = ["koji", f"--server={self.koji_server}"]

        if self.cert:
            cmd.append(f"--cert={self.cert}")
        # Note: --serverca is not supported on RHEL9/older koji CLI.
        # The serverca is read from ~/.koji/config instead.

        cmd.extend(args)

        logger.debug(f"Running: {' '.join(cmd)}")

        try:
            return subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, env=self._get_env()
            )
        except subprocess.TimeoutExpired:
            raise KojiConnectionError(f"Command timed out: {' '.join(args)}")

    def _submit_build(self, srpm_path: str) -> BuildTask:
        """
        Submit a single package build to Koji (no waiting).

        Registers the package with add-pkg, submits with --nowait,
        and returns a BuildTask with status BUILDING.

        Args:
            srpm_path: Path to SRPM file

        Returns:
            BuildTask with task_id and status BUILDING
        """
        srpm_path = Path(srpm_path)
        if not srpm_path.exists():
            raise FileNotFoundError(f"SRPM not found: {srpm_path}")

        package_info = get_package_info_from_srpm(str(srpm_path))

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
        if (
            getattr(self, "idempotent", False)
            and not getattr(self, "force", False)
            and not self.scratch
        ):
            try:
                latest = self.koji_client.latest_build(package_info.name, self.target)
                if latest and latest.get("nvr") == package_info.nvr:
                    logger.info(
                        "  %s already built in %s (NVR=%s), skipping",
                        package_info.name,
                        self.target,
                        latest["nvr"],
                    )
                    task.status = BuildStatus.ALREADY_BUILT
                    return task
            except Exception as exc:  # noqa: BLE001
                logger.debug("idempotency check skipped: %s", exc)

        # Ensure the package is registered in the destination tag
        dest_tag = self.target  # e.g. "f42"
        add_result = self._run_koji(
            "add-pkg",
            dest_tag,
            package_info.name,
            "--owner=kojiadmin",
            timeout=120,
        )
        if add_result.returncode != 0:
            # Ignore "already exists" errors
            if "already exists" not in (add_result.stderr or ""):
                logger.warning(f"add-pkg failed (may already exist): {add_result.stderr}")

        # Always submit with --nowait
        cmd_args = ["build", "--nowait"]

        if self.scratch:
            cmd_args.append("--scratch")

        cmd_args.extend([self.target, str(srpm_path)])

        logger.info(f"Starting build: {package_info.nvr}")

        result = self._run_koji(*cmd_args, timeout=60)

        if result.returncode != 0:
            task.status = BuildStatus.FAILED
            task.error_message = result.stderr
            raise KojiBuildError(f"Build failed: {result.stderr}")

        for line in result.stdout.split("\n"):
            if "Created task:" in line:
                try:
                    task.task_id = int(line.split(":")[-1].strip())
                except ValueError:
                    pass
            elif "Task info:" in line:
                try:
                    task.task_id = int(line.split("=")[-1].strip())
                except ValueError:
                    pass

        logger.info(f"Build submitted: task_id={task.task_id}")
        task.status = BuildStatus.BUILDING

        return task

    def build_package(self, srpm_path: str, wait: bool = True) -> BuildTask:
        """
        Submit a single package build to Koji.

        Args:
            srpm_path: Path to SRPM file
            wait: Whether to wait for build to complete

        Returns:
            BuildTask with result information
        """
        task = self._submit_build(srpm_path)

        if wait and not self.nowait and task.task_id:
            task.status = self._poll_build(task.task_id, task.nvr or task.package_name)
        elif wait and not self.nowait:
            task.status = BuildStatus.COMPLETE

        return task

    def _poll_build(
        self, task_id: int, nvr: str, timeout: int = 7200, interval: int = 30
    ) -> BuildStatus:
        """Poll a build task with progress logging.

        `interval` is accepted as the maximum interval; the real interval is
        adaptive: short at the start (quick reaction to fast builds),
        growing over time (saves requests to koji on long builds).
        """
        start = time.time()
        last_state = ""

        while time.time() - start < timeout:
            result = self._run_koji("taskinfo", str(task_id), timeout=120)
            if result.returncode != 0:
                logger.warning(f"  [{nvr}] Could not get task info")
                time.sleep(_adaptive_poll_interval(time.time() - start, interval))
                continue

            output = result.stdout
            # Parse current state
            state = "unknown"
            for line in output.split("\n"):
                if line.startswith("State:"):
                    state = line.split(":", 1)[1].strip().lower()
                    break

            # Parse subtasks for more detail
            subtasks = []
            sub_result = self._run_koji("list-tasks", f"--parent={task_id}")
            if sub_result.returncode == 0:
                for line in sub_result.stdout.strip().split("\n"):
                    if line and not line.startswith("ID"):
                        parts = line.split()
                        if len(parts) >= 5:
                            sub_state = parts[3]
                            sub_name = " ".join(parts[5:])
                            subtasks.append(f"{sub_name} [{sub_state}]")

            elapsed = int(time.time() - start)
            minutes, seconds = divmod(elapsed, 60)

            if subtasks:
                current = ", ".join(subtasks[:3])
                progress = f"  [{nvr}] {minutes}m{seconds:02d}s — {current}"
            else:
                progress = f"  [{nvr}] {minutes}m{seconds:02d}s — {state}"

            if progress != last_state:
                logger.info(progress)
                last_state = progress

            if state in ("closed", "complete"):
                return BuildStatus.COMPLETE
            elif state == "failed":
                logger.error(f"  [{nvr}] Build FAILED (task {task_id})")
                return BuildStatus.FAILED
            elif state == "canceled":
                return BuildStatus.CANCELED

            time.sleep(_adaptive_poll_interval(elapsed, interval))

        logger.error(f"  [{nvr}] Build timed out after {timeout}s")
        return BuildStatus.FAILED

    def _poll_builds(self, tasks: list, timeout: int = 7200, interval: int = 30) -> None:
        """Poll multiple build tasks simultaneously until all complete or timeout.

        Args:
            tasks: List of BuildTask objects with task_id set
            timeout: Maximum time to wait in seconds
            interval: Seconds between polling sweeps
        """
        pending = {t.task_id: t for t in tasks if t.task_id}
        if not pending:
            return

        start = time.time()

        while pending and time.time() - start < timeout:
            completed_ids = []
            for task_id, task in pending.items():
                result = self._run_koji("taskinfo", str(task_id), timeout=120)
                if result.returncode != 0:
                    continue

                state = "unknown"
                for line in result.stdout.split("\n"):
                    if line.startswith("State:"):
                        state = line.split(":", 1)[1].strip().lower()
                        break

                if state in ("closed", "complete"):
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

        # Timeout remaining tasks
        for task in pending.values():
            task.status = BuildStatus.FAILED
            task.error_message = f"Build timed out after {timeout}s"
            logger.error(f"  [{task.package_name}] Build timed out after {timeout}s")

    def _ensure_repo_ready(self) -> None:
        """Ensure a repo exists for the build tag, creating one if needed."""
        result = self._run_koji("list-tasks", timeout=120)
        has_newrepo = result.returncode == 0 and "newRepo" in result.stdout

        if not has_newrepo:
            regen = self._run_koji("call", "newRepo", self.build_tag, timeout=120)
            if regen.returncode == 0:
                logger.info(f"Triggered newRepo for {self.build_tag}")

        logger.info(f"Waiting for repo to be ready: {self.build_tag}")
        wait_result = self._run_koji("wait-repo", self.build_tag, "--timeout=1800", timeout=1860)
        if wait_result.returncode == 0:
            logger.info("Repo is ready")
        else:
            logger.warning("wait-repo returned non-zero, proceeding anyway")

    def wait_for_repo(self, tag: Optional[str] = None, timeout: int = 1800) -> bool:
        """
        Wait for repository to be regenerated after build.

        Args:
            tag: Build tag to wait for (defaults to self.build_tag)
            timeout: Maximum time to wait in seconds

        Returns:
            True if repo was regenerated successfully
        """
        tag = tag or self.build_tag

        logger.info(f"Waiting for repo regeneration: {tag}")

        # Trigger newRepo explicitly — some Koji setups don't auto-create it
        regen_result = self._run_koji("call", "newRepo", tag, timeout=120)
        if regen_result.returncode == 0:
            logger.info(f"Triggered newRepo for {tag}")

        result = self._run_koji("wait-repo", tag, f"--timeout={timeout}", timeout=timeout + 60)

        if result.returncode != 0:
            logger.warning(f"wait-repo failed: {result.stderr}")
            return False

        logger.info("Repo regenerated successfully")
        return True

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

        # Ensure repo is ready before dependency resolution and builds
        self._ensure_repo_ready()

        package_info = get_package_info_from_srpm(str(srpm_path))
        logger.info(f"Package: {package_info.nvr}")

        logger.info("Analyzing dependencies...")

        def srpm_resolver(pkg_name: str) -> Optional[str]:
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

        if not build_chain:
            logger.info("No dependencies to build, proceeding with target package")
        else:
            total_deps = sum(len(level) for level in build_chain)
            logger.info(f"Found {total_deps} packages to build in {len(build_chain)} levels")

            for level_idx, level in enumerate(build_chain):
                logger.info(f"Building level {level_idx + 1}/{len(build_chain)}: {level}")

                # Submit all packages in this level in parallel
                level_tasks = []
                for pkg_name in level:
                    if pkg_name == package_info.name:
                        continue

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

                    level_built = 0
                    for task in level_tasks:
                        if task.status == BuildStatus.COMPLETE:
                            result.built_packages.append(task.package_name)
                            level_built += 1
                        else:
                            result.failed_packages.append(task.package_name)
                            result.success = False

                    # Wait for repo between levels (not after last level)
                    if level_built > 0 and level_idx < len(build_chain) - 1:
                        self.wait_for_repo()

        # Wait for repo once before target if any deps were built
        if result.built_packages:
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

    def _collect_toolchains(self, root_build_requires) -> list[str]:
        """Aggregate BR from root + every built dep, classify via detect_toolchains."""
        names: list[str] = [
            br.name if hasattr(br, "name") else str(br) for br in root_build_requires
        ]
        for node in self.resolver._dependency_graph.values():
            if not node.srpm_path:
                continue
            try:
                names.extend(get_build_requires(node.srpm_path))
            except Exception as exc:
                logger.debug("get_build_requires(%s) failed: %s", node.srpm_path, exc)
        return detect_toolchains(names)

    def _populate_tagged_builds(self, result: BuildResult, target_name: str) -> None:
        """Query koji list-tagged --latest <dest-tag> and split target vs deps."""
        try:
            tagged = self.koji_client.list_tagged_builds(self.target)
        except Exception as exc:
            logger.warning("list-tagged for %s failed: %s", self.target, exc)
            return
        result.tagged_target = tagged.get(target_name)
        if target_name in result.built_packages and not result.tagged_target:
            logger.warning("target %s built, but not found in tag %s", target_name, self.target)
        for pkg in result.built_packages:
            if pkg == target_name:
                continue
            nvr = tagged.get(pkg)
            if nvr:
                result.tagged_deps.append(nvr)
            else:
                logger.warning("dep %s built, but not found in tag %s", pkg, self.target)

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
        """Get current status of a build task."""
        result = self._run_koji("taskinfo", str(task_id))

        if result.returncode != 0:
            return BuildStatus.FAILED

        output = result.stdout.lower()

        if "closed" in output or "complete" in output:
            return BuildStatus.COMPLETE
        elif "failed" in output:
            return BuildStatus.FAILED
        elif "canceled" in output:
            return BuildStatus.CANCELED
        elif "open" in output or "free" in output or "assigned" in output:
            return BuildStatus.BUILDING

        return BuildStatus.PENDING

    def cancel_build(self, task_id: int) -> bool:
        """Cancel a running build task."""
        result = self._run_koji("cancel", str(task_id))
        return result.returncode == 0
