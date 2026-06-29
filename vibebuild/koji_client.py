"""
Koji hub access over XML-RPC.

`KojiClient` answers read-only questions anonymously (tags, builds, targets) and
caches them; `KojiSession` is the logged-in session that registers packages,
uploads SRPMs, submits builds and regenerates repos.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

import koji  # type: ignore[import-untyped]

from vibebuild.exceptions import KojiBuildError, KojiConnectionError

logger = logging.getLogger(__name__)

# Koji task states by number.
TASK_STATES = {0: "free", 1: "open", 2: "closed", 3: "canceled", 4: "assigned", 5: "failed"}
FINAL_TASK_STATES = ("closed", "failed", "canceled")


def poll_interval(elapsed_s: float, max_interval: int = 30) -> float:
    """
    Seconds to wait before the next poll of a task that has run for elapsed_s.

    Short tasks are noticed quickly, long ones cost fewer requests:
    2 s for the first 30 s, then 10 s up to 3 minutes, then max_interval.
    """
    if elapsed_s < 30:
        return min(2.0, max_interval)
    if elapsed_s < 180:
        return min(10.0, max_interval)
    return float(max_interval)


def task_state_name(state: int | None) -> str:
    """Name of a numeric Koji task state; "unknown" for None or an unknown number."""
    if state is None:
        return "unknown"
    return TASK_STATES.get(state, "unknown")


class KojiClient:
    """Read-only client for one Koji hub over XML-RPC (anonymous session).

    Answers are cached per client: within one vibebuild run the tags only change
    through our own builds, and callers that need fresh data call `clear_cache`.
    """

    def __init__(
        self,
        server: str,
        serverca: str | None = None,
        no_ssl_verify: bool = False,
    ):
        self.server = server
        self.serverca = serverca
        self.no_ssl_verify = no_ssl_verify
        self._session: koji.ClientSession | None = None
        self._latest: dict[tuple[str, str], dict[str, str] | None] = {}

    @property
    def session(self) -> koji.ClientSession:
        """Anonymous session, created on first use. koji retries connection errors itself."""
        if self._session is None:
            opts: dict = {"anon_retry": True, "max_retries": 3, "retry_interval": 2}
            if self.no_ssl_verify:
                opts["no_ssl_verify"] = True
            elif self.serverca:
                opts["serverca"] = self.serverca
            self._session = koji.ClientSession(self.server, opts=opts)
        return self._session

    def _call(self, method: str, *args, **kwargs):
        """Call a hub method; transport failures become KojiConnectionError."""
        try:
            return getattr(self.session, method)(*args, **kwargs)
        except koji.GenericError:
            raise
        except Exception as exc:
            raise KojiConnectionError(f"Koji call {method} failed: {exc}") from exc

    def clear_cache(self) -> None:
        """Forget cached builds (after tagging new ones)."""
        self._latest.clear()

    def list_packages(self, tag: str) -> list[str]:
        """Names of packages in the tag's package list (inherited, not blocked)."""
        packages = self._call("listPackages", tagID=tag, inherited=True, with_blocked=False)
        return [p["package_name"] for p in packages or []]

    def list_tagged_builds(self, tag: str) -> dict[str, str]:
        """Latest build of every package in the tag: {package_name: nvr}."""
        builds = self._call("listTagged", tag, latest=True)
        return {b["package_name"]: b["nvr"] for b in builds or []}

    @staticmethod
    def _build_info(builds) -> dict[str, str] | None:
        if not builds:
            return None
        b = builds[0]
        return {"nvr": b["nvr"], "version": b["version"], "release": b["release"]}

    def latest_build(self, package: str, tag: str) -> dict[str, str] | None:
        """
        Latest build of a package in the tag, inheritance included.

        Returns:
            {"nvr": "...", "version": "...", "release": "..."} or None if there is no build.
        """
        key = (package, tag)
        if key not in self._latest:
            try:
                builds = self._call("getLatestBuilds", tag, package=package)
            except koji.GenericError:  # unknown package name
                builds = []
            self._latest[key] = self._build_info(builds)
        return self._latest[key]

    def prefetch_latest_builds(self, packages: Sequence[str], tag: str) -> None:
        """Look up the latest builds of many packages in one multicall request."""
        names = sorted({p for p in packages if (p, tag) not in self._latest})
        if not names:
            return
        try:
            with self.session.multicall(strict=False) as m:
                calls = {name: m.getLatestBuilds(tag, package=name) for name in names}
        except Exception as exc:
            logger.debug("multicall getLatestBuilds failed, falling back to single calls: %s", exc)
            return
        for name, call in calls.items():
            try:
                self._latest[(name, tag)] = self._build_info(call.result)
            except koji.GenericError:  # unknown package name
                self._latest[(name, tag)] = None

    def package_exists(self, package: str, tag: str) -> bool:
        """Check if package has a build tagged in the given tag (including inherited)."""
        return self.latest_build(package, tag) is not None

    def get_target(self, target: str) -> dict[str, str] | None:
        """
        Return the build and destination tags of a build target.

        Returns:
            {"build_tag": "...", "dest_tag": "..."} or None if the target does not exist.
        """
        info = self._call("getBuildTarget", target)
        if not info:
            return None
        return {"build_tag": info["build_tag_name"], "dest_tag": info["dest_tag_name"]}

    def has_external_repos(self, tag: str) -> bool:
        """Check if a tag has external repos configured (inheritance included)."""
        return bool(self._call("getExternalRepoList", tag))


class KojiSession:
    """The logged-in hub session of one vibebuild run.

    koji-hub serializes calls per user, and two logins in a row as the same user
    hit AuthLockError, so a run logs in once and sends everything through here.
    Used as a context manager it logs in on entry and out on the outermost exit;
    without one it logs in on first use.
    """

    def __init__(
        self,
        server: str,
        cert: str | None = None,
        serverca: str | None = None,
        no_ssl_verify: bool = False,
    ):
        self.server = server
        self.cert = cert
        self.serverca = serverca
        self.no_ssl_verify = no_ssl_verify
        self._session: koji.ClientSession | None = None
        self._user: str | None = None
        self._depth = 0

    def __enter__(self) -> KojiSession:
        self._depth += 1
        return self

    def __exit__(self, *exc_info) -> None:
        self._depth -= 1
        if self._depth == 0:
            self.logout()

    @property
    def session(self) -> koji.ClientSession:
        """The authenticated ClientSession, logging in on first use."""
        if self._session is None:
            opts = {"no_ssl_verify": True} if self.no_ssl_verify else {}
            session = koji.ClientSession(self.server, opts=opts)
            session.ssl_login(self.cert, self.serverca, self.serverca)
            self._session = session
        return self._session

    def logout(self) -> None:
        """Log out, quietly; the next call logs in again."""
        if self._session is None:
            return
        try:
            self._session.logout()
        except Exception:  # noqa: BLE001
            pass
        self._session = None
        self._user = None

    @property
    def user(self) -> str:
        """Name of the logged-in user."""
        if self._user is None:
            self._user = self.session.getLoggedInUser()["name"]
        return self._user

    def submit(
        self, srpm: Path, package: str, target: str, dest_tag: str, scratch: bool = False
    ) -> int | None:
        """
        Add the package to dest_tag's package list, upload the SRPM and build it.

        Returns:
            The build task id, or None if Koji already has a completed build of this NVR.
        """
        session = self.session
        try:
            session.packageListAdd(dest_tag, package, owner=self.user)
        except koji.GenericError as exc:
            if "already" not in str(exc).lower():
                raise KojiBuildError(f"packageListAdd failed: {exc}")

        server_dir = f"cli-build/vibebuild-{uuid.uuid4().hex[:12]}"
        session.uploadWrapper(str(srpm), server_dir)
        opts = {"scratch": True} if scratch else {}
        try:
            return int(session.build(f"{server_dir}/{srpm.name}", target, opts))
        except koji.GenericError as exc:
            msg = str(exc)
            if "Build already exists" in msg and "state=COMPLETE" in msg:
                return None
            raise KojiBuildError(f"Build submission failed: {exc}")

    def task_state(self, task_id: int) -> str | None:
        """State name of a task, or None if the hub could not be asked."""
        try:
            info = self.session.getTaskInfo(task_id)
        except Exception as exc:  # noqa: BLE001
            logger.debug("getTaskInfo(%s) failed: %s", task_id, exc)
            return None
        return task_state_name(info.get("state") if info else None)

    def subtasks(self, task_id: int) -> list[dict]:
        """Child tasks of a task; empty if the hub could not be asked."""
        try:
            return self.session.listTasks(opts={"parent": task_id}) or []
        except Exception as exc:  # noqa: BLE001
            logger.debug("listTasks(parent=%s) failed: %s", task_id, exc)
            return []

    def task_log_tail(self, task_id: int, name: str, size: int) -> str | None:
        """
        The last `size` bytes of a task output file, or None if it is not there.

        The hub refuses negative offsets, so the offset is computed from the file size.
        """
        try:
            stats = self.session.listTaskOutput(task_id, stat=True) or {}
            if not isinstance(stats, dict) or name not in stats:
                return None
            offset = max(0, int(stats[name].get("st_size", 0)) - size)
            chunk = self.session.downloadTaskOutput(task_id, name, offset=offset, size=-1)
        except Exception as exc:  # noqa: BLE001
            logger.debug("reading %s of task %s failed: %s", name, task_id, exc)
            return None
        return chunk.decode("utf-8", errors="replace") if isinstance(chunk, bytes) else str(chunk)

    def wait_task(self, task_id: int, timeout: int = 1800) -> str:
        """Poll a task until it ends; returns its final state name or "timeout"."""
        start = time.time()
        while time.time() - start < timeout:
            state = self.task_state(task_id)
            if state in FINAL_TASK_STATES:
                return state
            time.sleep(poll_interval(time.time() - start))
        return "timeout"

    def repo_is_current(self, tag: str) -> bool:
        """True if the tag has a ready repo and nothing it inherits changed since."""
        session = self.session
        try:
            repo = session.getRepo(tag)
            if not repo:
                return False
            tag_ids = [session.getTag(tag, strict=True)["id"]]
            tag_ids += [link["parent_id"] for link in session.getFullInheritance(tag)]
            return not session.tagChangedSinceEvent(repo["create_event"], tag_ids)
        except Exception as exc:  # noqa: BLE001
            logger.debug("repo freshness check for %s failed: %s", tag, exc)
            return False

    def regenerate_repo(self, tag: str, timeout: int = 1800) -> bool:
        """Start newRepo for the tag and wait for it. True if the repo is ready."""
        try:
            task_id = self.session.newRepo(tag)
        except Exception as exc:  # noqa: BLE001
            logger.warning("newRepo %s failed to start: %s", tag, exc)
            return False
        logger.info("Regenerating repo for %s (task %s)", tag, task_id)
        state = self.wait_task(task_id, timeout=timeout)
        if state != "closed":
            logger.warning("newRepo %s ended in state %s", tag, state)
            return False
        logger.info("Repo for %s is ready", tag)
        return True
