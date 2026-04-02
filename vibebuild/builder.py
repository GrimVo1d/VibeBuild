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

from vibebuild.analyzer import get_package_info_from_srpm
from vibebuild.exceptions import KojiBuildError, KojiConnectionError
from vibebuild.fetcher import SRPMFetcher
from vibebuild.name_resolver import PackageNameResolver
from vibebuild.resolver import DependencyResolver, KojiClient

logger = logging.getLogger(__name__)


class BuildStatus(Enum):
    """Status of a build task."""

    PENDING = "pending"
    BUILDING = "building"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELED = "canceled"


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

        # Ensure the package is registered in the destination tag
        dest_tag = self.target  # e.g. "f42"
        add_result = self._run_koji(
            "add-pkg", dest_tag, package_info.name, "--owner=kojiadmin",
            timeout=120,
        )
        if add_result.returncode != 0:
            # Ignore "already exists" errors
            if "already exists" not in (add_result.stderr or ""):
                logger.warning(
                    f"add-pkg failed (may already exist): {add_result.stderr}"
                )

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

    def _poll_build(self, task_id: int, nvr: str, timeout: int = 7200, interval: int = 30) -> BuildStatus:
        """Poll a build task with progress logging."""
        start = time.time()
        last_state = ""

        while time.time() - start < timeout:
            result = self._run_koji("taskinfo", str(task_id), timeout=120)
            if result.returncode != 0:
                logger.warning(f"  [{nvr}] Could not get task info")
                time.sleep(interval)
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

            time.sleep(interval)

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
                elapsed = int(time.time() - start)
                minutes, seconds = divmod(elapsed, 60)
                names = ", ".join(t.package_name for t in pending.values())
                logger.info(f"  [{minutes}m{seconds:02d}s] Waiting for: {names}")
                time.sleep(interval)

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
        wait_result = self._run_koji(
            "wait-repo", self.build_tag, "--timeout=1800", timeout=1860
        )
        if wait_result.returncode == 0:
            logger.info("Repo is ready")
        else:
            logger.warning(f"wait-repo returned non-zero, proceeding anyway")

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
