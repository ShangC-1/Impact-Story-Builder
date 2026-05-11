from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import sys
import textwrap
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional in local SQLite-only mode
    psycopg = None
    dict_row = None


def get_bundle_root() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def get_runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT_DIR = get_bundle_root()
RUNTIME_ROOT = get_runtime_root()
SCHEMA_PATH = ROOT_DIR / "schema" / "impactStorySchema.json"
SERVER_LOG_PATH = RUNTIME_ROOT / "impact-story-builder-server.log"
DEFAULT_DATABASE_PATH = RUNTIME_ROOT / "impact_story_builder_demo.sqlite3"
VALID_DRAFT_STATUSES = {"draft", "ready_for_review", "final"}
VALID_VISIBILITIES = {"private", "shared"}
STORY_LENGTH_MIN = 100
STORY_LENGTH_MAX = 750
STORY_LENGTH_WINDOW = 50
STORY_LENGTH_START_MAX = STORY_LENGTH_MAX - STORY_LENGTH_WINDOW
STORY_LENGTH_DEFAULT_MIN = 300
STORY_LENGTH_DEFAULT_MAX = 350
STORY_LENGTH_MIN_INTERVAL = 25
STORY_TONE_OPTIONS = {
    "professional": {
        "label": "Professional",
        "description": (
            "Clear, polished, evidence-based, and suitable for SEI internal reports, program communications, "
            "and general policy or funder audiences. It can be moderately formal, but should not sound bureaucratic."
        ),
    },
    "conversational": {
        "label": "Conversational",
        "description": (
            "Accessible, plain-language, and easier to read. It can use warmer transitions, "
            "but should still sound credible and evidence-based."
        ),
    },
    "funder_facing": {
        "label": "Funder-facing",
        "description": (
            "Outcome-oriented and evidence-forward, suitable for donor updates, proposals, and impact reporting. "
            "Emphasize contribution, uptake, durability, scale, and future potential without overstating causality."
        ),
    },
}


def load_dotenv(dotenv_path: Path) -> None:
    if not dotenv_path.exists():
        return

    for raw_line in dotenv_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def log_runtime_message(message: str) -> None:
    try:
        SERVER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SERVER_LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"{message}\n")
    except OSError:
        pass


def append_path(base_url: str, suffix: str) -> str:
    base_url = base_url.rstrip("/")
    suffix = suffix if suffix.startswith("/") else f"/{suffix}"
    return f"{base_url}{suffix}"


def strip_json_wrappers(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_render_deployment() -> bool:
    return bool(os.getenv("RENDER"))


def default_server_host() -> str:
    return "0.0.0.0" if is_render_deployment() else "127.0.0.1"


def sanitize_database_target(database_url: str, database_path: Path | None) -> str:
    if database_url:
        parsed = urlparse(database_url)
        host = parsed.hostname or "postgres"
        database_name = parsed.path.lstrip("/") or "app"
        return f"{host}/{database_name}"
    if database_path is not None:
        return str(database_path)
    return ""


def derive_project_name(raw_value: Any) -> str:
    return str(raw_value or "").strip()


def derive_region(project_name_location: Any) -> str:
    text = str(project_name_location or "").strip()
    if not text:
        return ""
    for separator in (" - ", " – "):
        if separator in text:
            return text.split(separator, 1)[1].strip()
    if "," in text:
        parts = [part.strip() for part in text.split(",")]
        if len(parts) > 1:
            return ", ".join(parts[1:]).strip()
    return ""


@dataclass
class BackendDefaults:
    default_provider: str
    claude_base_url: str
    claude_model: str
    openai_compatible_base_url: str
    openai_compatible_model: str


@dataclass
class AppConfig:
    host: str
    port: int
    defaults: BackendDefaults
    auth_mode: str
    dev_user_email: str
    demo_shared_password: str
    demo_allowed_emails: tuple[str, ...]
    session_cookie_name: str
    session_cookie_secure: bool
    database_backend: str
    database_target: str
    database_path: Path | None
    database_url: str

    @classmethod
    def from_env(cls, host: str, port: int) -> "AppConfig":
        defaults = BackendDefaults(
            default_provider=os.getenv("DEFAULT_AI_PROVIDER", "mock"),
            claude_base_url=os.getenv("CLAUDE_DEFAULT_BASE_URL", "https://api.anthropic.com"),
            claude_model=os.getenv("CLAUDE_DEFAULT_MODEL", "claude-sonnet-4-6"),
            openai_compatible_base_url=os.getenv("OPENAI_COMPATIBLE_DEFAULT_BASE_URL", "https://api.openai.com"),
            openai_compatible_model=os.getenv("OPENAI_COMPATIBLE_DEFAULT_MODEL", "gpt-5.4-mini"),
        )
        auth_mode = str(os.getenv("AUTH_MODE", "manual_invite")).strip() or "manual_invite"
        if auth_mode not in {"local_dev", "manual_invite"}:
            raise AppError(
                "AUTH_MODE must be either 'manual_invite' or 'local_dev'.",
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        allowed_emails = tuple(
            email
            for email in (
                item.strip().lower()
                for item in str(os.getenv("DEMO_ALLOWED_EMAILS", "")).split(",")
            )
            if email
        )
        database_url = str(os.getenv("DATABASE_URL", "")).strip()
        database_path: Path | None = None
        database_backend = "postgresql" if database_url else "sqlite"
        if database_backend == "sqlite":
            database_path = Path(os.getenv("DATABASE_PATH", str(DEFAULT_DATABASE_PATH))).expanduser()
            if not database_path.is_absolute():
                database_path = RUNTIME_ROOT / database_path
        secure_cookie_raw = str(os.getenv("SESSION_COOKIE_SECURE", "auto")).strip().lower() or "auto"
        session_cookie_secure = (
            is_render_deployment()
            if secure_cookie_raw == "auto"
            else secure_cookie_raw in {"1", "true", "yes", "on"}
        )
        return cls(
            host=host,
            port=port,
            defaults=defaults,
            auth_mode=auth_mode,
            dev_user_email=str(os.getenv("DEV_USER_EMAIL", "dev@local")).strip() or "dev@local",
            demo_shared_password=str(os.getenv("DEMO_SHARED_PASSWORD", "")).strip(),
            demo_allowed_emails=allowed_emails,
            session_cookie_name=str(os.getenv("SESSION_COOKIE_NAME", "impact_story_demo_session")).strip()
            or "impact_story_demo_session",
            session_cookie_secure=session_cookie_secure,
            database_backend=database_backend,
            database_target=sanitize_database_target(database_url, database_path),
            database_path=database_path,
            database_url=database_url,
        )


@dataclass
class ProviderSettings:
    provider: str
    api_key: str
    base_url: str
    model: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None, defaults: BackendDefaults) -> "ProviderSettings":
        raw = payload or {}
        provider = str(raw.get("provider") or defaults.default_provider or "mock").strip() or "mock"
        api_key = str(raw.get("apiKey") or "").strip()
        base_url = str(raw.get("baseUrl") or "").strip()
        model = str(raw.get("model") or "").strip()

        if provider == "claude":
            base_url = base_url or defaults.claude_base_url
            model = model or defaults.claude_model
        elif provider == "openai_compatible":
            model = model or defaults.openai_compatible_model
        else:
            provider = "mock"
            api_key = ""
            base_url = ""
            model = ""

        return cls(provider=provider, api_key=api_key, base_url=base_url, model=model)

    @property
    def provider_label(self) -> str:
        if self.provider == "claude":
            return "Claude API"
        if self.provider == "openai_compatible":
            return "OpenAI-compatible API"
        return "Mock AI"


@dataclass
class AuthenticatedUser:
    id: int
    email: str
    role: str
    auth_source: str


@dataclass
class StoryGenerationSettings:
    tone_key: str
    tone_label: str
    tone_description: str
    length_min: int
    length_max: int
    outcome_types: tuple[str, ...]


class PersistenceStore:
    def __init__(self, *, database_backend: str, database_path: Path | None, database_url: str) -> None:
        self.database_backend = database_backend
        self.database_path = database_path
        self.database_url = database_url
        if self.database_backend == "sqlite":
            if self.database_path is None:
                raise AppError("SQLite mode requires a database path.", status_code=HTTPStatus.INTERNAL_SERVER_ERROR)
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
        elif psycopg is None:
            raise AppError(
                "PostgreSQL support requires psycopg to be installed. Run the app with requirements.txt installed.",
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        self._initialize()

    def _connect(self) -> Any:
        if self.database_backend == "sqlite":
            connection = sqlite3.connect(self.database_path)
            connection.row_factory = sqlite3.Row
            return connection
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def _sql(self, sql: str) -> str:
        normalized = textwrap.dedent(sql).strip()
        if self.database_backend == "postgresql":
            return re.sub(r":([A-Za-z_][A-Za-z0-9_]*)", r"%(\1)s", normalized)
        return normalized

    def _execute(self, connection: Any, sql: str, params: dict[str, Any] | None = None) -> Any:
        return connection.execute(self._sql(sql), params or {})

    def _fetchone(self, connection: Any, sql: str, params: dict[str, Any] | None = None) -> Any:
        return self._execute(connection, sql, params).fetchone()

    def _fetchall(self, connection: Any, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return self._execute(connection, sql, params).fetchall()

    def _initialize(self) -> None:
        with self._connect() as connection:
            if self.database_backend == "sqlite":
                self._execute(connection, "PRAGMA journal_mode=WAL")
                user_id_type = "INTEGER"
                user_primary_key = "INTEGER PRIMARY KEY AUTOINCREMENT"
                active_flag_type = "INTEGER NOT NULL DEFAULT 1"
            else:
                user_id_type = "BIGINT"
                user_primary_key = "BIGSERIAL PRIMARY KEY"
                active_flag_type = "BOOLEAN NOT NULL DEFAULT TRUE"

            self._execute(
                connection,
                f"""
                CREATE TABLE IF NOT EXISTS users (
                    id {user_primary_key},
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT,
                    role TEXT NOT NULL DEFAULT 'editor',
                    auth_source TEXT NOT NULL,
                    cloudflare_subject TEXT,
                    is_active {active_flag_type},
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL
                )
                """,
            )
            self._execute(
                connection,
                f"""
                CREATE TABLE IF NOT EXISTS interviews (
                    id TEXT PRIMARY KEY,
                    created_by_user_id {user_id_type} NOT NULL,
                    updated_by_user_id {user_id_type} NOT NULL,
                    project_name TEXT NOT NULL DEFAULT '',
                    visibility TEXT NOT NULL DEFAULT 'private',
                    draft_status TEXT NOT NULL DEFAULT 'draft',
                    copied_from_interview_id TEXT,
                    current_step_index INTEGER NOT NULL DEFAULT 0,
                    review_return_step_index INTEGER,
                    answers_json TEXT NOT NULL DEFAULT '{{}}',
                    ai_inferences_json TEXT NOT NULL DEFAULT '{{}}',
                    generated_story TEXT NOT NULL DEFAULT '',
                    concise_version TEXT NOT NULL DEFAULT '',
                    review_notes_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (created_by_user_id) REFERENCES users(id),
                    FOREIGN KEY (updated_by_user_id) REFERENCES users(id)
                )
                """,
            )
            self._execute(
                connection,
                f"""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_token TEXT PRIMARY KEY,
                    user_id {user_id_type} NOT NULL,
                    created_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """,
            )
            self._migrate_interviews_table(connection)

    def _table_columns(self, connection: Any, table_name: str) -> set[str]:
        if self.database_backend == "sqlite":
            rows = self._fetchall(connection, f"PRAGMA table_info({table_name})")
            return {str(row["name"]) for row in rows}
        rows = self._fetchall(
            connection,
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = :table_name
            """,
            {"table_name": table_name},
        )
        return {str(row["column_name"]) for row in rows}

    def _migrate_interviews_table(self, connection: Any) -> None:
        columns = self._table_columns(connection, "interviews")
        if "project_name" not in columns:
            self._execute(connection, "ALTER TABLE interviews ADD COLUMN project_name TEXT NOT NULL DEFAULT ''")
        if "visibility" not in columns:
            self._execute(connection, "ALTER TABLE interviews ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private'")
        if "copied_from_interview_id" not in columns:
            self._execute(connection, "ALTER TABLE interviews ADD COLUMN copied_from_interview_id TEXT")
        self._execute(
            connection,
            "UPDATE interviews SET visibility = 'private' WHERE visibility IS NULL OR TRIM(visibility) = ''",
        )

    def get_or_create_user(
        self,
        *,
        email: str,
        auth_source: str,
        role: str = "editor",
        display_name: str | None = None,
        cloudflare_subject: str | None = None,
    ) -> AuthenticatedUser:
        normalized_email = email.strip().lower()
        if not normalized_email:
            raise AppError("Authenticated user email is missing.", status_code=HTTPStatus.UNAUTHORIZED)

        now = utc_now_iso()
        with self._connect() as connection:
            row = self._fetchone(
                connection,
                "SELECT * FROM users WHERE email = :email",
                {"email": normalized_email},
            )
            if row is None:
                params = {
                    "email": normalized_email,
                    "display_name": display_name,
                    "role": role,
                    "auth_source": auth_source,
                    "cloudflare_subject": cloudflare_subject,
                    "created_at": now,
                    "updated_at": now,
                    "last_seen_at": now,
                }
                if self.database_backend == "postgresql":
                    created = self._fetchone(
                        connection,
                        """
                        INSERT INTO users (
                            email,
                            display_name,
                            role,
                            auth_source,
                            cloudflare_subject,
                            created_at,
                            updated_at,
                            last_seen_at
                        ) VALUES (
                            :email,
                            :display_name,
                            :role,
                            :auth_source,
                            :cloudflare_subject,
                            :created_at,
                            :updated_at,
                            :last_seen_at
                        )
                        RETURNING id
                        """,
                        params,
                    )
                    user_id = int(created["id"])
                else:
                    cursor = self._execute(
                        connection,
                        """
                        INSERT INTO users (
                            email,
                            display_name,
                            role,
                            auth_source,
                            cloudflare_subject,
                            created_at,
                            updated_at,
                            last_seen_at
                        ) VALUES (
                            :email,
                            :display_name,
                            :role,
                            :auth_source,
                            :cloudflare_subject,
                            :created_at,
                            :updated_at,
                            :last_seen_at
                        )
                        """,
                        params,
                    )
                    user_id = int(cursor.lastrowid)
                return AuthenticatedUser(id=user_id, email=normalized_email, role=role, auth_source=auth_source)

            self._execute(
                connection,
                """
                UPDATE users
                SET last_seen_at = :last_seen_at,
                    updated_at = :updated_at,
                    auth_source = :auth_source,
                    cloudflare_subject = COALESCE(:cloudflare_subject, cloudflare_subject)
                WHERE id = :id
                """,
                {
                    "last_seen_at": now,
                    "updated_at": now,
                    "auth_source": auth_source,
                    "cloudflare_subject": cloudflare_subject,
                    "id": row["id"],
                },
            )
            return AuthenticatedUser(
                id=int(row["id"]),
                email=str(row["email"]),
                role=str(row["role"]),
                auth_source=auth_source,
            )

    def create_session(self, *, user_id: int) -> str:
        session_token = secrets.token_urlsafe(32)
        now = utc_now_iso()
        with self._connect() as connection:
            self._execute(
                connection,
                """
                INSERT INTO sessions (
                    session_token,
                    user_id,
                    created_at,
                    last_seen_at
                ) VALUES (:session_token, :user_id, :created_at, :last_seen_at)
                """,
                {
                    "session_token": session_token,
                    "user_id": user_id,
                    "created_at": now,
                    "last_seen_at": now,
                },
            )
        return session_token

    def get_user_by_session_token(self, session_token: str) -> AuthenticatedUser | None:
        normalized_token = session_token.strip()
        if not normalized_token:
            return None

        now = utc_now_iso()
        with self._connect() as connection:
            row = self._fetchone(
                connection,
                """
                SELECT
                    users.id,
                    users.email,
                    users.role,
                    users.auth_source
                FROM sessions
                JOIN users ON users.id = sessions.user_id
                WHERE sessions.session_token = :session_token
                """,
                {"session_token": normalized_token},
            )
            if row is None:
                return None
            self._execute(
                connection,
                "UPDATE sessions SET last_seen_at = :last_seen_at WHERE session_token = :session_token",
                {"last_seen_at": now, "session_token": normalized_token},
            )
            self._execute(
                connection,
                "UPDATE users SET last_seen_at = :last_seen_at, updated_at = :updated_at WHERE id = :id",
                {"last_seen_at": now, "updated_at": now, "id": row["id"]},
            )
            return AuthenticatedUser(
                id=int(row["id"]),
                email=str(row["email"]),
                role=str(row["role"]),
                auth_source=str(row["auth_source"]),
            )

    def delete_session(self, session_token: str) -> None:
        normalized_token = session_token.strip()
        if not normalized_token:
            return
        with self._connect() as connection:
            self._execute(
                connection,
                "DELETE FROM sessions WHERE session_token = :session_token",
                {"session_token": normalized_token},
            )

    def create_interview(self, *, user: AuthenticatedUser, draft: dict[str, Any]) -> dict[str, Any]:
        interview_id = str(uuid.uuid4())
        now = utc_now_iso()
        with self._connect() as connection:
            self._execute(
                connection,
                """
                INSERT INTO interviews (
                    id,
                    created_by_user_id,
                    updated_by_user_id,
                    project_name,
                    visibility,
                    draft_status,
                    copied_from_interview_id,
                    current_step_index,
                    review_return_step_index,
                    answers_json,
                    ai_inferences_json,
                    generated_story,
                    concise_version,
                    review_notes_json,
                    created_at,
                    updated_at
                ) VALUES (
                    :id,
                    :created_by_user_id,
                    :updated_by_user_id,
                    :project_name,
                    :visibility,
                    :draft_status,
                    :copied_from_interview_id,
                    :current_step_index,
                    :review_return_step_index,
                    :answers_json,
                    :ai_inferences_json,
                    :generated_story,
                    :concise_version,
                    :review_notes_json,
                    :created_at,
                    :updated_at
                )
                """,
                {
                    "id": interview_id,
                    "created_by_user_id": user.id,
                    "updated_by_user_id": user.id,
                    "project_name": draft["project_name"],
                    "visibility": draft["visibility"],
                    "draft_status": draft["draft_status"],
                    "copied_from_interview_id": draft["copied_from_interview_id"],
                    "current_step_index": draft["current_step_index"],
                    "review_return_step_index": draft["review_return_step_index"],
                    "answers_json": draft["answers_json"],
                    "ai_inferences_json": draft["ai_inferences_json"],
                    "generated_story": draft["generated_story"],
                    "concise_version": draft["concise_version"],
                    "review_notes_json": draft["review_notes_json"],
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return self.get_interview(interview_id=interview_id, user=user)

    def get_interview(self, *, interview_id: str, user: AuthenticatedUser) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_interview_row(connection, interview_id)
        if row is None:
            raise AppError("Interview draft not found.", status_code=HTTPStatus.NOT_FOUND)
        if not self._can_view_row(row, user):
            raise AppError("You do not have access to this interview draft.", status_code=HTTPStatus.FORBIDDEN)
        return self._serialize_interview(row, user=user)

    def update_interview(self, *, interview_id: str, user: AuthenticatedUser, draft: dict[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_interview_row(connection, interview_id)
            if row is None:
                raise AppError("Interview draft not found.", status_code=HTTPStatus.NOT_FOUND)
            if not self._can_view_row(row, user):
                raise AppError("You do not have access to this interview draft.", status_code=HTTPStatus.FORBIDDEN)
            if not self._is_owner_row(row, user):
                raise AppError(
                    "Only the interview owner can edit this draft. Copy it to My Drafts to make your own changes.",
                    status_code=HTTPStatus.FORBIDDEN,
                )

            self._execute(
                connection,
                """
                UPDATE interviews
                SET updated_by_user_id = :updated_by_user_id,
                    project_name = :project_name,
                    visibility = :visibility,
                    draft_status = :draft_status,
                    current_step_index = :current_step_index,
                    review_return_step_index = :review_return_step_index,
                    answers_json = :answers_json,
                    ai_inferences_json = :ai_inferences_json,
                    generated_story = :generated_story,
                    concise_version = :concise_version,
                    review_notes_json = :review_notes_json,
                    updated_at = :updated_at
                WHERE id = :id
                """,
                {
                    "updated_by_user_id": user.id,
                    "project_name": draft["project_name"],
                    "visibility": draft["visibility"],
                    "draft_status": draft["draft_status"],
                    "current_step_index": draft["current_step_index"],
                    "review_return_step_index": draft["review_return_step_index"],
                    "answers_json": draft["answers_json"],
                    "ai_inferences_json": draft["ai_inferences_json"],
                    "generated_story": draft["generated_story"],
                    "concise_version": draft["concise_version"],
                    "review_notes_json": draft["review_notes_json"],
                    "updated_at": utc_now_iso(),
                    "id": interview_id,
                },
            )
        return self.get_interview(interview_id=interview_id, user=user)

    def delete_interview(self, *, interview_id: str, user: AuthenticatedUser) -> dict[str, Any]:
        with self._connect() as connection:
            row = self._fetch_interview_row(connection, interview_id)
            if row is None:
                raise AppError("Interview draft not found.", status_code=HTTPStatus.NOT_FOUND)
            if not self._is_owner_row(row, user):
                raise AppError(
                    "Only the interview owner can delete this draft.",
                    status_code=HTTPStatus.FORBIDDEN,
                )
            self._execute(connection, "DELETE FROM interviews WHERE id = :id", {"id": interview_id})
        return {"deleted": True, "interviewId": interview_id}

    def list_interviews(self, *, scope: str, user: AuthenticatedUser) -> list[dict[str, Any]]:
        if scope not in {"mine", "shared", "all"}:
            raise AppError("scope must be one of: mine, shared, all.")

        query = """
            SELECT
                interviews.*,
                owner.email AS owner_email
            FROM interviews
            JOIN users AS owner ON owner.id = interviews.created_by_user_id
        """
        params: dict[str, Any] = {}
        if scope == "mine":
            query += " WHERE interviews.created_by_user_id = :user_id"
            params["user_id"] = user.id
        elif scope == "shared":
            query += " WHERE interviews.visibility = 'shared'"
        else:
            query += """
                WHERE interviews.created_by_user_id = :user_id
                   OR interviews.visibility = 'shared'
            """
            params["user_id"] = user.id
        query += " ORDER BY interviews.updated_at DESC"

        with self._connect() as connection:
            rows = self._fetchall(connection, query, params)
        return [self._serialize_interview_summary(row, user=user) for row in rows]

    def copy_interview(self, *, interview_id: str, user: AuthenticatedUser) -> dict[str, Any]:
        now = utc_now_iso()
        with self._connect() as connection:
            source = self._fetch_interview_row(connection, interview_id)
            if source is None:
                raise AppError("Interview draft not found.", status_code=HTTPStatus.NOT_FOUND)
            if not self._can_view_row(source, user):
                raise AppError("You do not have access to copy this interview draft.", status_code=HTTPStatus.FORBIDDEN)

            copied_id = str(uuid.uuid4())
            self._execute(
                connection,
                """
                INSERT INTO interviews (
                    id,
                    created_by_user_id,
                    updated_by_user_id,
                    project_name,
                    visibility,
                    draft_status,
                    copied_from_interview_id,
                    current_step_index,
                    review_return_step_index,
                    answers_json,
                    ai_inferences_json,
                    generated_story,
                    concise_version,
                    review_notes_json,
                    created_at,
                    updated_at
                ) VALUES (
                    :id,
                    :created_by_user_id,
                    :updated_by_user_id,
                    :project_name,
                    :visibility,
                    :draft_status,
                    :copied_from_interview_id,
                    :current_step_index,
                    :review_return_step_index,
                    :answers_json,
                    :ai_inferences_json,
                    :generated_story,
                    :concise_version,
                    :review_notes_json,
                    :created_at,
                    :updated_at
                )
                """,
                {
                    "id": copied_id,
                    "created_by_user_id": user.id,
                    "updated_by_user_id": user.id,
                    "project_name": str(source["project_name"] or ""),
                    "visibility": "private",
                    "draft_status": "draft",
                    "copied_from_interview_id": str(source["id"]),
                    "current_step_index": int(source["current_step_index"]),
                    "review_return_step_index": source["review_return_step_index"],
                    "answers_json": str(source["answers_json"] or "{}"),
                    "ai_inferences_json": str(source["ai_inferences_json"] or "{}"),
                    "generated_story": str(source["generated_story"] or ""),
                    "concise_version": str(source["concise_version"] or ""),
                    "review_notes_json": str(source["review_notes_json"] or "[]"),
                    "created_at": now,
                    "updated_at": now,
                },
            )
        return self.get_interview(interview_id=copied_id, user=user)

    def seed_shared_interview(self, *, current_user: AuthenticatedUser) -> dict[str, Any]:
        sample_email = "sample.collaborator@sei.org"
        sample_owner = self.get_or_create_user(email=sample_email, auth_source="demo_seed")
        sample_project = "Volta Basin Water Security - Ghana, West Africa"

        with self._connect() as connection:
            existing = self._fetchone(
                connection,
                """
                SELECT
                    interviews.*,
                    owner.email AS owner_email
                FROM interviews
                JOIN users AS owner ON owner.id = interviews.created_by_user_id
                WHERE interviews.created_by_user_id = :owner_id AND interviews.project_name = :project_name
                ORDER BY interviews.updated_at DESC
                LIMIT 1
                """,
                {"owner_id": sample_owner.id, "project_name": sample_project},
            )
            if existing is not None:
                return self._serialize_interview_summary(existing, user=current_user)

            now = utc_now_iso()
            interview_id = str(uuid.uuid4())
            answers = {
                "project_source_text": "We worked with basin authorities in Ghana to improve cross-border water planning using WEAP and targeted training.",
                "project_name_location": sample_project,
                "sei_activities": "Built a WEAP model, trained basin staff, and translated the findings into a policy-facing brief.",
                "project_adaptations": "Added a stakeholder scenario exercise after early workshops highlighted coordination gaps.",
                "primary_outcome_type": ["improved_decisions"],
                "primary_outcome_description": "Partner agencies now use the model to compare allocation scenarios before making seasonal planning decisions.",
                "beneficiaries_scale": "Three basin institutions and downstream communities across the Volta Basin.",
                "enabling_conditions": "Strong demand from basin authorities and good timing with an active policy review process.",
                "partner_quote": "\"The model gave us a practical way to compare options together.\" - Basin Authority participant",
                "impact_stat_1": "45 staff participated in the capacity-building series.",
                "impact_stat_2": "3 institutions adopted the shared scenario review process.",
                "future_potential": "The same approach could be adapted for neighboring basins facing similar coordination challenges.",
                "story_tone": "professional",
                "story_length_min": STORY_LENGTH_DEFAULT_MIN,
                "story_length_max": STORY_LENGTH_DEFAULT_MAX,
            }
            self._execute(
                connection,
                """
                INSERT INTO interviews (
                    id,
                    created_by_user_id,
                    updated_by_user_id,
                    project_name,
                    visibility,
                    draft_status,
                    copied_from_interview_id,
                    current_step_index,
                    review_return_step_index,
                    answers_json,
                    ai_inferences_json,
                    generated_story,
                    concise_version,
                    review_notes_json,
                    created_at,
                    updated_at
                ) VALUES (
                    :id,
                    :created_by_user_id,
                    :updated_by_user_id,
                    :project_name,
                    :visibility,
                    :draft_status,
                    :copied_from_interview_id,
                    :current_step_index,
                    :review_return_step_index,
                    :answers_json,
                    :ai_inferences_json,
                    :generated_story,
                    :concise_version,
                    :review_notes_json,
                    :created_at,
                    :updated_at
                )
                """,
                {
                    "id": interview_id,
                    "created_by_user_id": sample_owner.id,
                    "updated_by_user_id": sample_owner.id,
                    "project_name": sample_project,
                    "visibility": "shared",
                    "draft_status": "ready_for_review",
                    "copied_from_interview_id": None,
                    "current_step_index": 3,
                    "review_return_step_index": None,
                    "answers_json": json.dumps(answers, ensure_ascii=True),
                    "ai_inferences_json": json.dumps({}, ensure_ascii=True),
                    "generated_story": "",
                    "concise_version": "",
                    "review_notes_json": json.dumps([], ensure_ascii=True),
                    "created_at": now,
                    "updated_at": now,
                },
            )
            seeded = self._fetch_interview_row(connection, interview_id)
        return self._serialize_interview_summary(seeded, user=current_user)

    def _fetch_interview_row(self, connection: Any, interview_id: str) -> Any:
        return self._fetchone(
            connection,
            """
            SELECT
                interviews.*,
                owner.email AS owner_email
            FROM interviews
            JOIN users AS owner ON owner.id = interviews.created_by_user_id
            WHERE interviews.id = :id
            """,
            {"id": interview_id},
        )

    def _is_owner_row(self, row: Any, user: AuthenticatedUser) -> bool:
        return int(row["created_by_user_id"]) == user.id

    def _can_view_row(self, row: Any, user: AuthenticatedUser) -> bool:
        return self._is_owner_row(row, user) or str(row["visibility"] or "private") == "shared"

    def _normalize_outcome_types(self, raw_value: Any) -> list[str]:
        if isinstance(raw_value, list):
            raw_items = raw_value
        elif isinstance(raw_value, str):
            raw_items = [raw_value]
        else:
            raw_items = []

        outcome_types: list[str] = []
        for item in raw_items:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if value and value not in outcome_types:
                outcome_types.append(value)
        return outcome_types

    def _normalize_answers(self, answers: dict[str, Any]) -> dict[str, Any]:
        normalized_answers = dict(answers)
        normalized_answers["primary_outcome_type"] = self._normalize_outcome_types(
            normalized_answers.get("primary_outcome_type")
            if "primary_outcome_type" in normalized_answers
            else normalized_answers.get("primary_outcome_types")
        )
        if "story_tone" not in normalized_answers or not str(normalized_answers.get("story_tone") or "").strip():
            normalized_answers["story_tone"] = "professional"
        length_min = self._normalize_story_length_start_value(
            normalized_answers.get("story_length_min"),
            STORY_LENGTH_DEFAULT_MIN,
            normalized_answers.get("story_length_max"),
        )
        normalized_answers["story_length_min"] = length_min
        normalized_answers["story_length_max"] = self._derive_story_length_max(length_min)
        return normalized_answers

    def _outcome_type_labels(self, outcome_types: list[str] | tuple[str, ...]) -> list[str]:
        return [outcome_type.replace("_", " ").strip().title() for outcome_type in outcome_types if outcome_type]

    def _normalize_story_length_start_value(
        self,
        raw_value: Any,
        default_value: int,
        paired_max_value: Any | None = None,
    ) -> int:
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            parsed = default_value
            if paired_max_value is not None:
                try:
                    parsed = int(paired_max_value) - STORY_LENGTH_WINDOW
                except (TypeError, ValueError):
                    parsed = default_value
        return max(STORY_LENGTH_MIN, min(STORY_LENGTH_START_MAX, parsed))

    def _derive_story_length_max(self, length_min: int) -> int:
        return max(
            STORY_LENGTH_MIN + STORY_LENGTH_WINDOW,
            min(STORY_LENGTH_MAX, int(length_min) + STORY_LENGTH_WINDOW),
        )

    def _serialize_interview(self, row: Any, *, user: AuthenticatedUser) -> dict[str, Any]:
        answers = self._normalize_answers(json.loads(str(row["answers_json"] or "{}")))
        is_owner = self._is_owner_row(row, user)
        return {
            "id": str(row["id"]),
            "createdByUserId": int(row["created_by_user_id"]),
            "updatedByUserId": int(row["updated_by_user_id"]),
            "projectName": str(row["project_name"] or derive_project_name(answers.get("project_name_location"))),
            "title": str(row["project_name"] or derive_project_name(answers.get("project_name_location"))),
            "visibility": str(row["visibility"] or "private"),
            "draftStatus": str(row["draft_status"]),
            "copiedFromInterviewId": row["copied_from_interview_id"],
            "currentStepIndex": int(row["current_step_index"]),
            "reviewReturnStepIndex": row["review_return_step_index"],
            "answers": answers,
            "aiInferences": json.loads(str(row["ai_inferences_json"] or "{}")),
            "generatedStory": str(row["generated_story"] or ""),
            "conciseVersion": str(row["concise_version"] or ""),
            "reviewNotes": json.loads(str(row["review_notes_json"] or "[]")),
            "ownerEmail": str(row["owner_email"] or ""),
            "isOwner": is_owner,
            "canEdit": is_owner,
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
        }

    def _serialize_interview_summary(self, row: Any, *, user: AuthenticatedUser) -> dict[str, Any]:
        answers = self._normalize_answers(json.loads(str(row["answers_json"] or "{}")))
        project_name = str(row["project_name"] or derive_project_name(answers.get("project_name_location")) or "Untitled interview")
        outcome_types = self._normalize_outcome_types(answers.get("primary_outcome_type"))
        return {
            "id": str(row["id"]),
            "projectName": project_name,
            "title": project_name,
            "region": derive_region(answers.get("project_name_location") or project_name),
            "outcomeType": ", ".join(self._outcome_type_labels(outcome_types)),
            "outcomeTypes": outcome_types,
            "visibility": str(row["visibility"] or "private"),
            "draftStatus": str(row["draft_status"] or "draft"),
            "ownerEmail": str(row["owner_email"] or ""),
            "copiedFromInterviewId": row["copied_from_interview_id"],
            "updatedAt": str(row["updated_at"]),
            "createdAt": str(row["created_at"]),
            "isOwner": self._is_owner_row(row, user),
            "canEdit": self._is_owner_row(row, user),
        }


class AppError(Exception):
    def __init__(self, message: str, status_code: int = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class BaseProvider:
    provider_key = ""
    provider_label = ""

    def analyze_context(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        source_text: str,
        existing_answers: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def generate_story(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        answers: dict[str, Any],
        story_settings: StoryGenerationSettings,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def generate_concise_version(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        generated_story: str,
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        raise NotImplementedError

    def test_connection(self, *, service: "ImpactStoryService", settings: ProviderSettings) -> dict[str, Any]:
        raise NotImplementedError

    def _parse_json_text(self, raw_text: str) -> dict[str, Any]:
        cleaned = strip_json_wrappers(raw_text)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as error:
            raise AppError("The AI provider returned invalid JSON.", status_code=HTTPStatus.BAD_GATEWAY) from error


class MockProvider(BaseProvider):
    provider_key = "mock"
    provider_label = "Mock AI"

    def analyze_context(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        source_text: str,
        existing_answers: dict[str, Any],
    ) -> dict[str, Any]:
        return service._mock_analyze_context(source_text, existing_answers)

    def generate_story(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        answers: dict[str, Any],
        story_settings: StoryGenerationSettings,
    ) -> dict[str, Any]:
        return service._mock_generate_story(answers, story_settings)

    def generate_concise_version(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        generated_story: str,
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        return service._mock_generate_concise_version(generated_story, answers)

    def test_connection(self, *, service: "ImpactStoryService", settings: ProviderSettings) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": self.provider_key,
            "providerLabel": self.provider_label,
            "message": "Mock mode is ready. No API key is required.",
        }


class ClaudeProvider(BaseProvider):
    provider_key = "claude"
    provider_label = "Claude API"

    def analyze_context(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        source_text: str,
        existing_answers: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._structured_json_call(
            service=service,
            settings=settings,
            system_prompt=service.analysis_developer_prompt(),
            user_prompt=service.analysis_user_prompt(source_text, existing_answers),
            max_tokens=900,
        )
        return service.transform_analysis_result(result, mode=self.provider_key, provider_label=self.provider_label)

    def generate_story(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        answers: dict[str, Any],
        story_settings: StoryGenerationSettings,
    ) -> dict[str, Any]:
        result = self._structured_json_call(
            service=service,
            settings=settings,
            system_prompt=service.generation_developer_prompt(story_settings),
            user_prompt=service.generation_user_prompt(answers, story_settings),
            max_tokens=1600,
        )
        return service.transform_generation_result(result, mode=self.provider_key, provider_label=self.provider_label)

    def generate_concise_version(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        generated_story: str,
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._structured_json_call(
            service=service,
            settings=settings,
            system_prompt=service.concise_developer_prompt(),
            user_prompt=service.concise_user_prompt(generated_story, answers),
            max_tokens=500,
        )
        return service.transform_concise_result(result, mode=self.provider_key, provider_label=self.provider_label)

    def test_connection(self, *, service: "ImpactStoryService", settings: ProviderSettings) -> dict[str, Any]:
        self._require_api_key(settings)
        text = self._raw_message_call(
            settings=settings,
            system_prompt="Respond with exactly OK.",
            user_prompt="Return exactly OK.",
            max_tokens=12,
        )
        if "OK" not in text.upper():
            raise AppError("Claude API responded, but the response did not match the connection test.")
        return {
            "ok": True,
            "mode": self.provider_key,
            "providerLabel": self.provider_label,
            "baseUrl": self._messages_url(settings.base_url),
            "model": settings.model,
            "message": "Claude connection succeeded.",
        }

    def _structured_json_call(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        raw_text = self._raw_message_call(
            settings=settings,
            system_prompt=system_prompt + "\nReturn only valid JSON. Do not wrap the JSON in markdown fences.",
            user_prompt=user_prompt,
            max_tokens=max_tokens,
        )
        return self._parse_json_text(raw_text)

    def _raw_message_call(
        self,
        *,
        settings: ProviderSettings,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> str:
        self._require_api_key(settings)
        payload = {
            "model": settings.model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": user_prompt,
                }
            ],
        }
        request = urllib.request.Request(
            self._messages_url(settings.base_url),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "x-api-key": settings.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raw_body = error.read().decode("utf-8", errors="replace")
            raise AppError(self._extract_error_message(raw_body, "Claude API"), status_code=HTTPStatus.BAD_GATEWAY) from error
        except urllib.error.URLError as error:
            raise AppError(f"Claude API request failed: {error.reason}", status_code=HTTPStatus.BAD_GATEWAY) from error

        chunks: list[str] = []
        for content in body.get("content", []):
            if content.get("type") == "text" and isinstance(content.get("text"), str):
                chunks.append(content["text"])
        text = "\n".join(chunks).strip()
        if not text:
            raise AppError("Claude API returned no text output.", status_code=HTTPStatus.BAD_GATEWAY)
        return text

    def _messages_url(self, base_url: str) -> str:
        if base_url.rstrip("/").endswith("/v1/messages"):
            return base_url.rstrip("/")
        return append_path(base_url, "/v1/messages")

    def _require_api_key(self, settings: ProviderSettings) -> None:
        if not settings.api_key:
            raise AppError("API key missing for Claude provider.")

    def _extract_error_message(self, raw_body: str, provider_name: str) -> str:
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            return f"{provider_name} request failed."

        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            if message:
                return f"{provider_name} request failed: {message}"
        return f"{provider_name} request failed."


class OpenAICompatibleProvider(BaseProvider):
    provider_key = "openai_compatible"
    provider_label = "OpenAI-compatible API"

    def analyze_context(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        source_text: str,
        existing_answers: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._structured_json_call(
            service=service,
            settings=settings,
            system_prompt=service.analysis_developer_prompt(),
            user_prompt=service.analysis_user_prompt(source_text, existing_answers),
            max_tokens=900,
        )
        return service.transform_analysis_result(result, mode=self.provider_key, provider_label=self.provider_label)

    def generate_story(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        answers: dict[str, Any],
        story_settings: StoryGenerationSettings,
    ) -> dict[str, Any]:
        result = self._structured_json_call(
            service=service,
            settings=settings,
            system_prompt=service.generation_developer_prompt(story_settings),
            user_prompt=service.generation_user_prompt(answers, story_settings),
            max_tokens=1600,
        )
        return service.transform_generation_result(result, mode=self.provider_key, provider_label=self.provider_label)

    def generate_concise_version(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        generated_story: str,
        answers: dict[str, Any],
    ) -> dict[str, Any]:
        result = self._structured_json_call(
            service=service,
            settings=settings,
            system_prompt=service.concise_developer_prompt(),
            user_prompt=service.concise_user_prompt(generated_story, answers),
            max_tokens=500,
        )
        return service.transform_concise_result(result, mode=self.provider_key, provider_label=self.provider_label)

    def test_connection(self, *, service: "ImpactStoryService", settings: ProviderSettings) -> dict[str, Any]:
        self._validate_settings(settings)
        text = self._raw_chat_completion(
            settings=settings,
            system_prompt="Respond with exactly OK.",
            user_prompt="Return exactly OK.",
            max_tokens=12,
            use_json_mode=False,
        )
        if "OK" not in text.upper():
            raise AppError("The OpenAI-compatible endpoint responded, but the response did not match the connection test.")
        return {
            "ok": True,
            "mode": self.provider_key,
            "providerLabel": self.provider_label,
            "baseUrl": self._chat_completions_url(settings.base_url),
            "model": settings.model,
            "message": "OpenAI-compatible connection succeeded.",
        }

    def _structured_json_call(
        self,
        *,
        service: "ImpactStoryService",
        settings: ProviderSettings,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
    ) -> dict[str, Any]:
        try:
            raw_text = self._raw_chat_completion(
                settings=settings,
                system_prompt=system_prompt + "\nReturn only valid JSON. Do not wrap the JSON in markdown fences.",
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                use_json_mode=True,
            )
        except AppError as error:
            if not self._should_retry_without_json_mode(error):
                raise
            raw_text = self._raw_chat_completion(
                settings=settings,
                system_prompt=system_prompt + "\nReturn only valid JSON. Do not wrap the JSON in markdown fences.",
                user_prompt=user_prompt,
                max_tokens=max_tokens,
                use_json_mode=False,
            )
        return self._parse_json_text(raw_text)

    def _raw_chat_completion(
        self,
        *,
        settings: ProviderSettings,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        use_json_mode: bool,
    ) -> str:
        self._validate_settings(settings)
        payload: dict[str, Any] = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": max_tokens,
        }
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}

        request = urllib.request.Request(
            self._chat_completions_url(settings.base_url),
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raw_body = error.read().decode("utf-8", errors="replace")
            raise AppError(self._extract_error_message(raw_body), status_code=HTTPStatus.BAD_GATEWAY) from error
        except urllib.error.URLError as error:
            raise AppError(f"OpenAI-compatible request failed: {error.reason}", status_code=HTTPStatus.BAD_GATEWAY) from error

        choices = body.get("choices", [])
        if not choices:
            raise AppError("OpenAI-compatible provider returned no choices.", status_code=HTTPStatus.BAD_GATEWAY)

        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
            content = "\n".join(parts)
        if not isinstance(content, str) or not content.strip():
            raise AppError("OpenAI-compatible provider returned no text content.", status_code=HTTPStatus.BAD_GATEWAY)
        return content.strip()

    def _chat_completions_url(self, base_url: str) -> str:
        trimmed = base_url.rstrip("/")
        if trimmed.endswith("/chat/completions"):
            return trimmed
        if trimmed.endswith("/v1"):
            return append_path(trimmed, "/chat/completions")
        return append_path(trimmed, "/v1/chat/completions")

    def _validate_settings(self, settings: ProviderSettings) -> None:
        if not settings.api_key:
            raise AppError("API key missing for OpenAI-compatible provider.")
        if not settings.base_url:
            raise AppError("Base URL is required for OpenAI-compatible provider.")

    def _extract_error_message(self, raw_body: str) -> str:
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            return "OpenAI-compatible request failed."

        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            if message:
                return f"OpenAI-compatible request failed: {message}"
        return "OpenAI-compatible request failed."

    def _should_retry_without_json_mode(self, error: AppError) -> bool:
        message = error.message.lower()
        return "response_format" in message or "json_object" in message or "unsupported" in message


class ImpactStoryService:
    def __init__(self, config: AppConfig, schema: dict[str, Any]) -> None:
        self.config = config
        self.schema = schema
        self.store = PersistenceStore(
            database_backend=config.database_backend,
            database_path=config.database_path,
            database_url=config.database_url,
        )
        self.fields = self._index_fields(schema)
        self.outcome_options = {
            str(item.get("key")): str(item.get("label") or item.get("key") or "").strip()
            for item in schema.get("outcomeTypeOptions", [])
            if str(item.get("key") or "").strip()
        }
        self.providers: dict[str, BaseProvider] = {
            "mock": MockProvider(),
            "claude": ClaudeProvider(),
            "openai_compatible": OpenAICompatibleProvider(),
        }
        self.required_generation_fields = [
            field["fieldKey"]
            for field in self.fields.values()
            if field.get("missingStateDisplayRule", {}).get("blocksGeneration")
        ]

    def health(self) -> dict[str, Any]:
        return {
            "analysisAvailable": True,
            "generationAvailable": True,
            "supportedProviders": ["mock", "claude", "openai_compatible"],
            "authMode": self.config.auth_mode,
            "demoSeedAvailable": self.config.auth_mode == "local_dev",
            "manualInviteConfigured": bool(
                self.config.demo_shared_password and self.config.demo_allowed_emails
            ),
            "databaseBackend": self.config.database_backend,
            "databaseTarget": self.config.database_target,
            "defaults": {
                "defaultProvider": self.config.defaults.default_provider,
                "claude": {
                    "baseUrl": self.config.defaults.claude_base_url,
                    "model": self.config.defaults.claude_model,
                },
                "openaiCompatible": {
                    "baseUrl": self.config.defaults.openai_compatible_base_url,
                    "model": self.config.defaults.openai_compatible_model,
                },
            },
            "message": "Server ready. Select a provider in AI Settings.",
        }

    def current_user(self, headers: Any) -> dict[str, Any]:
        user = self._authenticate(headers)
        return {
            "id": user.id,
            "email": user.email,
            "role": user.role,
            "authSource": user.auth_source,
        }

    def login_manual_invite(self, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
        if self.config.auth_mode != "manual_invite":
            raise AppError("Manual invite sign-in is not enabled in the current auth mode.", status_code=HTTPStatus.BAD_REQUEST)

        if not self.config.demo_shared_password or not self.config.demo_allowed_emails:
            raise AppError(
                "Manual invite mode is not configured on the server. Set DEMO_ALLOWED_EMAILS and DEMO_SHARED_PASSWORD.",
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
            )

        email = str(payload.get("email") or "").strip().lower()
        password = str(payload.get("password") or "")

        if not email:
            raise AppError("Email is required.", status_code=HTTPStatus.UNAUTHORIZED)
        if email not in self.config.demo_allowed_emails:
            raise AppError("This email is not on the demo invite list.", status_code=HTTPStatus.UNAUTHORIZED)
        if password != self.config.demo_shared_password:
            raise AppError("Shared team password is incorrect.", status_code=HTTPStatus.UNAUTHORIZED)

        user = self.store.get_or_create_user(email=email, auth_source="manual_invite")
        session_token = self.store.create_session(user_id=user.id)
        return (
            {
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "role": user.role,
                    "authSource": user.auth_source,
                },
                "message": "Signed in.",
            },
            session_token,
        )

    def logout_manual_invite(self, headers: Any) -> dict[str, Any]:
        session_token = self._session_token_from_headers(headers)
        if session_token:
            self.store.delete_session(session_token)
        return {"message": "Signed out."}

    def analyze_context(self, payload: dict[str, Any], *, user: AuthenticatedUser) -> dict[str, Any]:
        source_text = str(payload.get("sourceText", "")).strip()
        existing_answers = payload.get("existingAnswers") or {}
        if not source_text:
            raise AppError("Source text is required for project context analysis.")

        settings = ProviderSettings.from_payload(payload.get("providerSettings"), self.config.defaults)
        provider = self._provider(settings.provider)
        return provider.analyze_context(
            service=self,
            settings=settings,
            source_text=source_text,
            existing_answers=existing_answers,
        )

    def generate_story(self, payload: dict[str, Any], *, user: AuthenticatedUser) -> dict[str, Any]:
        answers = self._normalize_answers(payload.get("answers") or {})
        story_settings = self._normalize_story_generation_settings(payload, answers)
        missing_required = [
            self.fields[field_key]["label"]
            for field_key in self.required_generation_fields
            if self._is_missing_field_value(field_key, answers.get(field_key))
        ]
        if missing_required:
            raise AppError(
                "Missing required fields for generation: " + ", ".join(missing_required),
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            )

        settings = ProviderSettings.from_payload(payload.get("providerSettings"), self.config.defaults)
        provider = self._provider(settings.provider)
        return provider.generate_story(
            service=self,
            settings=settings,
            answers=answers,
            story_settings=story_settings,
        )

    def generate_concise_version(self, payload: dict[str, Any], *, user: AuthenticatedUser) -> dict[str, Any]:
        answers = payload.get("answers") or {}
        generated_story = str(payload.get("generatedStory") or "").strip()
        if not generated_story:
            raise AppError("Generated story text is required before creating the concise version.")

        settings = ProviderSettings.from_payload(payload.get("providerSettings"), self.config.defaults)
        provider = self._provider(settings.provider)
        return provider.generate_concise_version(
            service=self,
            settings=settings,
            generated_story=generated_story,
            answers=answers,
        )

    def test_provider(self, payload: dict[str, Any], *, user: AuthenticatedUser) -> dict[str, Any]:
        settings = ProviderSettings.from_payload(payload.get("providerSettings"), self.config.defaults)
        provider = self._provider(settings.provider)
        return provider.test_connection(service=self, settings=settings)

    def create_interview(self, payload: dict[str, Any], *, user: AuthenticatedUser) -> dict[str, Any]:
        return self.store.create_interview(user=user, draft=self._normalize_interview_payload(payload))

    def list_interviews(self, scope: str, *, user: AuthenticatedUser) -> dict[str, Any]:
        return {
            "scope": scope,
            "interviews": self.store.list_interviews(scope=scope, user=user),
        }

    def get_interview(self, interview_id: str, *, user: AuthenticatedUser) -> dict[str, Any]:
        return self.store.get_interview(interview_id=interview_id, user=user)

    def update_interview(self, interview_id: str, payload: dict[str, Any], *, user: AuthenticatedUser) -> dict[str, Any]:
        return self.store.update_interview(interview_id=interview_id, user=user, draft=self._normalize_interview_payload(payload))

    def delete_interview(self, interview_id: str, *, user: AuthenticatedUser) -> dict[str, Any]:
        return self.store.delete_interview(interview_id=interview_id, user=user)

    def copy_interview(self, interview_id: str, *, user: AuthenticatedUser) -> dict[str, Any]:
        return self.store.copy_interview(interview_id=interview_id, user=user)

    def seed_shared_interview(self, *, user: AuthenticatedUser) -> dict[str, Any]:
        if self.config.auth_mode != "local_dev":
            raise AppError("Sample shared interview seeding is available only in local_dev mode.", status_code=HTTPStatus.FORBIDDEN)
        return self.store.seed_shared_interview(current_user=user)

    def _authenticate(self, headers: Any) -> AuthenticatedUser:
        if self.config.auth_mode == "local_dev":
            return self.store.get_or_create_user(email=self.config.dev_user_email, auth_source="local_dev")

        session_token = self._session_token_from_headers(headers)
        if not session_token:
            raise AppError("Sign in to continue.", status_code=HTTPStatus.UNAUTHORIZED)

        user = self.store.get_user_by_session_token(session_token)
        if user is None:
            raise AppError("Your demo session has expired. Sign in again.", status_code=HTTPStatus.UNAUTHORIZED)
        return user

    def session_cookie_header(self, session_token: str) -> str:
        cookie = SimpleCookie()
        cookie[self.config.session_cookie_name] = session_token
        morsel = cookie[self.config.session_cookie_name]
        morsel["path"] = "/"
        morsel["httponly"] = True
        morsel["samesite"] = "Lax"
        if self.config.session_cookie_secure:
            morsel["secure"] = True
        return morsel.OutputString()

    def clear_session_cookie_header(self) -> str:
        cookie = SimpleCookie()
        cookie[self.config.session_cookie_name] = ""
        morsel = cookie[self.config.session_cookie_name]
        morsel["path"] = "/"
        morsel["httponly"] = True
        morsel["samesite"] = "Lax"
        if self.config.session_cookie_secure:
            morsel["secure"] = True
        morsel["max-age"] = 0
        return morsel.OutputString()

    def _session_token_from_headers(self, headers: Any) -> str:
        cookie_header = str(headers.get("Cookie") or "").strip()
        if not cookie_header:
            return ""
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(self.config.session_cookie_name)
        if morsel is None:
            return ""
        return morsel.value.strip()

    def _normalize_interview_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        answers = payload.get("answers")
        ai_inferences = payload.get("aiInferences")
        review_notes = payload.get("reviewNotes")
        if answers is None and "draft" in payload and isinstance(payload.get("draft"), dict):
            draft = payload["draft"]
            answers = draft.get("answers")
            ai_inferences = draft.get("aiInferences")
            review_notes = draft.get("reviewNotes")
            payload = draft

        current_step_index = payload.get("currentStepIndex", 0)
        review_return_step_index = payload.get("reviewReturnStepIndex")

        try:
            normalized_step_index = max(0, int(current_step_index))
        except (TypeError, ValueError) as error:
            raise AppError("currentStepIndex must be a number.") from error

        normalized_review_step = None
        if review_return_step_index not in (None, ""):
            try:
                normalized_review_step = int(review_return_step_index)
            except (TypeError, ValueError) as error:
                raise AppError("reviewReturnStepIndex must be a number or null.") from error

        if answers is None:
            answers = {}
        if ai_inferences is None:
            ai_inferences = {}
        if review_notes is None:
            review_notes = []

        if not isinstance(answers, dict):
            raise AppError("answers must be an object.")
        if not isinstance(ai_inferences, dict):
            raise AppError("aiInferences must be an object.")
        if not isinstance(review_notes, list):
            raise AppError("reviewNotes must be an array.")

        answers = self._normalize_answers(answers)

        project_name = derive_project_name(payload.get("projectName"))
        if not project_name:
            project_name = derive_project_name(answers.get("project_name_location"))

        visibility = str(payload.get("visibility") or "private").strip().lower() or "private"
        if visibility not in VALID_VISIBILITIES:
            raise AppError("visibility must be either 'private' or 'shared'.")

        draft_status = str(payload.get("draftStatus") or payload.get("draft_status") or "draft").strip().lower() or "draft"
        if draft_status not in VALID_DRAFT_STATUSES:
            raise AppError("draftStatus must be one of: draft, ready_for_review, final.")

        return {
            "project_name": project_name,
            "visibility": visibility,
            "draft_status": draft_status,
            "copied_from_interview_id": None,
            "current_step_index": normalized_step_index,
            "review_return_step_index": normalized_review_step,
            "answers_json": json.dumps(answers, ensure_ascii=True),
            "ai_inferences_json": json.dumps(ai_inferences, ensure_ascii=True),
            "generated_story": str(payload.get("generatedStory") or ""),
            "concise_version": str(payload.get("conciseVersion") or ""),
            "review_notes_json": json.dumps(review_notes, ensure_ascii=True),
        }

    def _normalize_answers(self, answers: dict[str, Any]) -> dict[str, Any]:
        normalized_answers = dict(answers)
        normalized_answers["primary_outcome_type"] = self._normalize_outcome_types(
            normalized_answers.get("primary_outcome_type")
            if "primary_outcome_type" in normalized_answers
            else normalized_answers.get("primary_outcome_types")
        )
        normalized_answers["story_tone"] = self._normalize_story_tone(normalized_answers.get("story_tone"))
        length_min = self._normalize_story_length_start_value(
            normalized_answers.get("story_length_min"),
            STORY_LENGTH_DEFAULT_MIN,
            normalized_answers.get("story_length_max"),
        )
        normalized_answers["story_length_min"] = length_min
        normalized_answers["story_length_max"] = self._derive_story_length_max(length_min)
        return normalized_answers

    def _normalize_outcome_types(self, raw_value: Any) -> list[str]:
        if isinstance(raw_value, list):
            raw_items = raw_value
        elif isinstance(raw_value, str):
            raw_items = [raw_value]
        else:
            raw_items = []

        outcome_types: list[str] = []
        for item in raw_items:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if value and value not in outcome_types:
                outcome_types.append(value)
        return outcome_types

    def _normalize_story_tone(self, raw_value: Any) -> str:
        tone_key = str(raw_value or "").strip() or "professional"
        if tone_key == "formal":
            return "professional"
        return tone_key if tone_key in STORY_TONE_OPTIONS else "professional"

    def _normalize_story_length_value(self, raw_value: Any, default_value: int) -> int:
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            return default_value
        return max(STORY_LENGTH_MIN, min(STORY_LENGTH_MAX, parsed))

    def _normalize_story_length_start_value(
        self,
        raw_value: Any,
        default_value: int,
        paired_max_value: Any | None = None,
    ) -> int:
        try:
            parsed = int(raw_value)
        except (TypeError, ValueError):
            parsed = default_value
            if paired_max_value is not None:
                try:
                    parsed = int(paired_max_value) - STORY_LENGTH_WINDOW
                except (TypeError, ValueError):
                    parsed = default_value
        return max(STORY_LENGTH_MIN, min(STORY_LENGTH_START_MAX, parsed))

    def _derive_story_length_max(self, length_min: int) -> int:
        return max(
            STORY_LENGTH_MIN + STORY_LENGTH_WINDOW,
            min(STORY_LENGTH_MAX, int(length_min) + STORY_LENGTH_WINDOW),
        )

    def _normalize_story_generation_settings(
        self,
        payload: dict[str, Any],
        answers: dict[str, Any],
    ) -> StoryGenerationSettings:
        tone_key = self._normalize_story_tone(payload.get("tone") or answers.get("story_tone"))
        length_min = self._normalize_story_length_start_value(
            payload.get("lengthMin") if payload.get("lengthMin") is not None else answers.get("story_length_min"),
            STORY_LENGTH_DEFAULT_MIN,
            payload.get("lengthMax") if payload.get("lengthMax") is not None else answers.get("story_length_max"),
        )
        length_max = self._normalize_story_length_value(
            payload.get("lengthMax") if payload.get("lengthMax") is not None else answers.get("story_length_max"),
            self._derive_story_length_max(length_min),
        )
        outcome_types = tuple(
            self._normalize_outcome_types(
                payload.get("outcomeTypes")
                if payload.get("outcomeTypes") is not None
                else answers.get("primary_outcome_type")
            )
        )

        if length_max < length_min:
            raise AppError("Maximum length must be greater than or equal to minimum length.")
        if length_max - length_min < STORY_LENGTH_MIN_INTERVAL:
            raise AppError(
                f"Keep at least {STORY_LENGTH_MIN_INTERVAL} words between minimum and maximum length."
            )

        tone_config = STORY_TONE_OPTIONS[tone_key]
        return StoryGenerationSettings(
            tone_key=tone_key,
            tone_label=str(tone_config["label"]),
            tone_description=str(tone_config["description"]),
            length_min=length_min,
            length_max=length_max,
            outcome_types=outcome_types,
        )

    def _is_missing_field_value(self, field_key: str, raw_value: Any) -> bool:
        field = self.fields.get(field_key) or {}
        input_type = str(field.get("inputType") or "")
        if input_type == "multi_select_cards":
            return len(self._normalize_outcome_types(raw_value)) == 0
        if isinstance(raw_value, list):
            return len(raw_value) == 0
        return not str(raw_value or "").strip()

    def _outcome_type_labels(self, outcome_types: list[str] | tuple[str, ...]) -> list[str]:
        return [self.outcome_options.get(outcome_type, outcome_type.replace("_", " ")) for outcome_type in outcome_types]

    def analysis_developer_prompt(self) -> str:
        return textwrap.dedent(
            """
            You are extracting structured project context for the Impact Story Builder.
            Infer only what is explicitly stated or strongly implied by the pasted source text.
            Never invent facts, geographies, people, or statistics.
            Focus only on these two fields:
            - project_name_location: a concise "project name - place" style string when the source supports it.
            - sei_activities: a concise summary of what SEI did, using concrete verbs and preserving important activities.
            Return JSON that matches the requested structure.
            Confidence must be a number between 0 and 1.
            If the source does not support a field, omit it from inferred_fields instead of guessing.
            """
        ).strip()

    def analysis_user_prompt(self, source_text: str, existing_answers: dict[str, Any]) -> str:
        return textwrap.dedent(
            f"""
            Existing answers:
            {json.dumps(existing_answers, indent=2, ensure_ascii=True)}

            Source text to analyze:
            \"\"\"
            {source_text}
            \"\"\"

            Prefer filling blank fields. If an existing answer is already specific, do not produce a competing inference unless the source clearly confirms the same value.

            Return this JSON shape:
            {{
              "inferred_fields": [
                {{
                  "field_key": "project_name_location" or "sei_activities",
                  "value": "string",
                  "confidence": 0.0,
                  "rationale": "string"
                }}
              ],
              "summary": "string",
              "follow_up_gaps": ["string"]
            }}
            """
        ).strip()

    def generation_developer_prompt(self, story_settings: StoryGenerationSettings) -> str:
        template = self.schema.get("storyTemplate", {})
        structure = "\n".join(
            f"{item['order']}. {item['instruction']}" for item in template.get("requiredStructure", [])
        )
        tone_rules = "\n".join(f"- {rule}" for rule in template.get("toneRules", []))
        return textwrap.dedent(
            f"""
            You write SEI change stories for funder and policy audiences.
            Write a single polished draft between {story_settings.length_min} and {story_settings.length_max} words.
            Use the selected tone: {story_settings.tone_label}.
            Tone guidance: {story_settings.tone_description}

            Required structure:
            {structure}

            Tone rules:
            {tone_rules}

            Important generation rules:
            - Keep within the selected word-count range as closely as possible.
            - Use contribution language where appropriate. Do not overstate causality.
            - Use the partner quote verbatim when one is provided.
            - If no quote is provided, include [QUOTE NEEDED] in the draft and mention the gap in review_notes.
            - If the first statistic is missing, include [STAT NEEDED] in the draft and mention it in review_notes.
            - If the second statistic is missing, keep the draft readable but include [SECOND STAT RECOMMENDED] in the draft and mention it in review_notes.
            - If multiple outcome types are selected, weave them together in a coherent way instead of listing disconnected categories.
            - Do not invent statistics, quotes, formal adoption, policy impact, or scale.
            - Always expand WEAP on first mention as Water Evaluation and Adaptation Planning (WEAP). After the first mention, use WEAP.
            - Keep the story accessible. Avoid unexplained acronyms and internal jargon.
            - Return JSON that matches the requested structure.
            """
        ).strip()

    def generation_user_prompt(self, answers: dict[str, Any], story_settings: StoryGenerationSettings) -> str:
        selected_outcome_labels = self._outcome_type_labels(story_settings.outcome_types)
        ordered_answers = {
            field_key: (
                self._normalize_outcome_types(answers.get(field_key))
                if field_key == "primary_outcome_type"
                else str(answers.get(field_key, "")).strip()
            )
            for field_key in [
                "project_source_text",
                "project_name_location",
                "sei_activities",
                "project_adaptations",
                "primary_outcome_type",
                "primary_outcome_description",
                "beneficiaries_scale",
                "enabling_conditions",
                "partner_quote",
                "impact_stat_1",
                "impact_stat_2",
                "future_potential",
            ]
        }
        return textwrap.dedent(
            f"""
            Story settings:
            {json.dumps(
                {
                    "tone": story_settings.tone_label,
                    "tone_description": story_settings.tone_description,
                    "target_word_count_range": f"{story_settings.length_min}-{story_settings.length_max}",
                    "selected_outcome_types": list(story_settings.outcome_types),
                    "selected_outcome_labels": selected_outcome_labels,
                },
                indent=2,
                ensure_ascii=True,
            )}

            Create an impact story draft from these interview answers:
            {json.dumps(ordered_answers, indent=2, ensure_ascii=True)}

            The related resource link is intentionally deferred in this MVP, so do not invent one.

            Return this JSON shape:
            {{
              "story_draft": "string",
              "review_notes": ["string"]
            }}
            """
        ).strip()

    def concise_developer_prompt(self) -> str:
        return textwrap.dedent(
            """
            You are rewriting an impact story into a shorter public-facing version suitable for a professional social media draft.
            Keep it concise, clear, factual, and readable by a general audience.
            Do not use hashtags.
            Do not invent new facts.
            Always expand WEAP on first mention as Water Evaluation and Adaptation Planning (WEAP). After the first mention, use WEAP.
            Keep the tone professional, not promotional or exaggerated.
            Aim for roughly 70 to 120 words.
            Return JSON that matches the requested structure.
            """
        ).strip()

    def concise_user_prompt(self, generated_story: str, answers: dict[str, Any]) -> str:
        key_facts = {
            field_key: str(answers.get(field_key, "")).strip()
            for field_key in [
                "project_name_location",
                "sei_activities",
                "primary_outcome_description",
                "beneficiaries_scale",
                "impact_stat_1",
                "impact_stat_2",
                "future_potential",
            ]
        }
        return textwrap.dedent(
            f"""
            Full impact story draft:
            \"\"\"
            {generated_story}
            \"\"\"

            Key facts:
            {json.dumps(key_facts, indent=2, ensure_ascii=True)}

            Return this JSON shape:
            {{
              "concise_version": "string"
            }}
            """
        ).strip()

    def transform_analysis_result(self, result: dict[str, Any], *, mode: str, provider_label: str) -> dict[str, Any]:
        inferred_fields = [
            {
                "fieldKey": item["field_key"],
                "value": item["value"],
                "confidence": item["confidence"],
                "rationale": item["rationale"],
            }
            for item in result.get("inferred_fields", [])
            if item.get("field_key") in {"project_name_location", "sei_activities"}
        ]
        return {
            "mode": mode,
            "providerLabel": provider_label,
            "inferredFields": inferred_fields,
            "summary": str(result.get("summary") or "").strip(),
            "followUpGaps": [str(item).strip() for item in result.get("follow_up_gaps", []) if str(item).strip()],
        }

    def transform_generation_result(self, result: dict[str, Any], *, mode: str, provider_label: str) -> dict[str, Any]:
        story_draft = str(result.get("story_draft") or "").strip()
        if not story_draft:
            raise AppError("The AI provider returned an empty story draft.", status_code=HTTPStatus.BAD_GATEWAY)
        review_notes = [str(note).strip() for note in result.get("review_notes", []) if str(note).strip()]
        return {
            "mode": mode,
            "providerLabel": provider_label,
            "storyDraft": story_draft,
            "reviewNotes": review_notes,
            "wordCount": len(story_draft.split()),
        }

    def transform_concise_result(self, result: dict[str, Any], *, mode: str, provider_label: str) -> dict[str, Any]:
        concise_version = str(result.get("concise_version") or "").strip()
        if not concise_version:
            raise AppError("The AI provider returned an empty concise version.", status_code=HTTPStatus.BAD_GATEWAY)
        return {
            "mode": mode,
            "providerLabel": provider_label,
            "conciseVersion": concise_version,
            "wordCount": len(concise_version.split()),
        }

    def _provider(self, provider_key: str) -> BaseProvider:
        provider = self.providers.get(provider_key)
        if not provider:
            raise AppError(f"Unsupported provider: {provider_key}")
        return provider

    def _mock_analyze_context(self, source_text: str, existing_answers: dict[str, Any]) -> dict[str, Any]:
        inferred_fields: list[dict[str, Any]] = []
        project_hint = self._mock_project_name_location(source_text)
        activities_hint = self._mock_activity_summary(source_text)

        if not str(existing_answers.get("project_name_location", "")).strip() and project_hint:
            inferred_fields.append(
                {
                    "fieldKey": "project_name_location",
                    "value": project_hint,
                    "confidence": 0.42,
                    "rationale": "Mock inference based on phrases in the pasted source text.",
                }
            )

        if activities_hint:
            inferred_fields.append(
                {
                    "fieldKey": "sei_activities",
                    "value": activities_hint,
                    "confidence": 0.58,
                    "rationale": "Mock activity summary extracted from the first part of the source text.",
                }
            )

        return {
            "mode": "mock",
            "providerLabel": "Mock AI",
            "inferredFields": inferred_fields,
            "summary": "Mock analysis generated local prefills from the pasted project text.",
            "followUpGaps": [
                "Confirm the inferred project label and location.",
                "Review the activity summary and add any missing outputs or adaptations.",
            ],
        }

    def _mock_generate_story(self, answers: dict[str, Any], story_settings: StoryGenerationSettings) -> dict[str, Any]:
        project = self._answer(answers, "project_name_location", "This project")
        activities = self._clean_fragment(self._answer(answers, "sei_activities", "delivered a set of technical activities"))
        outcome_labels = self._outcome_type_labels(
            story_settings.outcome_types or tuple(self._normalize_outcome_types(answers.get("primary_outcome_type")))
        )
        outcome_phrase = self._format_outcome_type_phrase(outcome_labels or ["meaningful change"])
        outcome = self._clean_fragment(self._answer(answers, "primary_outcome_description", "created a meaningful outcome"))
        beneficiaries = self._clean_fragment(self._answer(answers, "beneficiaries_scale", "key partners and communities"))
        enabling_conditions = self._clean_fragment(self._answer(answers, "enabling_conditions", "timing, trust, and strong collaboration aligned"))
        quote = self._answer(answers, "partner_quote", "[QUOTE NEEDED]")
        stat_1 = self._clean_fragment(self._answer(answers, "impact_stat_1", "[STAT NEEDED]"))
        stat_2 = self._clean_fragment(self._answer(answers, "impact_stat_2", "[SECOND STAT RECOMMENDED]"))
        future_potential = self._clean_fragment(self._answer(answers, "future_potential", "the approach could be scaled further"))
        project_adaptations = self._clean_fragment(self._answer(answers, "project_adaptations", ""))
        source_text = self._answer(answers, "project_source_text", "")

        source_sentence = (
            "This draft draws on pasted project material as well as direct interview responses, which helps ground the narrative in existing documentation."
            if source_text
            else "This draft relies on direct interview responses because no source material was pasted into the project context field."
        )
        adaptation_sentence = (
            f"An important implementation detail was that {self._lowercase_first(project_adaptations)}."
            if project_adaptations
            else "Any adaptive changes or unexpected project pivots could still be added to strengthen the implementation narrative."
        )
        quote_sentence = (
            "A confirmed partner or policymaker quote should still be added before wider sharing."
            if quote == "[QUOTE NEEDED]"
            else f"As one partner put it, {quote}"
        )
        stat_2_sentence = (
            "A second supporting statistic would make the story more persuasive for funder and policy audiences."
            if stat_2 == "[SECOND STAT RECOMMENDED]"
            else f"{self._sentence_case(stat_2)}."
        )
        if story_settings.tone_key == "conversational":
            opening_sentence = f"{project} shows how SEI turned practical project work into visible progress across {outcome_phrase}."
            closing_sentence = f"Looking ahead, {self._lowercase_first(future_potential)}. This gives colleagues a strong starting draft to refine rather than beginning from scratch."
        elif story_settings.tone_key == "funder_facing":
            opening_sentence = f"{project} shows how SEI contributed to measurable progress across {outcome_phrase}."
            closing_sentence = f"Looking ahead, {self._lowercase_first(future_potential)}. That future pathway reinforces why this contribution is worth sustaining and scaling."
        else:
            opening_sentence = f"{project} demonstrates how SEI translated technical collaboration into clear, evidence-based progress across {outcome_phrase}."
            closing_sentence = f"Looking ahead, {self._lowercase_first(future_potential)}. This draft already provides a polished first version that a colleague can refine."

        core_sentences = [
            opening_sentence,
            f"Through this work, the team {self._lowercase_first(activities)}.",
            f"The most important result was that {self._lowercase_first(outcome)}.",
            f"The change benefited {beneficiaries}, which helps explain why the project mattered beyond activity delivery.",
            f"This progress became possible because {self._lowercase_first(enabling_conditions)}.",
            closing_sentence,
        ]
        supporting_sentences = [
            source_sentence,
            f"One signal of scale is that {self._lowercase_first(stat_1)}.",
            stat_2_sentence,
            adaptation_sentence,
            quote_sentence,
            "Together, those details move the draft beyond a generic success summary and make the story easier to use in communications.",
            "The structure also helps connect what SEI did, what changed, and why that change matters for future action.",
            "Where useful, the final version can still be tailored for donor reporting, internal communications, or policy outreach.",
        ]
        extension_sentences = [
            "That mix of action, evidence, and context helps the story stay credible while still being readable for non-specialist audiences.",
            "It also shows that the project mattered not only because activities happened, but because those activities supported a practical shift in decisions, practice, or capacity.",
            "For internal teams, this kind of draft makes it easier to refine audience-specific language without losing the core evidence and project logic.",
            "For external audiences, the emphasis on outcomes, scale, and enabling conditions creates a clearer link between technical work and public value.",
        ]

        target_min = story_settings.length_min
        target_max = story_settings.length_max
        target_midpoint = (target_min + target_max) // 2
        selected_sentences = list(core_sentences)
        next_support_index = 0

        while self._word_count(" ".join(selected_sentences)) < target_min and next_support_index < len(supporting_sentences):
            selected_sentences.append(supporting_sentences[next_support_index])
            next_support_index += 1

        while (
            self._word_count(" ".join(selected_sentences)) < target_midpoint
            and next_support_index < len(supporting_sentences)
        ):
            selected_sentences.append(supporting_sentences[next_support_index])
            next_support_index += 1

        extension_index = 0
        while self._word_count(" ".join(selected_sentences)) < target_min:
            selected_sentences.append(extension_sentences[extension_index % len(extension_sentences)])
            extension_index += 1

        story = self._expand_weap_first_mention(self._sentences_to_story(selected_sentences))

        if self._word_count(story) > target_max and len(selected_sentences) > len(core_sentences):
            while self._word_count(story) > target_max and len(selected_sentences) > len(core_sentences):
                selected_sentences.pop()
                story = self._expand_weap_first_mention(self._sentences_to_story(selected_sentences))

        review_notes: list[str] = []
        if quote == "[QUOTE NEEDED]":
            review_notes.append("Quote gap: add a partner, beneficiary, or policymaker quote before final external use.")
        if stat_2 == "[SECOND STAT RECOMMENDED]":
            review_notes.append("Evidence gap: add a second supporting statistic to strengthen the story.")
        if not review_notes:
            review_notes.append("All core fields are present. Review wording and evidence before sharing externally.")

        return {
            "mode": "mock",
            "providerLabel": "Mock AI",
            "storyDraft": story,
            "reviewNotes": review_notes,
            "wordCount": len(story.split()),
        }

    def _mock_generate_concise_version(self, generated_story: str, answers: dict[str, Any]) -> dict[str, Any]:
        project = self._answer(answers, "project_name_location", "This project")
        outcome = self._clean_fragment(self._answer(answers, "primary_outcome_description", "delivered a meaningful outcome"))
        beneficiaries = self._clean_fragment(self._answer(answers, "beneficiaries_scale", "partners and communities"))
        stat_1 = self._clean_fragment(self._answer(answers, "impact_stat_1", "the work reached an important population"))
        future_potential = self._clean_fragment(self._answer(answers, "future_potential", "the work has potential to scale further"))
        activities = self._clean_fragment(self._answer(answers, "sei_activities", "worked closely with partners"))

        concise = " ".join(
            [
                f"{project}: SEI {self._lowercase_first(activities)} and helped drive a practical outcome.",
                f"The clearest result was that {self._lowercase_first(outcome)}, with benefits for {beneficiaries}.",
                f"One signal of why this matters is that {self._lowercase_first(stat_1)}.",
                f"Looking ahead, {self._lowercase_first(future_potential)}.",
            ]
        ).strip()
        concise = self._expand_weap_first_mention(concise)

        return {
            "mode": "mock",
            "providerLabel": "Mock AI",
            "conciseVersion": concise,
            "wordCount": len(concise.split()),
        }

    def _format_outcome_type_phrase(self, outcome_labels: list[str]) -> str:
        cleaned = [label.strip() for label in outcome_labels if label.strip()]
        if not cleaned:
            return "meaningful change"
        if len(cleaned) == 1:
            return cleaned[0].lower()
        if len(cleaned) == 2:
            return f"{cleaned[0].lower()} and {cleaned[1].lower()}"
        return f"{', '.join(label.lower() for label in cleaned[:-1])}, and {cleaned[-1].lower()}"

    def _word_count(self, text: str) -> int:
        return len(text.split())

    def _sentences_to_story(self, sentences: list[str]) -> str:
        paragraphs: list[str] = []
        for index in range(0, len(sentences), 2):
            paragraphs.append(" ".join(sentences[index : index + 2]).strip())
        return "\n\n".join(paragraphs).strip()

    def _expand_weap_first_mention(self, text: str) -> str:
        if not text:
            return text
        updated = re.sub(
            r"Water Evaluation and Planning\s*\(WEAP\)",
            "Water Evaluation and Adaptation Planning (WEAP)",
            text,
            count=1,
        )
        updated = re.sub(
            r"Water Evaluation and Planning",
            "Water Evaluation and Adaptation Planning",
            updated,
        )
        if "Water Evaluation and Adaptation Planning (WEAP)" in updated:
            return updated
        return re.sub(r"\bWEAP\b", "Water Evaluation and Adaptation Planning (WEAP)", updated, count=1)

    def _mock_project_name_location(self, source_text: str) -> str:
        lower = source_text.lower()
        if "volta basin" in lower and "ghana" in lower:
            return "Volta Basin project - Ghana"
        if "ghana" in lower:
            return "Project inferred from source text - Ghana"
        if "kenya" in lower:
            return "Project inferred from source text - Kenya"
        return ""

    def _mock_activity_summary(self, source_text: str) -> str:
        sentence = source_text.strip().split(".")[0].strip()
        if not sentence:
            return ""
        sentence = sentence[:220].rstrip(" ,;:")
        if not sentence.endswith("."):
            sentence += "."
        return sentence

    def _index_fields(self, schema: dict[str, Any]) -> dict[str, dict[str, Any]]:
        indexed: dict[str, dict[str, Any]] = {}
        for step in schema.get("steps", []):
            for field in step.get("fields", []):
                indexed[field["fieldKey"]] = field
        return indexed

    def _answer(self, answers: dict[str, Any], key: str, fallback: str) -> str:
        value = str(answers.get(key, "")).strip()
        return value or fallback

    def _sentence_case(self, value: str) -> str:
        if not value:
            return value
        return value[0].upper() + value[1:]

    def _lowercase_first(self, value: str) -> str:
        if not value:
            return value
        return value[0].lower() + value[1:]

    def _clean_fragment(self, value: str) -> str:
        return value.strip().rstrip(".!?;:,")


class ImpactStoryHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], request_handler_class, *, service: ImpactStoryService) -> None:
        super().__init__(server_address, request_handler_class)
        self.service = service


class ImpactStoryRequestHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, directory: str | None = None, **kwargs) -> None:
        super().__init__(*args, directory=str(ROOT_DIR), **kwargs)

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/api/health":
                self._send_json(HTTPStatus.OK, self.server.service.health())
                return
            if parsed.path == "/api/me":
                self._send_json(HTTPStatus.OK, self.server.service.current_user(self.headers))
                return
            if parsed.path == "/api/interviews":
                user = self._require_user()
                scope = str(query.get("scope", ["mine"])[0] or "mine").strip().lower()
                self._send_json(HTTPStatus.OK, self.server.service.list_interviews(scope, user=user))
                return
            if parsed.path.startswith("/api/interviews/"):
                user = self._require_user()
                interview_id = parsed.path.removeprefix("/api/interviews/").strip()
                self._send_json(HTTPStatus.OK, self.server.service.get_interview(interview_id, user=user))
                return
            super().do_GET()
        except AppError as error:
            self._send_json(error.status_code, {"error": error.message})
        except Exception as error:  # pragma: no cover
            log_runtime_message(f"GET {parsed.path} failed: {error}")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Unexpected server error: {error}"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json_body()
            if parsed.path == "/api/auth/login":
                response_payload, session_token = self.server.service.login_manual_invite(payload)
                self._send_json(
                    HTTPStatus.OK,
                    response_payload,
                    extra_headers=[("Set-Cookie", self.server.service.session_cookie_header(session_token))],
                )
                return
            if parsed.path == "/api/auth/logout":
                response_payload = self.server.service.logout_manual_invite(self.headers)
                self._send_json(
                    HTTPStatus.OK,
                    response_payload,
                    extra_headers=[("Set-Cookie", self.server.service.clear_session_cookie_header())],
                )
                return

            user = self._require_user() if parsed.path != "/api/health" else None
            if parsed.path == "/api/interviews":
                self._send_json(HTTPStatus.CREATED, self.server.service.create_interview(payload, user=user))
                return
            if parsed.path.startswith("/api/interviews/") and parsed.path.endswith("/copy"):
                interview_id = parsed.path.removeprefix("/api/interviews/").removesuffix("/copy").strip()
                self._send_json(HTTPStatus.CREATED, self.server.service.copy_interview(interview_id, user=user))
                return
            if parsed.path == "/api/demo/seed-shared-interview":
                self._send_json(HTTPStatus.CREATED, self.server.service.seed_shared_interview(user=user))
                return
            if parsed.path == "/api/analyze-context":
                self._send_json(HTTPStatus.OK, self.server.service.analyze_context(payload, user=user))
                return
            if parsed.path == "/api/generate-story":
                self._send_json(HTTPStatus.OK, self.server.service.generate_story(payload, user=user))
                return
            if parsed.path == "/api/generate-concise-version":
                self._send_json(HTTPStatus.OK, self.server.service.generate_concise_version(payload, user=user))
                return
            if parsed.path == "/api/test-provider":
                self._send_json(HTTPStatus.OK, self.server.service.test_provider(payload, user=user))
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown API endpoint."})
        except AppError as error:
            self._send_json(error.status_code, {"error": error.message})
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be valid JSON."})
        except Exception as error:  # pragma: no cover
            log_runtime_message(f"POST {parsed.path} failed: {error}")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Unexpected server error: {error}"})

    def do_PATCH(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not parsed.path.startswith("/api/interviews/"):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown API endpoint."})
                return
            user = self._require_user()
            payload = self._read_json_body()
            interview_id = parsed.path.removeprefix("/api/interviews/").strip()
            self._send_json(HTTPStatus.OK, self.server.service.update_interview(interview_id, payload, user=user))
        except AppError as error:
            self._send_json(error.status_code, {"error": error.message})
        except json.JSONDecodeError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Request body must be valid JSON."})
        except Exception as error:  # pragma: no cover
            log_runtime_message(f"PATCH {parsed.path} failed: {error}")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Unexpected server error: {error}"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            if not parsed.path.startswith("/api/interviews/"):
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Unknown API endpoint."})
                return
            user = self._require_user()
            interview_id = parsed.path.removeprefix("/api/interviews/").strip()
            self._send_json(HTTPStatus.OK, self.server.service.delete_interview(interview_id, user=user))
        except AppError as error:
            self._send_json(error.status_code, {"error": error.message})
        except Exception as error:  # pragma: no cover
            log_runtime_message(f"DELETE {parsed.path} failed: {error}")
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"Unexpected server error: {error}"})

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        log_runtime_message(format % args)

    def log_error(self, format: str, *args) -> None:  # noqa: A003
        log_runtime_message(f"ERROR: {format % args}")

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw_body = self.rfile.read(content_length) if content_length else b"{}"
        if not raw_body:
            return {}
        return json.loads(raw_body.decode("utf-8"))

    def _require_user(self) -> AuthenticatedUser:
        return self.server.service._authenticate(self.headers)

    def _send_json(
        self,
        status_code: int,
        payload: dict[str, Any],
        *,
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for header_name, header_value in extra_headers or []:
            self.send_header(header_name, header_value)
        self.end_headers()
        self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Impact Story Builder local web server.")
    parser.add_argument("--host", default=os.getenv("HOST", default_server_host()))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "4173")))
    return parser.parse_args()


def load_runtime_environment() -> None:
    load_dotenv(RUNTIME_ROOT / ".env")
    if RUNTIME_ROOT != ROOT_DIR:
        load_dotenv(ROOT_DIR / ".env")


def create_server(host: str, port: int) -> ImpactStoryHTTPServer:
    load_runtime_environment()
    config = AppConfig.from_env(host=host, port=port)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    service = ImpactStoryService(config, schema)
    return ImpactStoryHTTPServer((config.host, config.port), ImpactStoryRequestHandler, service=service)


def main() -> None:
    args = parse_args()
    server = create_server(args.host, args.port)
    config = server.service.config
    print(f"Serving Impact Story Builder on http://{config.host}:{config.port}")
    print("AI provider mode is selected from the local UI panel.")
    print(f"Auth mode: {config.auth_mode}")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
