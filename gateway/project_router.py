"""Profile-scoped SQLite routing, idempotency, and workspace leases."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping


class ProjectRouterError(Exception):
    """Base exception for project routing failures."""


class UnknownBindingError(ProjectRouterError):
    """No project is bound to the requested topic."""


class AccessDeniedError(ProjectRouterError):
    """The sender is explicitly denied access."""


class UnknownUserError(ProjectRouterError):
    """The sender has no ACL entry and access therefore fails closed."""


class BindingConflictError(ProjectRouterError):
    """A topic is already bound to a different project."""


class LeaseNotOwnedError(ProjectRouterError):
    """A lease operation was attempted by a non-owner."""


@dataclass(frozen=True)
class ProjectContext:
    project_id: str
    slug: str
    board_slug: str
    workdir: Path | None
    status: str
    platform: str
    chat_id: str
    thread_id: str
    sender_user_id: str
    is_management: bool
    access: str = "allow"


@dataclass(frozen=True)
class EventClaim:
    claimed: bool
    result_ref: str | None


@dataclass(frozen=True)
class LeaseResult:
    acquired: bool
    owner_id: str
    run_id: str
    expires_at: int


@dataclass(frozen=True)
class ProvisionedProject:
    project_id: str
    slug: str
    board_slug: str
    workdir: Path | None
    platform: str
    chat_id: str
    thread_id: str
    is_management: bool


_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def normalize_project_slug(name: object) -> str:
    """Return a stable ASCII project slug derived from a display name."""
    raw = _required(name, "name")
    decomposed = unicodedata.normalize("NFKD", raw)
    ascii_name = "".join(
        char for char in decomposed
        if not unicodedata.combining(char) and ord(char) < 128
    ).lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:64].rstrip("-")
    if not normalized:
        raise ValueError("name does not contain any slug characters")
    return normalized


def _required(value: object, name: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise ValueError(f"{name} must be non-empty")
    return normalized


def _id(value: object, name: str) -> str:
    return _required(value, name)


def _slug(value: object, name: str) -> str:
    normalized = _required(value, name)
    if (
        len(normalized) > 64
        or normalized in {".", ".."}
        or not _SLUG_RE.fullmatch(normalized)
    ):
        raise ValueError(f"{name} is not a valid slug")
    return normalized


def _workdir(value: Path | str) -> str:
    raw = _required(value, "workdir")
    return str(Path(raw).expanduser().resolve(strict=False))


def _resolve_or_create_workspace(root: Path | str, slug: str) -> tuple[Path, bool]:
    """Resolve one direct normalized child or create ``root/slug`` deterministically."""
    workspace_root = Path(_workdir(root))
    workspace_root.mkdir(parents=True, exist_ok=True)
    if not workspace_root.is_dir():
        raise BindingConflictError("workspace_root is not a directory")

    matches: list[Path] = []
    for child in workspace_root.iterdir():
        try:
            same_slug = normalize_project_slug(child.name) == slug
        except ValueError:
            same_slug = False
        if not same_slug:
            continue
        if child.is_symlink() or not child.is_dir():
            raise BindingConflictError("workspace candidate is not a safe directory")
        matches.append(child.resolve())

    if len(matches) > 1:
        raise BindingConflictError("multiple workspace directories match the project slug")
    if matches:
        return matches[0], False

    target = workspace_root / slug
    try:
        target.mkdir()
        return target.resolve(), True
    except FileExistsError:
        if target.is_symlink() or not target.is_dir():
            raise BindingConflictError("canonical workspace path is not a safe directory")
        return target.resolve(), False


class ProjectRouter:
    """Small SQLite-backed router scoped to one profile."""

    def __init__(
        self,
        db_path: Path,
        profile: str,
        *,
        now: Callable[[], float] | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self.db_path = Path(db_path)
        self.profile = _required(profile, "profile")
        self._now = now or time.time
        self._lock = threading.RLock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(
            str(self.db_path),
            timeout=max(busy_timeout_ms, 0) / 1_000,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute(f"PRAGMA busy_timeout = {max(busy_timeout_ms, 0)}")
        try:
            self._connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        self._migrate()

    def _migrate(self) -> None:
        schema = """
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS projects (
            profile TEXT NOT NULL,
            project_id TEXT NOT NULL,
            slug TEXT NOT NULL,
            board_slug TEXT NOT NULL,
            workdir TEXT,
            status TEXT NOT NULL,
            PRIMARY KEY (profile, project_id),
            UNIQUE (profile, slug)
        );
        CREATE TABLE IF NOT EXISTS topic_bindings (
            profile TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            is_management INTEGER NOT NULL DEFAULT 0 CHECK (is_management IN (0, 1)),
            PRIMARY KEY (profile, platform, chat_id, thread_id),
            FOREIGN KEY (profile, project_id)
                REFERENCES projects(profile, project_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS acl_entries (
            profile TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
            PRIMARY KEY (profile, chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS processed_events (
            profile TEXT NOT NULL,
            platform TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            message_id TEXT NOT NULL,
            operation TEXT NOT NULL,
            result_ref TEXT,
            claimed_at INTEGER NOT NULL,
            PRIMARY KEY (profile, platform, chat_id, message_id, operation)
        );
        CREATE TABLE IF NOT EXISTS workspace_leases (
            workdir TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            acquired_at INTEGER NOT NULL,
            heartbeat_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        COMMIT;
        """
        with self._lock:
            try:
                self._connection.executescript(schema)
                workdir_column = next(
                    (
                        row
                        for row in self._connection.execute("PRAGMA table_info(projects)")
                        if row["name"] == "workdir"
                    ),
                    None,
                )
                if workdir_column is not None and bool(workdir_column["notnull"]):
                    self._rebuild_projects_for_nullable_workdir()
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _rebuild_projects_for_nullable_workdir(self) -> None:
        """Transactionally replace the legacy NOT NULL projects table."""
        self._connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                """
                CREATE TABLE projects_nullable (
                    profile TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    board_slug TEXT NOT NULL,
                    workdir TEXT,
                    status TEXT NOT NULL,
                    PRIMARY KEY (profile, project_id),
                    UNIQUE (profile, slug)
                )
                """
            )
            self._connection.execute(
                """INSERT INTO projects_nullable(
                       profile, project_id, slug, board_slug, workdir, status
                   )
                   SELECT profile, project_id, slug, board_slug, workdir, status
                   FROM projects"""
            )
            self._connection.execute("DROP TABLE projects")
            self._connection.execute("ALTER TABLE projects_nullable RENAME TO projects")
            self._connection.execute("COMMIT")
        except BaseException:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise
        finally:
            self._connection.execute("PRAGMA foreign_keys = ON")

    def _transaction(self, callback):
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                result = callback(self._connection)
                self._connection.execute("COMMIT")
                return result
            except BaseException:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def upsert_project(
        self,
        project_id: object,
        slug: object,
        board_slug: object,
        workdir: Path | str | None,
        status: object = "active",
    ) -> None:
        values = (
            self.profile,
            _id(project_id, "project_id"),
            _slug(slug, "slug"),
            _slug(board_slug, "board_slug"),
            _workdir(workdir) if workdir is not None and str(workdir).strip() else None,
            _required(status, "status"),
        )
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO projects(profile, project_id, slug, board_slug, workdir, status)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile, project_id) DO UPDATE SET
                    slug=excluded.slug,
                    board_slug=excluded.board_slug,
                    workdir=excluded.workdir,
                    status=excluded.status
                """,
                values,
            )

    def provision_topic_project(
        self,
        project_name: object,
        topic_name: object,
        platform: object,
        chat_id: object,
        thread_id: object,
        *,
        slug: object | None = None,
        board_slug: object | None = None,
        workdir: Path | str | None = None,
        workspace_root: Path | str | None = None,
        status: object = "active",
        is_management: bool = False,
        sender_user_id: object | None = None,
        allowed_users: Mapping[object, object] | None = None,
        board_creator: Callable[..., object] | None = None,
    ) -> ProvisionedProject:
        """Atomically provision a dynamic Topic project after fail-closed ACL validation.

        ``sender_user_id`` is required for runtime-created projects. Bootstrap callers may
        omit it while atomically seeding ``allowed_users``. Project identity and Topic
        binding commit together; board creation is idempotent and happens after that
        authoritative transaction so a later request can repair a missing board.
        """
        display_name = _required(project_name, "project_name")
        _required(topic_name, "topic_name")
        base_slug = _slug(
            slug if slug is not None else normalize_project_slug(display_name),
            "slug",
        )
        requested_board = _slug(
            board_slug if board_slug is not None else base_slug,
            "board_slug",
        )
        platform_s = _id(platform, "platform")
        chat_s = _id(chat_id, "chat_id")
        thread_s = _id(thread_id, "thread_id")
        sender_s = (
            _id(sender_user_id, "sender_user_id")
            if sender_user_id is not None
            else None
        )
        canonical_workdir = (
            Path(_workdir(workdir))
            if workdir is not None and str(workdir).strip()
            else None
        )
        canonical_workspace_root = (
            Path(_workdir(workspace_root))
            if workspace_root is not None and str(workspace_root).strip()
            else None
        )
        status_s = _required(status, "status")
        key = (self.profile, platform_s, chat_s, thread_s)
        created_workspace: Path | None = None

        creator = board_creator
        if creator is None and not is_management:
            from hermes_cli.kanban_db import create_board

            creator = create_board

        def as_result(row: sqlite3.Row) -> ProvisionedProject:
            return ProvisionedProject(
                project_id=row["project_id"],
                slug=row["slug"],
                board_slug=row["board_slug"],
                workdir=Path(row["workdir"]) if row["workdir"] else None,
                platform=platform_s,
                chat_id=chat_s,
                thread_id=thread_s,
                is_management=bool(row["is_management"]),
            )

        def operation(connection: sqlite3.Connection) -> ProvisionedProject:
            nonlocal created_workspace
            for user_id, effect in (allowed_users or {}).items():
                normalized_effect = _required(effect, "effect").lower()
                if normalized_effect not in {"allow", "deny"}:
                    raise ValueError("effect must be 'allow' or 'deny'")
                connection.execute(
                    """INSERT INTO acl_entries(profile, chat_id, user_id, effect)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(profile, chat_id, user_id)
                       DO UPDATE SET effect=excluded.effect""",
                    (self.profile, chat_s, _id(user_id, "user_id"), normalized_effect),
                )

            if sender_s is not None:
                acl = connection.execute(
                    """SELECT effect FROM acl_entries
                       WHERE profile=? AND chat_id=? AND user_id=?""",
                    (self.profile, chat_s, sender_s),
                ).fetchone()
                if acl is None:
                    raise UnknownUserError("sender has no ACL entry")
                if acl["effect"] == "deny":
                    raise AccessDeniedError("sender is denied by ACL")

            existing = connection.execute(
                """SELECT p.project_id, p.slug, p.board_slug, p.workdir,
                          b.is_management
                   FROM topic_bindings AS b
                   JOIN projects AS p
                     ON p.profile=b.profile AND p.project_id=b.project_id
                   WHERE b.profile=? AND b.platform=? AND b.chat_id=? AND b.thread_id=?""",
                key,
            ).fetchone()
            if existing is not None:
                result = as_result(existing)
                if (
                    result.workdir is None
                    and not result.is_management
                    and canonical_workspace_root is not None
                ):
                    resolved_workdir, was_created = _resolve_or_create_workspace(
                        canonical_workspace_root, result.slug
                    )
                    if was_created:
                        created_workspace = resolved_workdir
                    connection.execute(
                        """UPDATE projects SET workdir=?
                           WHERE profile=? AND project_id=? AND workdir IS NULL""",
                        (str(resolved_workdir), self.profile, result.project_id),
                    )
                    result = replace(result, workdir=resolved_workdir)
                    assert creator is not None
                    creator(
                        result.board_slug,
                        name=display_name,
                        default_workdir=str(resolved_workdir),
                    )
                return result

            resolved_slug = base_slug
            resolved_board = requested_board
            resolved_project_id = resolved_slug
            project = connection.execute(
                """SELECT project_id, slug, board_slug, workdir, status
                   FROM projects WHERE profile=? AND slug=?""",
                (self.profile, resolved_slug),
            ).fetchone()
            if project is not None:
                bound_elsewhere = connection.execute(
                    """SELECT 1 FROM topic_bindings
                       WHERE profile=? AND project_id=? LIMIT 1""",
                    (self.profile, project["project_id"]),
                ).fetchone()
                if bound_elsewhere is None:
                    resolved_project_id = project["project_id"]
                    resolved_slug = project["slug"]
                    resolved_board = project["board_slug"]
                    canonical_project_workdir = (
                        Path(project["workdir"]) if project["workdir"] else None
                    )
                else:
                    suffix = hashlib.sha256(
                        f"{platform_s}:{chat_s}:{thread_s}".encode("utf-8")
                    ).hexdigest()[:10]
                    resolved_slug = _slug(
                        f"{base_slug[:53].rstrip('-')}-{suffix}", "slug"
                    )
                    resolved_board = resolved_slug
                    resolved_project_id = resolved_slug
                    collision = connection.execute(
                        """SELECT project_id FROM projects
                           WHERE profile=? AND slug=?""",
                        (self.profile, resolved_slug),
                    ).fetchone()
                    if collision is not None:
                        raise BindingConflictError(
                            "stable Topic slug is already owned by another project"
                        )
                    canonical_project_workdir = canonical_workdir
            else:
                canonical_project_workdir = canonical_workdir

            if (
                canonical_project_workdir is None
                and not is_management
                and canonical_workspace_root is not None
            ):
                canonical_project_workdir, was_created = _resolve_or_create_workspace(
                    canonical_workspace_root, resolved_slug
                )
                if was_created:
                    created_workspace = canonical_project_workdir

            connection.execute(
                """INSERT INTO projects(
                       profile, project_id, slug, board_slug, workdir, status
                   ) VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(profile, project_id) DO NOTHING""",
                (
                    self.profile,
                    resolved_project_id,
                    resolved_slug,
                    resolved_board,
                    str(canonical_project_workdir)
                    if canonical_project_workdir is not None
                    else None,
                    status_s,
                ),
            )
            connection.execute(
                """INSERT INTO topic_bindings(
                       profile, platform, chat_id, thread_id, project_id, is_management
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                (*key, resolved_project_id, int(is_management)),
            )
            created = connection.execute(
                """SELECT p.project_id, p.slug, p.board_slug, p.workdir,
                          b.is_management
                   FROM topic_bindings AS b
                   JOIN projects AS p
                     ON p.profile=b.profile AND p.project_id=b.project_id
                   WHERE b.profile=? AND b.platform=? AND b.chat_id=? AND b.thread_id=?""",
                key,
            ).fetchone()
            provisioned = as_result(created)
            if not provisioned.is_management:
                assert creator is not None
                creator(
                    provisioned.board_slug,
                    name=display_name,
                    default_workdir=(
                        str(provisioned.workdir) if provisioned.workdir else None
                    ),
                )
            return provisioned

        try:
            return self._transaction(operation)
        except BaseException:
            if created_workspace is not None:
                try:
                    created_workspace.rmdir()
                except OSError:
                    pass
            raise

    def ensure_bound_workspace(
        self,
        project_context: ProjectContext,
        workspace_root: Path | str,
        *,
        display_name: str | None = None,
        board_creator: Callable[..., object] | None = None,
    ) -> ProjectContext:
        """Repair a bound project's missing workdir without user intervention."""
        if project_context.is_management or project_context.workdir is not None:
            return project_context

        creator = board_creator
        if creator is None:
            from hermes_cli.kanban_db import create_board

            creator = create_board
        created_workspace: Path | None = None

        def operation(connection: sqlite3.Connection) -> ProjectContext:
            nonlocal created_workspace
            row = connection.execute(
                """SELECT p.workdir FROM topic_bindings AS b
                   JOIN projects AS p
                     ON p.profile=b.profile AND p.project_id=b.project_id
                   WHERE b.profile=? AND b.platform=? AND b.chat_id=?
                     AND b.thread_id=? AND b.project_id=?""",
                (
                    self.profile,
                    project_context.platform,
                    project_context.chat_id,
                    project_context.thread_id,
                    project_context.project_id,
                ),
            ).fetchone()
            if row is None:
                raise UnknownBindingError("project binding no longer exists")
            if row["workdir"]:
                return replace(project_context, workdir=Path(row["workdir"]))

            resolved_workdir, was_created = _resolve_or_create_workspace(
                workspace_root, project_context.slug
            )
            if was_created:
                created_workspace = resolved_workdir
            connection.execute(
                """UPDATE projects SET workdir=?
                   WHERE profile=? AND project_id=? AND workdir IS NULL""",
                (str(resolved_workdir), self.profile, project_context.project_id),
            )
            creator(
                project_context.board_slug,
                name=display_name or project_context.slug,
                default_workdir=str(resolved_workdir),
            )
            return replace(project_context, workdir=resolved_workdir)

        try:
            return self._transaction(operation)
        except BaseException:
            if created_workspace is not None:
                try:
                    created_workspace.rmdir()
                except OSError:
                    pass
            raise
    def ensure_bound_board(
        self,
        project_context: ProjectContext,
        *,
        board_creator: Callable[..., object] | None = None,
    ) -> None:
        """Ensure the board named by an already-authoritative binding exists."""
        creator = board_creator
        if creator is None:
            from hermes_cli.kanban_db import create_board

            creator = create_board
        creator(
            _slug(project_context.board_slug, "board_slug"),
            name=project_context.slug,
            default_workdir=str(project_context.workdir) if project_context.workdir else None,
        )

    def bind_topic(
        self,
        platform: object,
        chat_id: object,
        thread_id: object,
        project_id: object,
        *,
        is_management: bool = False,
        replace: bool = False,
    ) -> None:
        key = (
            self.profile,
            _id(platform, "platform"),
            _id(chat_id, "chat_id"),
            _id(thread_id, "thread_id"),
        )
        target = _id(project_id, "project_id")

        def operation(connection: sqlite3.Connection) -> None:
            existing = connection.execute(
                """SELECT project_id FROM topic_bindings
                   WHERE profile=? AND platform=? AND chat_id=? AND thread_id=?""",
                key,
            ).fetchone()
            if existing is not None and existing["project_id"] != target and not replace:
                raise BindingConflictError("topic is already bound to another project")
            connection.execute(
                """
                INSERT INTO topic_bindings(
                    profile, platform, chat_id, thread_id, project_id, is_management
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile, platform, chat_id, thread_id) DO UPDATE SET
                    project_id=excluded.project_id,
                    is_management=excluded.is_management
                """,
                (*key, target, int(is_management)),
            )

        self._transaction(operation)

    def bind_existing_topic(
        self,
        platform: object,
        chat_id: object,
        thread_id: object,
        project_slug: object,
        sender_user_id: object,
        *,
        is_management: bool = False,
    ) -> ProjectContext:
        """Bind an unbound topic to an existing active project after ACL validation."""
        platform_s = _id(platform, "platform")
        chat_s = _id(chat_id, "chat_id")
        thread_s = _id(thread_id, "thread_id")
        user_s = _id(sender_user_id, "sender_user_id")
        slug_s = _slug(project_slug, "project_slug")

        def operation(connection: sqlite3.Connection) -> None:
            acl = connection.execute(
                """SELECT effect FROM acl_entries
                   WHERE profile=? AND chat_id=? AND user_id=?""",
                (self.profile, chat_s, user_s),
            ).fetchone()
            if acl is None:
                raise UnknownUserError("sender has no ACL entry")
            if acl["effect"] == "deny":
                raise AccessDeniedError("sender is denied by ACL")

            project = connection.execute(
                """SELECT project_id FROM projects
                   WHERE profile=? AND slug=? AND status='active'""",
                (self.profile, slug_s),
            ).fetchone()
            if project is None:
                raise UnknownBindingError("no active existing project matches the topic marker")

            key = (self.profile, platform_s, chat_s, thread_s)
            existing = connection.execute(
                """SELECT project_id FROM topic_bindings
                   WHERE profile=? AND platform=? AND chat_id=? AND thread_id=?""",
                key,
            ).fetchone()
            if existing is not None and existing["project_id"] != project["project_id"]:
                raise BindingConflictError("topic is already bound to another project")
            connection.execute(
                """
                INSERT INTO topic_bindings(
                    profile, platform, chat_id, thread_id, project_id, is_management
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile, platform, chat_id, thread_id) DO UPDATE SET
                    project_id=excluded.project_id,
                    is_management=excluded.is_management
                """,
                (*key, project["project_id"], int(is_management)),
            )

        self._transaction(operation)
        return self.resolve(platform_s, chat_s, thread_s, user_s)

    def set_acl(self, chat_id: object, user_id: object, effect: str) -> None:
        normalized_effect = _required(effect, "effect").lower()
        if normalized_effect not in {"allow", "deny"}:
            raise ValueError("effect must be 'allow' or 'deny'")
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO acl_entries(profile, chat_id, user_id, effect)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(profile, chat_id, user_id) DO UPDATE SET effect=excluded.effect
                """,
                (
                    self.profile,
                    _id(chat_id, "chat_id"),
                    _id(user_id, "user_id"),
                    normalized_effect,
                ),
            )

    def resolve(
        self,
        platform: object,
        chat_id: object,
        thread_id: object,
        sender_user_id: object,
    ) -> ProjectContext:
        platform_s = _id(platform, "platform")
        chat_s = _id(chat_id, "chat_id")
        thread_s = _id(thread_id, "thread_id")
        user_s = _id(sender_user_id, "sender_user_id")
        with self._lock:
            row = self._connection.execute(
                """
                SELECT p.project_id, p.slug, p.board_slug, p.workdir, p.status,
                       b.is_management
                FROM topic_bindings AS b
                JOIN projects AS p
                  ON p.profile=b.profile AND p.project_id=b.project_id
                WHERE b.profile=? AND b.platform=? AND b.chat_id=? AND b.thread_id=?
                """,
                (self.profile, platform_s, chat_s, thread_s),
            ).fetchone()
            if row is None:
                raise UnknownBindingError("no project binding exists for this topic")
            acl = self._connection.execute(
                """SELECT effect FROM acl_entries
                   WHERE profile=? AND chat_id=? AND user_id=?""",
                (self.profile, chat_s, user_s),
            ).fetchone()
        if acl is None:
            raise UnknownUserError("sender has no ACL entry")
        if acl["effect"] == "deny":
            raise AccessDeniedError("sender is denied by ACL")
        return ProjectContext(
            project_id=row["project_id"],
            slug=row["slug"],
            board_slug=row["board_slug"],
            workdir=Path(row["workdir"]) if row["workdir"] else None,
            status=row["status"],
            platform=platform_s,
            chat_id=chat_s,
            thread_id=thread_s,
            sender_user_id=user_s,
            is_management=bool(row["is_management"]),
        )

    def find_telegram_binding(
        self,
        chat_id: object,
        project: object,
    ) -> ProvisionedProject | None:
        """Find this profile/chat's Telegram binding by project id or slug."""
        chat_s = _id(chat_id, "chat_id")
        project_s = _id(project, "project")

        def find(connection: sqlite3.Connection) -> ProvisionedProject | None:
            row = connection.execute(
                """
                SELECT p.project_id, p.slug, p.board_slug, p.workdir,
                       b.thread_id, b.is_management
                FROM topic_bindings AS b
                JOIN projects AS p
                  ON p.profile=b.profile AND p.project_id=b.project_id
                WHERE b.profile=? AND b.platform='telegram' AND b.chat_id=?
                  AND (p.project_id=? OR p.slug=?)
                ORDER BY b.thread_id
                LIMIT 1
                """,
                (self.profile, chat_s, project_s, project_s),
            ).fetchone()
            if row is None:
                return None
            return ProvisionedProject(
                project_id=row["project_id"],
                slug=row["slug"],
                board_slug=row["board_slug"],
                workdir=Path(row["workdir"]) if row["workdir"] else None,
                platform="telegram",
                chat_id=chat_s,
                thread_id=row["thread_id"],
                is_management=bool(row["is_management"]),
            )

        return self._transaction(find)

    def claim_event(
        self,
        platform: object,
        chat_id: object,
        message_id: object,
        operation: object,
    ) -> EventClaim:
        key = (
            self.profile,
            _id(platform, "platform"),
            _id(chat_id, "chat_id"),
            _id(message_id, "message_id"),
            _id(operation, "operation"),
        )

        def claim(connection: sqlite3.Connection) -> EventClaim:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO processed_events(
                    profile, platform, chat_id, message_id, operation, claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (*key, int(self._now())),
            )
            if cursor.rowcount == 1:
                return EventClaim(True, None)
            row = connection.execute(
                """SELECT result_ref FROM processed_events
                   WHERE profile=? AND platform=? AND chat_id=?
                     AND message_id=? AND operation=?""",
                key,
            ).fetchone()
            return EventClaim(False, row["result_ref"])

        return self._transaction(claim)

    def finalize_event(
        self,
        platform: object,
        chat_id: object,
        message_id: object,
        operation: object,
        result_ref: object,
    ) -> bool:
        result = _required(result_ref, "result_ref")
        with self._lock:
            cursor = self._connection.execute(
                """
                UPDATE processed_events SET result_ref=?
                WHERE profile=? AND platform=? AND chat_id=?
                  AND message_id=? AND operation=?
                  AND (result_ref IS NULL OR result_ref=?)
                """,
                (
                    result,
                    self.profile,
                    _id(platform, "platform"),
                    _id(chat_id, "chat_id"),
                    _id(message_id, "message_id"),
                    _id(operation, "operation"),
                    result,
                ),
            )
            return cursor.rowcount == 1

    def abandon_event(
        self,
        platform: object,
        chat_id: object,
        message_id: object,
        operation: object,
    ) -> bool:
        """Release exactly one still-unfinalized event claim owned by this key."""
        key = (
            self.profile,
            _id(platform, "platform"),
            _id(chat_id, "chat_id"),
            _id(message_id, "message_id"),
            _id(operation, "operation"),
        )

        def abandon(connection: sqlite3.Connection) -> bool:
            cursor = connection.execute(
                """
                DELETE FROM processed_events
                WHERE profile=? AND platform=? AND chat_id=?
                  AND message_id=? AND operation=? AND result_ref IS NULL
                """,
                key,
            )
            return cursor.rowcount == 1

        return self._transaction(abandon)

    def acquire_lease(
        self,
        workdir: Path | str,
        owner_id: object,
        run_id: object,
        ttl_seconds: int,
    ) -> LeaseResult:
        canonical = _workdir(workdir)
        owner = _id(owner_id, "owner_id")
        run = _id(run_id, "run_id")
        ttl = int(ttl_seconds)
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = int(self._now())
        expires = now + ttl

        def acquire(connection: sqlite3.Connection) -> LeaseResult:
            row = connection.execute(
                "SELECT * FROM workspace_leases WHERE workdir=?", (canonical,)
            ).fetchone()
            if row is None or row["expires_at"] <= now:
                connection.execute(
                    """
                    INSERT INTO workspace_leases(
                        workdir, profile, owner_id, run_id,
                        acquired_at, heartbeat_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workdir) DO UPDATE SET
                        profile=excluded.profile,
                        owner_id=excluded.owner_id,
                        run_id=excluded.run_id,
                        acquired_at=excluded.acquired_at,
                        heartbeat_at=excluded.heartbeat_at,
                        expires_at=excluded.expires_at
                    """,
                    (canonical, self.profile, owner, run, now, now, expires),
                )
                return LeaseResult(True, owner, run, expires)
            if (
                row["profile"] == self.profile
                and row["owner_id"] == owner
                and row["run_id"] == run
            ):
                connection.execute(
                    """UPDATE workspace_leases SET heartbeat_at=?, expires_at=?
                       WHERE workdir=?""",
                    (now, expires, canonical),
                )
                return LeaseResult(True, owner, run, expires)
            return LeaseResult(False, row["owner_id"], row["run_id"], row["expires_at"])

        return self._transaction(acquire)

    def renew_lease(
        self,
        workdir: Path | str,
        owner_id: object,
        run_id: object,
        ttl_seconds: int,
    ) -> LeaseResult:
        canonical = _workdir(workdir)
        owner = _id(owner_id, "owner_id")
        run = _id(run_id, "run_id")
        ttl = int(ttl_seconds)
        if ttl <= 0:
            raise ValueError("ttl_seconds must be positive")
        now = int(self._now())
        expires = now + ttl

        def renew(connection: sqlite3.Connection) -> LeaseResult:
            row = connection.execute(
                "SELECT * FROM workspace_leases WHERE workdir=?", (canonical,)
            ).fetchone()
            if (
                row is None
                or row["expires_at"] <= now
                or row["profile"] != self.profile
                or row["owner_id"] != owner
                or row["run_id"] != run
            ):
                raise LeaseNotOwnedError("cannot renew a lease not owned by this run")
            connection.execute(
                """UPDATE workspace_leases SET heartbeat_at=?, expires_at=?
                   WHERE workdir=?""",
                (now, expires, canonical),
            )
            return LeaseResult(True, owner, run, expires)

        return self._transaction(renew)

    def release_lease(
        self, workdir: Path | str, owner_id: object, run_id: object
    ) -> bool:
        canonical = _workdir(workdir)
        owner = _id(owner_id, "owner_id")
        run = _id(run_id, "run_id")

        def release(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT profile, owner_id, run_id FROM workspace_leases WHERE workdir=?",
                (canonical,),
            ).fetchone()
            if row is None:
                return False
            if (
                row["profile"] != self.profile
                or row["owner_id"] != owner
                or row["run_id"] != run
            ):
                raise LeaseNotOwnedError("cannot release a lease not owned by this run")
            connection.execute("DELETE FROM workspace_leases WHERE workdir=?", (canonical,))
            return True

        return self._transaction(release)

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "ProjectRouter":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


__all__ = [
    "AccessDeniedError",
    "BindingConflictError",
    "EventClaim",
    "LeaseNotOwnedError",
    "LeaseResult",
    "ProjectContext",
    "ProvisionedProject",
    "ProjectRouter",
    "ProjectRouterError",
    "UnknownBindingError",
    "UnknownUserError",
    "normalize_project_slug",
]
