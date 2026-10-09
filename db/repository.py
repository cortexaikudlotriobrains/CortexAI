"""
Repository layer for CortexAI database operations.
All CRUD functions using SQLAlchemy Core with reflected tables.

Design principles:
- Functions do NOT commit - caller commits for transaction control
- Returns None or raises exceptions on errors
- Uses SQLAlchemy Core (insert/select/update) not ORM
"""

import hashlib
import json
import zlib
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_EVEN, localcontext
from typing import Any, Mapping
from uuid import UUID, uuid4

from sqlalchemy import (
    String,
    and_,
    case,
    cast,
    delete,
    desc,
    func,
    insert,
    literal,
    or_,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert  # For UPSERT
from sqlalchemy.exc import NoSuchTableError
from sqlalchemy.orm import Session

# Import logger
from utils.api_key_utils import (
    compute_api_key_hash as _compute_api_key_hash,
    generate_api_key as _generate_api_key,
)
from utils.logger import get_logger

logger = get_logger(__name__)

# Import UnifiedResponse for type hints
try:
    from models.unified_response import UnifiedResponse
except ImportError:
    UnifiedResponse = Any  # Fallback if import fails

ALLOWED_PROMPT_CATEGORIES = {
    "coding",
    "financial",
    "educational",
    "math",
    "legal",
    "data_technical",
    "general",
    "unknown",
}

ALLOWED_RESEARCH_MODES = {"off", "auto", "on"}
ALLOWED_ROUTING_MODES = {"smart", "cheap", "strong", "explicit", "legacy"}
ALLOWED_CACHE_REUSE_OPERATIONS = {
    "research",
    "prompt_optimization",
    "cortex_analysis",
}
ALLOWED_FILE_STATUSES = {
    "uploading",
    "uploaded",
    "processing",
    "ready",
    "failed",
    "expired",
    "deleting",
    "deleted",
}
ALLOWED_ATTACHMENT_USAGE_ROLES = {"primary", "reference"}
ALLOWED_ATTACHMENT_TRANSFORM_MODES = {"auto", "text_only", "vision_pages", "table_summary"}
ALLOWED_FILE_DELETION_JOB_STATUSES = {"pending", "in_progress", "retry", "succeeded", "failed"}
SERVICE_AUTH_PROVIDER = "service"
SERVICE_AUTH_SUBJECT = "api-service"
SERVICE_AUTH_ISSUER = "cortexai"


class CortexAnalysisSchemaUnavailable(RuntimeError):
    """Raised when the Cortex Analysis persistence migration is not available."""


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================


def compute_prompt_sha256(prompt: str) -> str:
    """
    Compute SHA-256 hash of prompt for deduplication/indexing.

    Args:
        prompt: The user's prompt text

    Returns:
        str: Hexadecimal SHA-256 hash
    """
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def compute_context_hash(context: str) -> str:
    """
    Compute SHA-256 hash of context for deduplication.

    Args:
        context: The context text

    Returns:
        str: Hexadecimal SHA-256 hash
    """
    return hashlib.sha256(context.encode("utf-8")).hexdigest()


def compute_api_key_hash(api_key: str) -> str:
    """
    Compute SHA-256 hash of API key for secure comparison.

    NOTE: Check server/dependencies.py for existing hash function.
    If auth already uses a different hashing method (bcrypt, argon2, etc.),
    import and use that function instead.

    Args:
        api_key: Plaintext API key (from X-API-Key header)

    Returns:
        str: Hexadecimal SHA-256 hash
    """
    return _compute_api_key_hash(api_key)


def generate_api_key(prefix: str = "cortex") -> str:
    """
    Generate a random API key string.

    Args:
        prefix: Key prefix label

    Returns:
        str: API key (raw, return-once secret)
    """
    return _generate_api_key(prefix)


def coerce_uuid(value: str | None) -> UUID | None:
    """Parse a UUID string safely."""
    if not value:
        return None
    try:
        return UUID(str(value))
    except Exception:
        return None


# ============================================================================
# USER & API KEY MANAGEMENT
# ============================================================================


def get_or_create_cli_user(
    db: Session, email: str = "cli@cortexai.local", display_name: str = "CLI User"
) -> UUID:
    """
    Get or create a default CLI user for local testing.

    This is a temporary solution for CLI usage. When authentication is ready,
    use get_user_by_api_key() or JWT-based user resolution instead.

    Args:
        db: Database session
        email: User email (default: cli@cortexai.local)
        display_name: Display name (default: CLI User)

    Returns:
        UUID: user_id

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    users = get_table("users")

    # Check if CLI user exists
    stmt = select(users.c.id).where(users.c.email == email)
    user_id = db.execute(stmt).scalar_one_or_none()

    if user_id:
        return user_id

    # Create CLI user
    stmt = (
        insert(users)
        .values(
            email=email,
            display_name=display_name,
            is_active=True,
            auth_provider="cli",
            auth_subject="local-cli",
            auth_issuer="cortexai",
        )
        .returning(users.c.id)
    )

    user_id = db.execute(stmt).scalar_one()

    logger.info(f"Created CLI user: {email} (id: {user_id})")

    return user_id


def get_or_create_service_user(
    db: Session, email: str = "api@cortexai.local", display_name: str = "API Service User"
) -> UUID:
    """
    Get or create deterministic service user for API fallback/autoreg paths.

    Uses a dedicated auth identity that cannot collide with CLI identity:
    - auth_provider=service
    - auth_subject=api-service
    - auth_issuer=cortexai

    Args:
        db: Database session
        email: Service user email
        display_name: Service user display name

    Returns:
        UUID: user_id

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    users = get_table("users")

    # Identity-first lookup guarantees deterministic resolution.
    by_identity_stmt = select(users.c.id).where(
        and_(
            users.c.auth_provider == SERVICE_AUTH_PROVIDER,
            users.c.auth_subject == SERVICE_AUTH_SUBJECT,
            users.c.auth_issuer == SERVICE_AUTH_ISSUER,
        )
    )
    service_user_id = db.execute(by_identity_stmt).scalar_one_or_none()
    if service_user_id:
        return service_user_id

    # Protect against accidental email reuse by a non-service principal.
    by_email_stmt = select(
        users.c.id,
        users.c.auth_provider,
        users.c.auth_subject,
        users.c.auth_issuer,
    ).where(users.c.email == email)
    existing_email_user = db.execute(by_email_stmt).first()
    if existing_email_user:
        existing_id, provider, subject, issuer = existing_email_user
        if (
            provider == SERVICE_AUTH_PROVIDER
            and subject == SERVICE_AUTH_SUBJECT
            and issuer == SERVICE_AUTH_ISSUER
        ):
            return existing_id
        raise ValueError(
            "Configured API fallback user email is already used by a non-service identity. "
            f"email={email}"
        )

    stmt = (
        insert(users)
        .values(
            email=email,
            display_name=display_name,
            is_active=True,
            auth_provider=SERVICE_AUTH_PROVIDER,
            auth_subject=SERVICE_AUTH_SUBJECT,
            auth_issuer=SERVICE_AUTH_ISSUER,
        )
        .returning(users.c.id)
    )

    service_user_id = db.execute(stmt).scalar_one()
    logger.info(f"Created service user: {email} (id: {service_user_id})")
    return service_user_id


COGNITO_AUTH_PROVIDER = "cognito"


def get_or_create_user_by_cognito(
    db: Session,
    *,
    sub: str,
    email: str | None = None,
    issuer: str,
) -> UUID:
    """
    Get or create a user identified by Cognito (e.g. Google sign-in).

    Uses auth_provider='cognito', auth_subject=sub, auth_issuer=issuer.
    Email is stored for display; sub is the unique identity.

    Args:
        db: Database session
        sub: Cognito subject (unique per user in pool)
        email: Email from token (optional)
        issuer: Token issuer (Cognito user pool URL)

    Returns:
        UUID: user_id

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    users = get_table("users")

    stmt = select(users.c.id).where(
        and_(
            users.c.auth_provider == COGNITO_AUTH_PROVIDER,
            users.c.auth_subject == sub,
            users.c.auth_issuer == issuer,
        )
    )
    user_id = db.execute(stmt).scalar_one_or_none()
    if user_id:
        return user_id

    display_name = (email or sub).split("@")[0] if email else sub[:16]
    stmt = (
        insert(users)
        .values(
            email=email,
            display_name=display_name,
            is_active=True,
            auth_provider=COGNITO_AUTH_PROVIDER,
            auth_subject=sub,
            auth_issuer=issuer,
        )
        .returning(users.c.id)
    )
    user_id = db.execute(stmt).scalar_one()
    logger.info(f"Created Cognito user: sub={sub[:12]}... (id: {user_id})")
    return user_id


def get_user_by_api_key(db: Session, api_key: str) -> tuple[UUID, UUID] | None:
    """
    Look up user_id AND api_key_id from API key via api_keys table.

    CRITICAL: Returns BOTH user_id and api_key_id for proper attribution.
    - user_id: Used for sessions, usage enforcement, ownership
    - api_key_id: Stored in llm_requests for audit trail (which key was used)

    IMPORTANT: Check server/dependencies.py for existing API key verification.
    If it already has a hash function, use that instead of compute_api_key_hash().

    Args:
        db: Database session
        api_key: Plaintext API key from X-API-Key header

    Returns:
        tuple[UUID, UUID]: (user_id, api_key_id) if found, None otherwise
    """
    from db.tables import get_table

    api_keys = get_table("api_keys")

    # Hash the API key (adjust if using different hash method)
    key_hash = compute_api_key_hash(api_key)

    # Look up BOTH user_id and api_key_id
    stmt = select(api_keys.c.user_id, api_keys.c.id).where(
        and_(api_keys.c.key_hash == key_hash, api_keys.c.is_active)
    )
    result = db.execute(stmt).first()

    if result:
        user_id, api_key_id = result
        logger.debug(f"API key matched to user_id: {user_id}, api_key_id: {api_key_id}")
        return (user_id, api_key_id)
    else:
        logger.warning(f"API key not found or inactive: {key_hash[:8]}...")
        return None


def update_api_key_last_used(db: Session, api_key: str) -> None:
    """
    Update api_keys.last_used_at timestamp.

    Args:
        db: Database session
        api_key: Plaintext API key

    Note:
        Does NOT commit. Fails silently if key not found.
    """
    from db.tables import get_table

    api_keys = get_table("api_keys")

    key_hash = compute_api_key_hash(api_key)

    stmt = update(api_keys).where(api_keys.c.key_hash == key_hash).values(last_used_at=func.now())

    db.execute(stmt)


# ============================================================================
# SESSION MANAGEMENT
# ============================================================================


def create_session(
    db: Session,
    user_id: UUID,
    mode: str = "ask",
    title: str | None = None,
    session_id: UUID | None = None,
) -> UUID:
    """
    Create a new chat session.

    Args:
        db: Database session
        user_id: User ID
        mode: Session mode ('ask', 'compare', 'eval', 'research')
        title: Optional session title
        session_id: Optional explicit session UUID

    Returns:
        UUID: session_id

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    sessions = get_table("sessions")

    values: dict[str, Any] = {
        "user_id": user_id,
        "title": title,
        "mode": mode,
    }
    if session_id is not None and "id" in {col.name for col in sessions.columns}:
        values["id"] = session_id

    stmt = insert(sessions).values(**values).returning(sessions.c.id)

    session_id = db.execute(stmt).scalar_one()

    logger.info(f"Created session: {session_id} for user: {user_id}, mode: {mode}")

    return session_id


def get_active_session(db: Session, user_id: UUID, mode: str | None = "ask") -> UUID | None:
    """
    Get user's most recent active session.

    Args:
        db: Database session
        user_id: User ID
        mode: Optional session mode filter. Pass None to search across all modes.

    Returns:
        UUID: session_id if found, None otherwise
    """
    from db.tables import get_table

    sessions = get_table("sessions")

    conditions = [sessions.c.user_id == user_id]
    if mode is not None:
        conditions.append(sessions.c.mode == mode)

    stmt = (
        select(sessions.c.id)
        .where(and_(*conditions))
        .order_by(desc(sessions.c.updated_at))
        .limit(1)
    )

    return db.execute(stmt).scalar_one_or_none()


def get_session_by_id(db: Session, session_id: UUID) -> dict[str, Any] | None:
    """
    Get session metadata by ID.

    Args:
        db: Database session
        session_id: Session ID

    Returns:
        dict: Session data or None if not found
    """
    from db.tables import get_table

    sessions = get_table("sessions")

    stmt = select(sessions).where(sessions.c.id == session_id)
    result = db.execute(stmt).first()

    if result:
        return dict(result._mapping)
    return None


def verify_session_belongs_to_user(db: Session, session_id: UUID, user_id: UUID) -> bool:
    """
    Verify that a session belongs to a user.

    Args:
        db: Database session
        session_id: Session ID
        user_id: User ID

    Returns:
        bool: True if session belongs to user, False otherwise
    """
    from db.tables import get_table

    sessions = get_table("sessions")

    stmt = select(sessions.c.id).where(
        and_(sessions.c.id == session_id, sessions.c.user_id == user_id)
    )

    result = db.execute(stmt).scalar_one_or_none()
    return result is not None


def update_session_timestamp(db: Session, session_id: UUID | str) -> None:
    """
    Update sessions.updated_at to current timestamp.

    Args:
        db: Database session
        session_id: Session ID

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    sessions = get_table("sessions")

    stmt = update(sessions).where(sessions.c.id == session_id).values(updated_at=func.now())

    db.execute(stmt)


# ============================================================================
# MESSAGE MANAGEMENT
# ============================================================================


def save_message(db: Session, session_id: UUID, role: str, content: str) -> UUID:
    """
    Insert a message into the messages table.

    Args:
        db: Database session
        session_id: Session ID
        role: Message role ('user' or 'assistant')
        content: Message content

    Returns:
        UUID: message_id

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    messages = get_table("messages")

    stmt = (
        insert(messages)
        .values(session_id=session_id, role=role, content=content)
        .returning(messages.c.id)
    )

    message_id = db.execute(stmt).scalar_one()

    logger.debug(f"Saved {role} message to session {session_id}: {message_id}")

    return message_id


def get_session_messages(
    db: Session, session_id: UUID, limit: int = 10, offset: int = 0
) -> list[dict[str, Any]]:
    """
    Get messages for a session (for building context).

    Args:
        db: Database session
        session_id: Session ID
        limit: Maximum number of messages to return (default: 10)
        offset: Number of messages to skip (default: 0)

    Returns:
        list: List of message dicts, ordered by created_at ASC

    Note:
        Returns messages in chronological order (oldest first) for context building.
    """
    from db.tables import get_table

    messages = get_table("messages")

    stmt = (
        select(messages)
        .where(messages.c.session_id == session_id)
        .order_by(desc(messages.c.created_at))
        .limit(limit)
        .offset(offset)
    )

    results = db.execute(stmt).fetchall()

    # Reverse to get chronological order (oldest first)
    messages_list = [dict(row._mapping) for row in reversed(results)]

    return messages_list


# ============================================================================
# CONTEXT SNAPSHOTS
# ============================================================================


def get_latest_context_snapshot(db: Session, session_id: UUID) -> dict[str, Any] | None:
    """
    Get the most recent context snapshot for a session.

    Args:
        db: Database session
        session_id: Session ID

    Returns:
        dict: Snapshot data or None if no snapshot exists
    """
    from db.tables import get_table

    context_snapshots = get_table("context_snapshots")

    stmt = (
        select(context_snapshots)
        .where(context_snapshots.c.session_id == session_id)
        .order_by(desc(context_snapshots.c.created_at))
        .limit(1)
    )

    result = db.execute(stmt).first()

    if result:
        return dict(result._mapping)
    return None


def create_context_snapshot(
    db: Session,
    user_id: UUID,
    session_id: UUID,
    context_text: str,
    base_message_id: UUID | None = None,
    *,
    source_message_range: str | None = None,
    source_hash: str | None = None,
    summary_policy_version: str | None = None,
) -> UUID:
    """
    Create a new context snapshot (with hash-based deduplication).

    Args:
        db: Database session
        user_id: User ID
        session_id: Session ID
        context_text: The full context text
        base_message_id: Optional ID of the message this context is based on

    Returns:
        UUID: snapshot_id

    Note:
        Does NOT commit. Caller must commit.
        Uses ON CONFLICT DO NOTHING for hash-based deduplication.
    """
    from db.tables import get_table

    context_snapshots = get_table("context_snapshots")

    context_hash = compute_context_hash(
        ":".join(
            part
            for part in (
                source_hash,
                source_message_range,
                summary_policy_version,
            )
            if part
        )
        or context_text
    )
    columns = set(context_snapshots.c.keys())
    values: dict[str, Any] = {
        "user_id": user_id,
        "session_id": session_id,
        "base_message_id": base_message_id,
        "context_hash": context_hash,
        "context_text": context_text,
    }
    if "source_message_range" in columns:
        values["source_message_range"] = source_message_range
    if "source_hash" in columns:
        values["source_hash"] = source_hash
    if "summary_policy_version" in columns:
        values["summary_policy_version"] = summary_policy_version

    # Use PostgreSQL UPSERT to handle duplicate hash
    stmt = (
        pg_insert(context_snapshots)
        .values(**values)
        .on_conflict_do_nothing(index_elements=["session_id", "context_hash"])
        .returning(context_snapshots.c.id)
    )

    result = db.execute(stmt).scalar_one_or_none()

    if result:
        logger.debug(f"Created context snapshot: {result}")
        return result
    else:
        # Snapshot with same hash already exists, fetch it
        stmt = select(context_snapshots.c.id).where(
            and_(
                context_snapshots.c.session_id == session_id,
                context_snapshots.c.context_hash == context_hash,
            )
        )
        existing_id = db.execute(stmt).scalar_one()
        logger.debug(f"Context snapshot already exists: {existing_id}")
        return existing_id


# ============================================================================
# ATTACHMENT FILES
# ============================================================================


def _normalize_jsonb_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize optional JSON payloads to dict for JSONB columns."""
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("JSON payload must be a dict")
    return payload


def create_uploaded_file(
    db: Session,
    *,
    file_id: UUID | None = None,
    user_id: UUID,
    original_filename: str,
    mime_type: str,
    size_bytes: int,
    sha256: str | None,
    storage_bucket: str,
    storage_key: str,
    api_key_id: UUID | None = None,
    status: str = "uploaded",
    ingestion_meta: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> UUID:
    """
    Insert a file metadata row into uploaded_files.

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    if status not in ALLOWED_FILE_STATUSES:
        raise ValueError(f"Invalid uploaded file status: {status}")
    if int(size_bytes) <= 0:
        raise ValueError("size_bytes must be > 0")

    normalized_sha = str(sha256 or "").strip().lower() or None

    uploaded_files = get_table("uploaded_files")
    values: dict[str, Any] = {
        "user_id": user_id,
        "api_key_id": api_key_id,
        "original_filename": str(original_filename or "").strip() or "file",
        "mime_type": str(mime_type or "").strip().lower(),
        "size_bytes": int(size_bytes),
        "sha256": normalized_sha,
        "storage_bucket": str(storage_bucket or "").strip(),
        "storage_key": str(storage_key or "").strip(),
        "status": status,
        "ingestion_meta": _normalize_jsonb_payload(ingestion_meta),
        "expires_at": expires_at,
    }
    if file_id is not None:
        values["id"] = file_id

    if not values["mime_type"]:
        raise ValueError("mime_type is required")
    if not values["storage_bucket"] or not values["storage_key"]:
        raise ValueError("storage_bucket and storage_key are required")

    stmt = insert(uploaded_files).values(**values).returning(uploaded_files.c.id)
    uploaded_file_id = db.execute(stmt).scalar_one()
    logger.debug("Created uploaded_file %s for user %s", uploaded_file_id, user_id)
    return uploaded_file_id


def find_active_uploaded_file_by_hash(
    db: Session,
    *,
    user_id: UUID,
    sha256: str,
    size_bytes: int,
) -> dict[str, Any] | None:
    """
    Lookup an active uploaded file for same-user dedup by hash + size.
    """
    from db.tables import get_table

    uploaded_files = get_table("uploaded_files")
    normalized_sha = str(sha256 or "").strip().lower()
    if not normalized_sha or int(size_bytes) <= 0:
        return None

    conditions = [
        uploaded_files.c.user_id == user_id,
        uploaded_files.c.sha256 == normalized_sha,
        uploaded_files.c.size_bytes == int(size_bytes),
    ]
    column_names = {col.name for col in uploaded_files.columns}
    if "deleted_at" in column_names:
        conditions.append(uploaded_files.c.deleted_at.is_(None))

    stmt = (
        select(uploaded_files)
        .where(and_(*conditions))
        .order_by(desc(uploaded_files.c.created_at))
        .limit(1)
    )
    row = db.execute(stmt).first()
    if not row:
        return None
    return dict(row._mapping)


def get_uploaded_file_for_user(
    db: Session,
    *,
    user_id: UUID,
    file_id: UUID,
    include_deleted: bool = False,
) -> dict[str, Any] | None:
    """
    Fetch one uploaded file row scoped by user ownership.
    """
    from db.tables import get_table

    uploaded_files = get_table("uploaded_files")
    conditions = [
        uploaded_files.c.id == file_id,
        uploaded_files.c.user_id == user_id,
    ]
    column_names = {col.name for col in uploaded_files.columns}
    if "deleted_at" in column_names and not include_deleted:
        conditions.append(uploaded_files.c.deleted_at.is_(None))

    stmt = select(uploaded_files).where(and_(*conditions)).limit(1)
    row = db.execute(stmt).first()
    if not row:
        return None
    return dict(row._mapping)


def get_uploaded_file_by_id(
    db: Session,
    *,
    file_id: UUID,
    include_deleted: bool = True,
) -> dict[str, Any] | None:
    """
    Fetch one uploaded file row by ID.

    This helper is intended for internal background jobs (cleanup/retention)
    where user scoping is already handled at the caller boundary.
    """
    from db.tables import get_table

    uploaded_files = get_table("uploaded_files")
    conditions = [uploaded_files.c.id == file_id]
    column_names = {col.name for col in uploaded_files.columns}
    if "deleted_at" in column_names and not include_deleted:
        conditions.append(uploaded_files.c.deleted_at.is_(None))

    stmt = select(uploaded_files).where(and_(*conditions)).limit(1)
    row = db.execute(stmt).first()
    if not row:
        return None
    return dict(row._mapping)


def list_expired_uploaded_files(
    db: Session,
    *,
    now_utc: datetime | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """
    List uploaded files whose TTL has elapsed and are not yet deleted.
    """
    from db.tables import get_table

    uploaded_files = get_table("uploaded_files")
    cap = max(1, min(int(limit), 5000))
    expiry_cutoff = now_utc if now_utc is not None else func.now()

    conditions = [
        uploaded_files.c.expires_at.is_not(None),
        uploaded_files.c.expires_at <= expiry_cutoff,
    ]
    column_names = {col.name for col in uploaded_files.columns}
    if "deleted_at" in column_names:
        conditions.append(uploaded_files.c.deleted_at.is_(None))
    if "status" in column_names:
        conditions.append(uploaded_files.c.status != "deleted")

    stmt = (
        select(uploaded_files)
        .where(and_(*conditions))
        .order_by(uploaded_files.c.expires_at.asc(), uploaded_files.c.created_at.asc())
        .limit(cap)
    )
    rows = db.execute(stmt).fetchall()
    return [dict(row._mapping) for row in rows]


def update_uploaded_file_status(
    db: Session,
    *,
    file_id: UUID,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    ingestion_meta: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> bool:
    """
    Update uploaded file status and optional metadata fields.
    """
    from db.tables import get_table

    if status not in ALLOWED_FILE_STATUSES:
        raise ValueError(f"Invalid uploaded file status: {status}")

    uploaded_files = get_table("uploaded_files")
    values: dict[str, Any] = {"status": status}
    column_names = {col.name for col in uploaded_files.columns}

    if "updated_at" in column_names:
        values["updated_at"] = func.now()
    if "error_code" in column_names:
        values["error_code"] = error_code
    if "error_message" in column_names:
        values["error_message"] = error_message
    if "ingestion_meta" in column_names and ingestion_meta is not None:
        values["ingestion_meta"] = _normalize_jsonb_payload(ingestion_meta)
    if "expires_at" in column_names and expires_at is not None:
        values["expires_at"] = expires_at
    if "deleted_at" in column_names and deleted_at is not None:
        values["deleted_at"] = deleted_at

    stmt = update(uploaded_files).where(uploaded_files.c.id == file_id).values(**values)
    result = db.execute(stmt)
    return bool(result.rowcount and result.rowcount > 0)


def create_request_attachment(
    db: Session,
    *,
    llm_request_id: UUID,
    file_id: UUID,
    order_index: int,
    usage_role: str = "primary",
    transform_mode: str = "auto",
    resolved_artifact_meta: dict[str, Any] | None = None,
) -> UUID:
    """
    Link a persisted llm_request row to one uploaded file.

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    if usage_role not in ALLOWED_ATTACHMENT_USAGE_ROLES:
        raise ValueError(f"Invalid attachment usage_role: {usage_role}")
    if transform_mode not in ALLOWED_ATTACHMENT_TRANSFORM_MODES:
        raise ValueError(f"Invalid attachment transform_mode: {transform_mode}")
    if int(order_index) < 0:
        raise ValueError("order_index must be >= 0")

    request_attachments = get_table("request_attachments")
    stmt = (
        insert(request_attachments)
        .values(
            llm_request_id=llm_request_id,
            file_id=file_id,
            order_index=int(order_index),
            usage_role=usage_role,
            transform_mode=transform_mode,
            resolved_artifact_meta=_normalize_jsonb_payload(resolved_artifact_meta),
        )
        .returning(request_attachments.c.id)
    )

    attachment_id = db.execute(stmt).scalar_one()
    logger.debug(
        "Created request_attachment %s (request=%s, file=%s)",
        attachment_id,
        llm_request_id,
        file_id,
    )
    return attachment_id


def list_request_attachments_for_request(
    db: Session,
    *,
    llm_request_id: UUID,
) -> list[dict[str, Any]]:
    """
    List request attachment rows for one llm_request in deterministic order.
    """
    from db.tables import get_table

    request_attachments = get_table("request_attachments")
    stmt = (
        select(request_attachments)
        .where(request_attachments.c.llm_request_id == llm_request_id)
        .order_by(request_attachments.c.order_index.asc())
    )
    rows = db.execute(stmt).fetchall()
    return [dict(row._mapping) for row in rows]


def enqueue_file_deletion_job(
    db: Session,
    *,
    file_id: UUID,
    available_at: datetime | None = None,
    status: str = "pending",
    last_error: str | None = None,
) -> UUID:
    """
    Create (or reuse) an active file deletion queue job for one file.
    """
    from db.tables import get_table

    if status not in ALLOWED_FILE_DELETION_JOB_STATUSES:
        raise ValueError(f"Invalid file deletion job status: {status}")

    file_deletion_queue = get_table("file_deletion_queue")
    existing_stmt = (
        select(file_deletion_queue.c.id)
        .where(
            and_(
                file_deletion_queue.c.file_id == file_id,
                file_deletion_queue.c.status.in_(("pending", "in_progress", "retry")),
            )
        )
        .order_by(desc(file_deletion_queue.c.created_at))
        .limit(1)
    )
    existing_job_id = db.execute(existing_stmt).scalar_one_or_none()
    if existing_job_id:
        return existing_job_id

    values: dict[str, Any] = {
        "file_id": file_id,
        "status": status,
        "last_error": last_error,
    }
    column_names = {col.name for col in file_deletion_queue.columns}
    if "available_at" in column_names and available_at is not None:
        values["available_at"] = available_at
    if "updated_at" in column_names:
        values["updated_at"] = func.now()

    stmt = insert(file_deletion_queue).values(**values).returning(file_deletion_queue.c.id)
    return db.execute(stmt).scalar_one()


def list_due_file_deletion_jobs(
    db: Session,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    List deletion jobs ready for execution.
    """
    from db.tables import get_table

    file_deletion_queue = get_table("file_deletion_queue")
    cap = max(1, min(int(limit), 1000))
    stmt = (
        select(file_deletion_queue)
        .where(
            and_(
                file_deletion_queue.c.status.in_(("pending", "retry")),
                file_deletion_queue.c.available_at <= func.now(),
            )
        )
        .order_by(file_deletion_queue.c.available_at.asc(), file_deletion_queue.c.created_at.asc())
        .limit(cap)
    )
    rows = db.execute(stmt).fetchall()
    return [dict(row._mapping) for row in rows]


def update_file_deletion_job(
    db: Session,
    *,
    job_id: UUID,
    status: str,
    attempt_count: int | None = None,
    last_error: str | None = None,
    available_at: datetime | None = None,
    locked_at: datetime | None = None,
) -> bool:
    """
    Update deletion queue job status and retry metadata.
    """
    from db.tables import get_table

    if status not in ALLOWED_FILE_DELETION_JOB_STATUSES:
        raise ValueError(f"Invalid file deletion job status: {status}")
    if attempt_count is not None and int(attempt_count) < 0:
        raise ValueError("attempt_count must be >= 0")

    file_deletion_queue = get_table("file_deletion_queue")
    values: dict[str, Any] = {"status": status}
    column_names = {col.name for col in file_deletion_queue.columns}

    if attempt_count is not None and "attempt_count" in column_names:
        values["attempt_count"] = int(attempt_count)
    if "last_error" in column_names:
        values["last_error"] = last_error
    if available_at is not None and "available_at" in column_names:
        values["available_at"] = available_at
    if locked_at is not None and "locked_at" in column_names:
        values["locked_at"] = locked_at
    if "updated_at" in column_names:
        values["updated_at"] = func.now()

    stmt = update(file_deletion_queue).where(file_deletion_queue.c.id == job_id).values(**values)
    result = db.execute(stmt)
    return bool(result.rowcount and result.rowcount > 0)


# ============================================================================
# LLM AUDIT LOGGING
# ============================================================================


def create_llm_request(
    db: Session,
    user_id: UUID,
    request_id: str,
    route_mode: str,
    provider: str,
    model: str,
    prompt: str,
    session_id: UUID | str | None = None,
    request_group_id: UUID | None = None,
    api_key_id: UUID | None = None,
    input_tokens_est: int | None = None,
    store_prompt: bool = False,
    prompt_text_override: str | None = None,
    response_revision_root_id: UUID | None = None,
    response_revision: int = 1,
    requested_model: str | None = None,
    generation_budget: Mapping[str, Any] | None = None,
) -> UUID:
    """
    Insert a row into llm_requests table.

    Args:
        db: Database session
        user_id: User ID
        request_id: Unique request ID (from UnifiedResponse)
        route_mode: Route mode ('ask', 'compare', 'eval', 'research')
        provider: LLM provider name (e.g., 'openai')
        model: Model name (e.g., 'gpt-4')
        prompt: User's prompt text
        session_id: Optional session ID
        request_group_id: Optional grouping UUID for multi-response flows (e.g., compare mode)
        api_key_id: Optional API key ID (if request via API)
        input_tokens_est: Optional estimated input tokens
        store_prompt: Whether to store prompt text
        prompt_text_override: Optional prompt text value to store when store_prompt=True
        response_revision_root_id: Original Compare request row when this is a regeneration
        response_revision: Monotonic version for one logical Compare response slot

    Returns:
        UUID: The llm_requests.id of the inserted row

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    llm_requests = get_table("llm_requests")

    prompt_sha256 = compute_prompt_sha256(prompt)
    values: dict[str, Any] = {
        "user_id": user_id,
        "session_id": session_id,
        "request_id": request_id,
        "route_mode": route_mode,
        "provider": provider,
        "model": model,
        "prompt_sha256": prompt_sha256,
        "prompt_stored": store_prompt,
        "prompt_text": (
            prompt_text_override
            if (store_prompt and prompt_text_override is not None)
            else (prompt if store_prompt else None)
        ),
        "input_tokens_est": input_tokens_est,
        "api_key_id": api_key_id,
    }
    column_names = {col.name for col in llm_requests.columns}
    if "requested_model" in column_names:
        values["requested_model"] = str(requested_model or model)
    if "request_group_id" in column_names:
        group_column = llm_requests.c.request_group_id
        values["request_group_id"] = (
            request_group_id.hex
            if isinstance(request_group_id, UUID) and isinstance(group_column.type, String)
            else request_group_id
        )
    if "response_revision_root_id" in column_names:
        values["response_revision_root_id"] = response_revision_root_id
    if "response_revision" in column_names:
        values["response_revision"] = max(1, int(response_revision))
    budget = dict(generation_budget or {})
    budget_values = {
        "generation_profile": budget.get("profile"),
        "requested_max_output_tokens": budget.get("requested_max_output_tokens"),
        "effective_max_output_tokens": budget.get("effective_max_output_tokens"),
        "requested_reasoning_mode": budget.get("requested_reasoning_mode"),
        "effective_reasoning_mode": budget.get("effective_reasoning_mode"),
        "requested_reasoning_effort": budget.get("requested_reasoning_effort"),
        "effective_reasoning_effort": budget.get("effective_reasoning_effort"),
        "generation_policy_version": budget.get("policy_version"),
    }
    values.update(
        {key: value for key, value in budget_values.items() if key in column_names}
    )

    stmt = insert(llm_requests).values(**values).returning(llm_requests.c.id)

    llm_request_id = db.execute(stmt).scalar_one()

    logger.debug(
        f"Created llm_request: {llm_request_id} "
        f"(request_id: {request_id}, provider: {provider}, model: {model})"
    )

    return llm_request_id


def create_llm_response(db: Session, llm_request_id: UUID, response: UnifiedResponse) -> None:
    """
    Insert a row into llm_responses table from UnifiedResponse.

    Args:
        db: Database session
        llm_request_id: Foreign key to llm_requests.id
        response: UnifiedResponse object from orchestrator

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    llm_responses = get_table("llm_responses")

    # Extract error info if present
    error_type = None
    error_message = None
    if hasattr(response, "error") and response.error:
        error_type = response.error.code
        error_message = response.error.message

    values: dict[str, Any] = {
        "llm_request_id": llm_request_id,
        "text": response.text,
        "finish_reason": response.finish_reason,
        "latency_ms": response.latency_ms,
        "prompt_tokens": response.token_usage.prompt_tokens,
        "completion_tokens": response.token_usage.completion_tokens,
        "total_tokens": response.token_usage.total_tokens,
        "estimated_cost": response.estimated_cost,
        "error_type": error_type,
        "error_message": error_message,
    }
    column_names = {column.name for column in llm_responses.columns}
    audit_values: dict[str, Any] = {
        "served_model": response.served_model or response.model,
        "pricing_model": response.pricing_model or response.served_model or response.model,
        "model_lifecycle_status": response.model_lifecycle_status,
        "alias_redirected": response.alias_redirected,
        "replacement_model": response.replacement_model,
        "model_migration_reason": response.migration_reason,
        "reasoning_mode": response.reasoning_mode,
        "cached_input_tokens": response.token_usage.cached_input_tokens,
        "cache_write_tokens": response.token_usage.cache_write_tokens,
        "reasoning_tokens": response.token_usage.reasoning_tokens,
        "pricing_rule_applied": response.pricing_rule_applied,
        "pricing_version": response.pricing_version,
        "pricing_unknown": response.pricing_unknown,
        "pricing_snapshot": dict(response.pricing_snapshot or {}),
        "rate_card_id": (response.pricing_snapshot or {}).get("rate_card_id"),
        "usage_calculated_provider_cost_usd": (response.pricing_snapshot or {}).get(
            "usage_calculated_provider_cost_usd"
        ),
        "completion_status": (response.metadata or {}).get("completion_status", "complete"),
        "stop_cause": (response.metadata or {}).get("stop_cause", "unknown"),
    }
    if audit_values["usage_calculated_provider_cost_usd"] is not None:
        with localcontext() as context:
            context.prec = 40
            audit_values["usage_calculated_provider_cost_usd"] = Decimal(
                str(audit_values["usage_calculated_provider_cost_usd"])
            ).quantize(Decimal("0.000000000000001"), rounding=ROUND_HALF_EVEN)
    values.update({key: value for key, value in audit_values.items() if key in column_names})

    stmt = insert(llm_responses).values(**values)

    db.execute(stmt)


def _build_api_key_insert_values(
    api_keys_table,
    *,
    user_id: UUID,
    key_hash: str,
    raw_api_key: str,
    label: str | None,
) -> dict[str, Any]:
    """
    Build insert payload for api_keys table while supporting schema variants.
    """
    values: dict[str, Any] = {"user_id": user_id, "key_hash": key_hash}
    column_names = {col.name for col in api_keys_table.columns}

    if "label" in column_names and label is not None:
        values["label"] = label
    if "name" in column_names and label is not None:
        values["name"] = label
    if "is_active" in column_names:
        values["is_active"] = True

    key_prefix = raw_api_key[:12]
    key_last4 = raw_api_key[-4:]
    if "key_prefix" in column_names:
        values["key_prefix"] = key_prefix
    if "prefix" in column_names:
        values["prefix"] = key_prefix
    if "key_last4" in column_names:
        values["key_last4"] = key_last4
    if "last4" in column_names:
        values["last4"] = key_last4

    missing_required: list[str] = []
    for col in api_keys_table.columns:
        if col.name in values:
            continue
        if col.nullable:
            continue
        if col.default is not None or col.server_default is not None:
            continue
        if col.primary_key:
            continue
        missing_required.append(col.name)

    if missing_required:
        raise ValueError(
            "api_keys has required columns not handled by insert payload: "
            f"{', '.join(sorted(missing_required))}"
        )

    return values


def create_api_key(
    db: Session,
    *,
    user_id: UUID,
    raw_api_key: str,
    label: str | None = None,
) -> tuple[UUID, UUID]:
    """
    Create or reuse API key mapping in api_keys table.

    Args:
        db: Database session
        user_id: User ID to own the key
        raw_api_key: Raw API key secret (will be hashed; not stored)
        label: Optional label/name for key

    Returns:
        tuple[UUID, UUID]: (api_key_id, owner_user_id)

    Note:
        Does NOT commit. Caller must commit.
        If key hash already exists for another owner, existing owner is always returned.
    """
    from db.tables import get_table

    api_keys = get_table("api_keys")
    key_hash = compute_api_key_hash(raw_api_key)

    # Reuse existing mapping if hash already exists.
    existing_stmt = select(api_keys.c.id, api_keys.c.user_id).where(api_keys.c.key_hash == key_hash)
    existing = db.execute(existing_stmt).first()
    if existing:
        api_key_id, owner_user_id = existing
        if owner_user_id != user_id:
            logger.warning(
                "API key hash already mapped to different owner; forcing existing owner",
                extra={
                    "extra_fields": {
                        "api_key_id": str(api_key_id),
                        "existing_owner_user_id": str(owner_user_id),
                        "requested_user_id": str(user_id),
                    }
                },
            )
        logger.debug(f"API key hash already exists: {str(api_key_id)[:8]}...")
        return api_key_id, owner_user_id

    values = _build_api_key_insert_values(
        api_keys,
        user_id=user_id,
        key_hash=key_hash,
        raw_api_key=raw_api_key,
        label=label,
    )

    stmt = insert(api_keys).values(**values).returning(api_keys.c.id)
    api_key_id = db.execute(stmt).scalar_one()
    logger.info(
        f"Created api_key mapping: api_key_id={api_key_id}, "
        f"user_id={user_id}, label={label or 'n/a'}"
    )
    return api_key_id, user_id


def _safe_json_dict(payload: Any) -> dict[str, Any]:
    """Ensure payload is JSONB-safe dict-like data."""
    if isinstance(payload, dict):
        return payload
    return {}


def _normalize_prompt_category(value: str | None) -> str:
    category = (value or "unknown").strip().lower()
    return category if category in ALLOWED_PROMPT_CATEGORIES else "unknown"


def _normalize_research_mode(value: str | None) -> str:
    mode = (value or "off").strip().lower()
    return mode if mode in ALLOWED_RESEARCH_MODES else "off"


def _normalize_routing_mode(value: str | None) -> str:
    mode = (value or "smart").strip().lower()
    return mode if mode in ALLOWED_ROUTING_MODES else "legacy"


def create_routing_decision(
    db: Session,
    llm_request_id: UUID,
    routing_metadata: dict[str, Any],
    *,
    prompt_category: str = "unknown",
    research_mode: str = "off",
    features: dict[str, Any] | None = None,
) -> UUID:
    """
    Upsert routing_decisions row for a request.

    Args:
        db: Database session
        llm_request_id: FK to llm_requests.id
        routing_metadata: metadata["routing"] payload from UnifiedResponse
        prompt_category: Prompt category enum value
        research_mode: off|auto|on
        features: Optional PromptAnalyzer-like features payload

    Returns:
        UUID: routing_decisions.id

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    routing_decisions = get_table("routing_decisions")

    trace = _safe_json_dict(routing_metadata)
    decision_reasons_raw = trace.get("decision_reasons", [])
    if isinstance(decision_reasons_raw, list):
        decision_reasons = [str(reason) for reason in decision_reasons_raw]
    else:
        decision_reasons = []

    attempts_raw = trace.get("attempts", [])
    attempt_count = trace.get("attempt_count")
    if attempt_count is None:
        attempt_count = len(attempts_raw) if isinstance(attempts_raw, list) else 1
    try:
        attempt_count = max(int(attempt_count), 1)
    except Exception:
        attempt_count = 1

    payload = {
        "llm_request_id": llm_request_id,
        "prompt_category": _normalize_prompt_category(prompt_category),
        "research_mode": _normalize_research_mode(research_mode),
        "routing_mode": _normalize_routing_mode(trace.get("mode")),
        "initial_tier": trace.get("initial_tier"),
        "final_tier": trace.get("final_tier"),
        "attempt_count": attempt_count,
        "fallback_used": bool(trace.get("fallback_used", False)),
        "decision_reasons": decision_reasons,
        "features": _safe_json_dict(features),
        "trace": trace,
    }

    stmt = (
        pg_insert(routing_decisions)
        .values(**payload)
        .on_conflict_do_update(
            index_elements=["llm_request_id"],
            set_={
                "prompt_category": payload["prompt_category"],
                "research_mode": payload["research_mode"],
                "routing_mode": payload["routing_mode"],
                "initial_tier": payload["initial_tier"],
                "final_tier": payload["final_tier"],
                "attempt_count": payload["attempt_count"],
                "fallback_used": payload["fallback_used"],
                "decision_reasons": payload["decision_reasons"],
                "features": payload["features"],
                "trace": payload["trace"],
            },
        )
        .returning(routing_decisions.c.id)
    )

    routing_decision_id = db.execute(stmt).scalar_one()
    logger.debug(
        f"Upserted routing_decision: {routing_decision_id} for llm_request_id: {llm_request_id}"
    )
    return routing_decision_id


def create_routing_attempts(
    db: Session,
    routing_decision_id: UUID,
    attempts: list[dict[str, Any]] | None,
) -> int:
    """
    Upsert routing_attempts rows from routing metadata.

    Args:
        db: Database session
        routing_decision_id: FK to routing_decisions.id
        attempts: Attempt payload list from selected_sequence/attempts

    Returns:
        int: Number of attempt rows processed

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    if not attempts:
        return 0

    routing_attempts = get_table("routing_attempts")
    processed = 0

    for index, attempt in enumerate(attempts, start=1):
        if not isinstance(attempt, dict):
            continue

        raw_attempt_number = attempt.get("attempt_number", index)
        try:
            attempt_number = int(raw_attempt_number)
        except Exception:
            attempt_number = index
        attempt_number = max(attempt_number, 1)

        validation = str(attempt.get("validation", "") or "").strip().lower()
        if not validation:
            status = str(attempt.get("status", "") or "").strip().lower()
            validation = "ok" if status == "success" else "unknown"

        error_message = attempt.get("error_message") or attempt.get("why_failed")
        if error_message is not None:
            error_message = str(error_message)
            if len(error_message) > 4000:
                error_message = error_message[:4000]

        error_type = attempt.get("error_type")
        if not error_type and validation != "ok":
            error_type = validation
        if error_type is not None:
            error_type = str(error_type)

        latency = attempt.get("latency_ms")
        if latency is not None:
            try:
                latency = int(latency)
            except Exception:
                latency = None

        payload = {
            "routing_decision_id": routing_decision_id,
            "attempt_number": attempt_number,
            "tier": attempt.get("tier"),
            "provider": attempt.get("provider"),
            "model": attempt.get("model"),
            "validation": validation,
            "latency_ms": latency,
            "error_type": error_type,
            "error_message": error_message,
        }

        stmt = (
            pg_insert(routing_attempts)
            .values(**payload)
            .on_conflict_do_update(
                index_elements=["routing_decision_id", "attempt_number"],
                set_={
                    "tier": payload["tier"],
                    "provider": payload["provider"],
                    "model": payload["model"],
                    "validation": payload["validation"],
                    "latency_ms": payload["latency_ms"],
                    "error_type": payload["error_type"],
                    "error_message": payload["error_message"],
                },
            )
        )

        db.execute(stmt)
        processed += 1

    logger.debug(
        f"Upserted {processed} routing_attempts for routing_decision_id: {routing_decision_id}"
    )
    return processed


def get_failed_routing_attempts_by_request_group(
    db: Session,
    user_id: UUID,
    request_group_id: UUID,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Return failed routing attempts for one compare request group scoped to one user.

    Failure rows are defined as attempts where validation != 'ok' or an explicit error
    type/message is present.
    """
    from db.tables import get_table

    llm_requests = get_table("llm_requests")
    routing_decisions = get_table("routing_decisions")
    routing_attempts = get_table("routing_attempts")

    req_cols = {col.name for col in llm_requests.columns}
    attempt_cols = {col.name for col in routing_attempts.columns}

    if "request_group_id" not in req_cols:
        return []

    failure_conditions = []
    if "validation" in attempt_cols:
        failure_conditions.append(
            func.lower(func.coalesce(routing_attempts.c.validation, "")) != "ok"
        )
    if "error_type" in attempt_cols:
        failure_conditions.append(routing_attempts.c.error_type.is_not(None))
    if "error_message" in attempt_cols:
        failure_conditions.append(routing_attempts.c.error_message.is_not(None))
    if not failure_conditions:
        return []

    request_created_expr = (
        llm_requests.c.created_at if "created_at" in req_cols else llm_requests.c.id
    )
    attempt_number_expr = (
        routing_attempts.c.attempt_number if "attempt_number" in attempt_cols else literal(0)
    )

    group_text_expr = func.lower(cast(llm_requests.c.request_group_id, String))
    request_group_match = or_(
        group_text_expr == str(request_group_id).lower(),
        func.replace(group_text_expr, "-", "") == request_group_id.hex.lower(),
    )

    stmt = (
        select(
            llm_requests.c.request_id.label("request_id"),
            llm_requests.c.request_group_id.label("request_group_id"),
            attempt_number_expr.label("attempt_number"),
            (routing_attempts.c.tier if "tier" in attempt_cols else literal(None)).label("tier"),
            (routing_attempts.c.provider if "provider" in attempt_cols else literal(None)).label(
                "provider"
            ),
            (routing_attempts.c.model if "model" in attempt_cols else literal(None)).label("model"),
            (
                routing_attempts.c.validation if "validation" in attempt_cols else literal(None)
            ).label("validation"),
            (
                routing_attempts.c.latency_ms if "latency_ms" in attempt_cols else literal(None)
            ).label("latency_ms"),
            (
                routing_attempts.c.error_type if "error_type" in attempt_cols else literal(None)
            ).label("error_type"),
            (
                routing_attempts.c.error_message
                if "error_message" in attempt_cols
                else literal(None)
            ).label("error_message"),
        )
        .select_from(
            llm_requests.join(
                routing_decisions,
                routing_decisions.c.llm_request_id == llm_requests.c.id,
            ).join(
                routing_attempts,
                routing_attempts.c.routing_decision_id == routing_decisions.c.id,
            )
        )
        .where(
            and_(
                llm_requests.c.user_id == user_id,
                request_group_match,
                or_(*failure_conditions),
            )
        )
        .order_by(desc(request_created_expr), attempt_number_expr)
        .limit(max(1, int(limit)))
    )

    rows = db.execute(stmt).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row._mapping)
        group_value = payload.get("request_group_id")
        if group_value is not None:
            payload["request_group_id"] = str(group_value)
        if payload.get("attempt_number") is not None:
            try:
                payload["attempt_number"] = int(payload["attempt_number"])
            except Exception:
                pass
        result.append(payload)

    return result


# ============================================================================
# USAGE TRACKING & ENFORCEMENT
# ============================================================================


def get_usage_daily(
    db: Session, user_id: UUID, usage_date: date | None = None
) -> dict[str, Any] | None:
    """
    Get usage_daily row for user and date.

    Args:
        db: Database session
        user_id: User ID
        usage_date: Date to query (default: today)

    Returns:
        dict: Usage data or None if no usage today
    """
    from db.tables import get_table

    usage_daily = get_table("usage_daily")

    if usage_date is None:
        usage_date = date.today()

    stmt = select(usage_daily).where(
        and_(usage_daily.c.user_id == user_id, usage_daily.c.usage_date == usage_date)
    )

    result = db.execute(stmt).first()

    if result:
        return dict(result._mapping)
    return None


def upsert_usage_daily(
    db: Session,
    user_id: UUID,
    total_tokens: int,
    estimated_cost: float,
    usage_date: date | None = None,
) -> None:
    """
    Upsert (INSERT or UPDATE) usage_daily for user and date.

    Uses PostgreSQL ON CONFLICT to handle both insert and update in one query.

    Args:
        db: Database session
        user_id: User ID
        total_tokens: Tokens to add
        estimated_cost: Cost to add
        usage_date: Date to update (default: today)

    Note:
        Does NOT commit. Caller must commit.
    """
    from db.tables import get_table

    usage_daily = get_table("usage_daily")

    if usage_date is None:
        usage_date = date.today()

    # PostgreSQL UPSERT
    stmt = (
        pg_insert(usage_daily)
        .values(
            user_id=user_id,
            usage_date=usage_date,
            total_requests=1,
            total_tokens=total_tokens,
            total_cost=estimated_cost,
        )
        .on_conflict_do_update(
            index_elements=["user_id", "usage_date"],
            set_=dict(
                total_requests=usage_daily.c.total_requests + 1,
                total_tokens=usage_daily.c.total_tokens + total_tokens,
                total_cost=usage_daily.c.total_cost + estimated_cost,
            ),
        )
    )

    db.execute(stmt)

    logger.debug(
        f"Updated usage_daily for user {user_id}: "
        f"+{total_tokens} tokens, +${estimated_cost:.6f}"
    )


def check_usage_limit(
    db: Session, user_id: UUID, token_cap: int | None = None, cost_cap: float | None = None
) -> dict[str, Any]:
    """
    Check if user has exceeded usage limits for today.

    Args:
        db: Database session
        user_id: User ID
        token_cap: Max tokens per day (None = no limit)
        cost_cap: Max cost per day in dollars (None = no limit)

    Returns:
        dict: {
            "allowed": bool,
            "current_tokens": int,
            "current_cost": float,
            "reason": str (if denied)
        }
    """
    usage = get_usage_daily(db, user_id)

    if usage is None:
        # No usage today - allowed
        return {"allowed": True, "current_tokens": 0, "current_cost": 0.0}

    current_tokens = usage["total_tokens"]
    current_cost = float(usage["total_cost"])

    # Check token cap
    if token_cap is not None and current_tokens >= token_cap:
        return {
            "allowed": False,
            "current_tokens": current_tokens,
            "current_cost": current_cost,
            "reason": f"Daily token limit exceeded ({current_tokens}/{token_cap})",
        }

    # Check cost cap
    if cost_cap is not None and current_cost >= cost_cap:
        return {
            "allowed": False,
            "current_tokens": current_tokens,
            "current_cost": current_cost,
            "reason": f"Daily cost limit exceeded (${current_cost:.2f}/${cost_cap:.2f})",
        }

    # Allowed
    return {"allowed": True, "current_tokens": current_tokens, "current_cost": current_cost}


# ============================================================================
# B2B SETTINGS / BYOK / SAVINGS / REPORTING
# ============================================================================


def get_api_key_settings(db: Session, api_key_id: UUID) -> dict[str, Any] | None:
    """Fetch per-api-key settings row."""
    from db.tables import get_table

    try:
        api_key_settings = get_table("api_key_settings")
    except NoSuchTableError:
        logger.warning(
            "api_key_settings table is missing; treating per-api-key settings as unavailable"
        )
        return None

    stmt = select(api_key_settings).where(api_key_settings.c.api_key_id == api_key_id)
    row = db.execute(stmt).first()
    if row:
        return dict(row._mapping)
    return None


def upsert_api_key_settings(
    db: Session,
    *,
    api_key_id: UUID,
    baseline_provider: str | None = None,
    baseline_model: str | None = None,
    requests_per_minute: int | None = None,
) -> None:
    """Create/update per-api-key settings."""
    from db.tables import get_table

    api_key_settings = get_table("api_key_settings")
    payload: dict[str, Any] = {
        "api_key_id": api_key_id,
        "baseline_provider": baseline_provider,
        "baseline_model": baseline_model,
        "requests_per_minute": requests_per_minute,
    }

    stmt = (
        pg_insert(api_key_settings)
        .values(**payload)
        .on_conflict_do_update(
            index_elements=["api_key_id"],
            set_={
                "baseline_provider": payload["baseline_provider"],
                "baseline_model": payload["baseline_model"],
                "requests_per_minute": payload["requests_per_minute"],
                "updated_at": func.now(),
            },
        )
    )
    db.execute(stmt)


def upsert_byok_provider_key(
    db: Session,
    *,
    api_key_id: UUID,
    provider: str,
    encrypted_key: str,
    key_fingerprint: str,
    key_last4: str | None = None,
) -> None:
    """Upsert encrypted BYOK provider credential for one API key."""
    from db.tables import get_table

    byok_provider_keys = get_table("byok_provider_keys")
    provider_norm = (provider or "").strip().lower()
    payload = {
        "api_key_id": api_key_id,
        "provider": provider_norm,
        "encrypted_key": encrypted_key,
        "key_fingerprint": key_fingerprint,
        "key_last4": key_last4,
    }
    stmt = (
        pg_insert(byok_provider_keys)
        .values(**payload)
        .on_conflict_do_update(
            index_elements=["api_key_id", "provider"],
            set_={
                "encrypted_key": payload["encrypted_key"],
                "key_fingerprint": payload["key_fingerprint"],
                "key_last4": payload["key_last4"],
                "updated_at": func.now(),
            },
        )
    )
    db.execute(stmt)


def get_byok_provider_key(db: Session, *, api_key_id: UUID, provider: str) -> dict[str, Any] | None:
    """Fetch one encrypted BYOK provider row."""
    from db.tables import get_table

    byok_provider_keys = get_table("byok_provider_keys")
    stmt = select(byok_provider_keys).where(
        and_(
            byok_provider_keys.c.api_key_id == api_key_id,
            byok_provider_keys.c.provider == (provider or "").strip().lower(),
        )
    )
    row = db.execute(stmt).first()
    if row:
        return dict(row._mapping)
    return None


def list_byok_provider_keys(db: Session, *, api_key_id: UUID) -> list[dict[str, Any]]:
    """List encrypted BYOK rows for one API key."""
    from db.tables import get_table

    byok_provider_keys = get_table("byok_provider_keys")
    stmt = (
        select(byok_provider_keys)
        .where(byok_provider_keys.c.api_key_id == api_key_id)
        .order_by(byok_provider_keys.c.provider)
    )
    rows = db.execute(stmt).fetchall()
    return [dict(row._mapping) for row in rows]


def delete_byok_provider_keys(db: Session, *, api_key_id: UUID, provider: str | None = None) -> int:
    """Delete BYOK rows for one API key (single provider or all)."""
    from db.tables import get_table

    byok_provider_keys = get_table("byok_provider_keys")
    where_clause = byok_provider_keys.c.api_key_id == api_key_id
    if provider:
        where_clause = and_(
            where_clause,
            byok_provider_keys.c.provider == provider.strip().lower(),
        )

    result = db.execute(delete(byok_provider_keys).where(where_clause))
    return int(result.rowcount or 0)


def upsert_llm_savings(
    db: Session,
    *,
    llm_request_id: UUID,
    user_id: UUID,
    api_key_id: UUID | None,
    provider: str,
    model: str,
    actual_cost: float,
    baseline_provider: str,
    baseline_model: str,
    baseline_cost: float,
    savings_amount: float,
    savings_pct: float,
    response_status: str = "success",
    error_code: str | None = None,
    usage_date: date | None = None,
) -> None:
    """Upsert per-request savings telemetry."""
    from db.tables import get_table

    llm_savings = get_table("llm_savings")
    cols = {col.name for col in llm_savings.columns}
    payload: dict[str, Any] = {
        "llm_request_id": llm_request_id,
        "user_id": user_id,
        "api_key_id": api_key_id,
        "usage_date": usage_date or date.today(),
        "provider": provider,
        "model": model,
        "actual_cost": float(actual_cost or 0.0),
        "baseline_provider": baseline_provider,
        "baseline_model": baseline_model,
        "baseline_cost": float(baseline_cost or 0.0),
        "savings_amount": float(savings_amount or 0.0),
        "savings_pct": float(savings_pct or 0.0),
    }
    if "response_status" in cols:
        payload["response_status"] = (response_status or "success").strip().lower()
    if "error_code" in cols:
        payload["error_code"] = error_code

    stmt = (
        pg_insert(llm_savings)
        .values(**payload)
        .on_conflict_do_update(
            index_elements=["llm_request_id"],
            set_={k: v for k, v in payload.items() if k != "llm_request_id"},
        )
    )
    db.execute(stmt)


def _apply_date_range_filters(
    stmt,
    *,
    created_col,
    date_from: date | None,
    date_to: date | None,
):
    if created_col is None:
        return stmt

    if date_from is not None:
        stmt = stmt.where(created_col >= date_from)
    if date_to is not None:
        stmt = stmt.where(created_col < (date_to + timedelta(days=1)))
    return stmt


def get_usage_aggregates(
    db: Session,
    *,
    user_id: UUID,
    date_from: date | None = None,
    date_to: date | None = None,
    group_by: str = "day",
) -> list[dict[str, Any]]:
    """
    Aggregate usage from llm_requests/llm_responses for a user.

    Returns rows with keys: bucket, requests, tokens, cost.
    """
    from db.tables import get_table

    llm_requests = get_table("llm_requests")
    llm_responses = get_table("llm_responses")

    req_cols = {col.name for col in llm_requests.columns}
    resp_cols = {col.name for col in llm_responses.columns}

    created_col = llm_requests.c.created_at if "created_at" in req_cols else None
    provider_col = llm_requests.c.provider if "provider" in req_cols else None
    operation_col = llm_requests.c.route_mode if "route_mode" in req_cols else None
    model_col = (
        llm_responses.c.served_model
        if "served_model" in resp_cols
        else (llm_requests.c.model if "model" in req_cols else None)
    )
    tokens_col = llm_responses.c.total_tokens if "total_tokens" in resp_cols else None
    cost_col = llm_responses.c.estimated_cost if "estimated_cost" in resp_cols else None

    group = (group_by or "day").strip().lower()
    if group == "provider":
        bucket_expr = provider_col if provider_col is not None else literal("unknown")
    elif group == "model":
        bucket_expr = model_col if model_col is not None else literal("unknown")
    elif group == "operation":
        bucket_expr = operation_col if operation_col is not None else literal("unknown")
    else:
        group = "day"
        if created_col is not None:
            bucket_expr = func.date(created_col)
        else:
            bucket_expr = literal("unknown")

    stmt = (
        select(
            bucket_expr.label("bucket"),
            func.count(llm_requests.c.id).label("requests"),
            (
                func.coalesce(func.sum(tokens_col), 0).label("tokens")
                if tokens_col is not None
                else literal(0).label("tokens")
            ),
            (
                func.coalesce(func.sum(cost_col), 0.0).label("cost")
                if cost_col is not None
                else literal(0.0).label("cost")
            ),
        )
        .select_from(
            llm_requests.outerjoin(
                llm_responses,
                llm_responses.c.llm_request_id == llm_requests.c.id,
            )
        )
        .where(llm_requests.c.user_id == user_id)
        .group_by(bucket_expr)
        .order_by(bucket_expr)
    )

    stmt = _apply_date_range_filters(
        stmt,
        created_col=created_col,
        date_from=date_from,
        date_to=date_to,
    )

    rows = db.execute(stmt).fetchall()
    payload: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row._mapping)
        bucket_value = item.get("bucket")
        if group == "day" and bucket_value is not None:
            bucket_value = str(bucket_value)
        payload.append(
            {
                "bucket": bucket_value if bucket_value is not None else "unknown",
                "requests": int(item.get("requests") or 0),
                "tokens": int(item.get("tokens") or 0),
                "cost": float(item.get("cost") or 0.0),
            }
        )
    return payload


def get_savings_aggregates(
    db: Session,
    *,
    user_id: UUID,
    date_from: date | None = None,
    date_to: date | None = None,
    group_by: str = "day",
) -> list[dict[str, Any]]:
    """
    Aggregate savings from llm_savings for a user.

    Returns rows with keys:
    bucket, requests, actual_cost, baseline_cost, savings_amount, savings_pct.
    """
    from db.tables import get_table

    llm_savings = get_table("llm_savings")
    cols = {col.name for col in llm_savings.columns}

    usage_date_col = llm_savings.c.usage_date if "usage_date" in cols else None
    provider_col = llm_savings.c.provider if "provider" in cols else None
    model_col = llm_savings.c.model if "model" in cols else None
    actual_col = llm_savings.c.actual_cost if "actual_cost" in cols else None
    baseline_col = llm_savings.c.baseline_cost if "baseline_cost" in cols else None
    savings_col = llm_savings.c.savings_amount if "savings_amount" in cols else None
    status_col = llm_savings.c.response_status if "response_status" in cols else None

    group = (group_by or "day").strip().lower()
    if group == "provider":
        bucket_expr = provider_col if provider_col is not None else literal("unknown")
    elif group == "model":
        bucket_expr = model_col if model_col is not None else literal("unknown")
    else:
        group = "day"
        if usage_date_col is not None:
            bucket_expr = usage_date_col
        else:
            bucket_expr = literal("unknown")

    sum_actual = (
        func.coalesce(func.sum(actual_col), 0.0).label("actual_cost")
        if actual_col is not None
        else literal(0.0).label("actual_cost")
    )
    sum_baseline = (
        func.coalesce(func.sum(baseline_col), 0.0).label("baseline_cost")
        if baseline_col is not None
        else literal(0.0).label("baseline_cost")
    )
    sum_savings = (
        func.coalesce(func.sum(savings_col), 0.0).label("savings_amount")
        if savings_col is not None
        else literal(0.0).label("savings_amount")
    )
    failed_expr = (
        func.coalesce(
            func.sum(
                case(
                    (
                        func.lower(func.coalesce(status_col, literal("success"))) != "success",
                        1,
                    ),
                    else_=0,
                )
            ),
            0,
        ).label("failed_requests")
        if status_col is not None
        else literal(0).label("failed_requests")
    )

    stmt = (
        select(
            bucket_expr.label("bucket"),
            func.count(llm_savings.c.llm_request_id).label("requests"),
            sum_actual,
            sum_baseline,
            sum_savings,
            failed_expr,
        )
        .where(llm_savings.c.user_id == user_id)
        .group_by(bucket_expr)
        .order_by(bucket_expr)
    )

    if usage_date_col is not None:
        if date_from is not None:
            stmt = stmt.where(usage_date_col >= date_from)
        if date_to is not None:
            stmt = stmt.where(usage_date_col <= date_to)

    rows = db.execute(stmt).fetchall()
    payload: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row._mapping)
        bucket_value = item.get("bucket")
        if group == "day" and bucket_value is not None:
            bucket_value = str(bucket_value)

        baseline_cost = float(item.get("baseline_cost") or 0.0)
        savings_amount = float(item.get("savings_amount") or 0.0)
        savings_pct = (savings_amount / baseline_cost) if baseline_cost > 0 else 0.0
        failed_requests = int(item.get("failed_requests") or 0)
        total_requests = int(item.get("requests") or 0)
        payload.append(
            {
                "bucket": bucket_value if bucket_value is not None else "unknown",
                "requests": total_requests,
                "actual_cost": float(item.get("actual_cost") or 0.0),
                "baseline_cost": baseline_cost,
                "savings_amount": savings_amount,
                "savings_pct": savings_pct,
                "successful_requests": max(total_requests - failed_requests, 0),
                "failed_requests": failed_requests,
            }
        )
    return payload


# ============================================================================
# API HISTORY (LLM AUDIT BACKED)
# ============================================================================


def _history_entry_id(request_pk: Any, request_id: str | None) -> int:
    """
    Compute a deterministic int identifier for history entries.

    Prefers native integer primary keys; otherwise derives a stable 31-bit hash
    from request identifiers/primary key values.
    """
    if isinstance(request_pk, int):
        return request_pk

    if isinstance(request_pk, UUID):
        return int(request_pk.int % 2147483647)

    if request_pk is not None:
        raw_pk = str(request_pk).strip()
        if raw_pk:
            try:
                return int(raw_pk)
            except Exception:
                pass

    source = str(request_id or request_pk or "")
    if not source:
        source = "0"
    return int(zlib.crc32(source.encode("utf-8")) & 0x7FFFFFFF)


def _normalize_history_web_source_items(raw_items: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    if not isinstance(raw_items, list):
        return items

    for item in raw_items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        normalized_url = url.lower()
        if normalized_url in seen_urls:
            continue
        seen_urls.add(normalized_url)
        title = str(item.get("title") or "").strip() or url
        items.append({"title": title, "url": url})
        if len(items) >= 8:
            break

    return items


def _history_routing_trace_payload(routing_trace: Any) -> dict[str, Any]:
    if isinstance(routing_trace, dict):
        return routing_trace
    if isinstance(routing_trace, str):
        text = routing_trace.strip()
        if text:
            try:
                decoded = json.loads(text)
                if isinstance(decoded, dict):
                    return decoded
            except Exception:
                pass
    return {}


def _extract_history_web_source_items(routing_trace: Any) -> list[dict[str, str]]:
    trace_payload = _history_routing_trace_payload(routing_trace)

    if not trace_payload:
        return []

    raw_items = trace_payload.get("web_source_items")
    if not isinstance(raw_items, list):
        raw_items = trace_payload.get("sources")
    return _normalize_history_web_source_items(raw_items)


def _history_optional_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _extract_history_credit_snapshot(routing_trace: Any) -> dict[str, Any]:
    trace_payload = _history_routing_trace_payload(routing_trace)
    ai_credits = _history_optional_nonnegative_int(trace_payload.get("ai_credits"))
    research_ai_credits = _history_optional_nonnegative_int(
        trace_payload.get("research_ai_credits")
    )
    return {
        "ai_credits": ai_credits,
        "credit_usage_estimated": bool(
            trace_payload.get("credit_usage_estimated", False)
        ),
        "research_ai_credits": research_ai_credits,
        "research_credit_usage_estimated": bool(
            trace_payload.get("research_credit_usage_estimated", False)
        ),
    }


def _to_history_timestamp(raw_value: Any) -> str:
    """Normalize DB timestamp-like values to ISO8601 with trailing Z when possible."""
    if raw_value is None:
        return ""

    # datetime objects
    try:
        iso = raw_value.isoformat()
        if iso.endswith("+00:00"):
            return iso.replace("+00:00", "Z")
        return iso
    except Exception:
        pass

    text = str(raw_value).strip()
    if not text:
        return ""
    if text.endswith("+00:00"):
        return text.replace("+00:00", "Z")
    return text


def _decode_json_value(raw_value: Any, fallback: Any) -> Any:
    if raw_value is None:
        return fallback
    if isinstance(raw_value, (list, dict)):
        return raw_value
    if isinstance(raw_value, str):
        try:
            return json.loads(raw_value)
        except Exception:
            return fallback
    return fallback


def _revision_root_key(payload: dict[str, Any]) -> str:
    root_id = payload.get("response_revision_root_id")
    request_pk = payload.get("request_pk")
    return (
        str(root_id or request_pk or payload.get("request_id") or "")
        .strip()
        .lower()
        .replace("-", "")
    )


def require_cortex_analysis_schema() -> None:
    """Fail predictably before provider work when the Cortex migration is missing."""
    from db.tables import get_table

    try:
        analysis_runs = get_table("cortex_analysis_runs")
        llm_requests = get_table("llm_requests")
    except NoSuchTableError as exc:
        raise CortexAnalysisSchemaUnavailable(
            "cortex_analysis_runs is missing; apply "
            "db/migrations/20260727_add_cortex_analysis_runs.sql"
        ) from exc

    required_run_columns = {
        "id",
        "user_id",
        "session_id",
        "request_group_id",
        "source_fingerprint",
        "source_snapshot",
        "recommended_answer",
        "disagreement_note",
        "created_at",
    }
    run_columns = {column.name for column in analysis_runs.columns}
    request_columns = {column.name for column in llm_requests.columns}
    missing_run_columns = sorted(required_run_columns - run_columns)
    missing_request_columns = sorted(
        {"response_revision_root_id", "response_revision"} - request_columns
    )
    if missing_run_columns or missing_request_columns:
        missing = ", ".join(missing_run_columns + missing_request_columns)
        raise CortexAnalysisSchemaUnavailable(
            f"Cortex Analysis schema is incomplete ({missing}); apply the Cortex migrations "
            "and restart the API"
        )


def get_compare_response_regeneration_source(
    db: Session,
    user_id: UUID,
    source_request_id: str,
) -> dict[str, Any] | None:
    """Resolve an owned Compare response and its next append-only revision."""
    from db.tables import get_table

    normalized_request_id = str(source_request_id or "").strip()
    if not normalized_request_id:
        return None

    llm_requests = get_table("llm_requests")
    req_cols = {col.name for col in llm_requests.columns}
    if "request_group_id" not in req_cols:
        return None

    revision_root_expr = (
        llm_requests.c.response_revision_root_id
        if "response_revision_root_id" in req_cols
        else literal(None)
    )
    revision_expr = (
        llm_requests.c.response_revision if "response_revision" in req_cols else literal(1)
    )
    row = db.execute(
        select(
            llm_requests.c.id.label("request_pk"),
            llm_requests.c.request_id.label("request_id"),
            llm_requests.c.session_id.label("session_id"),
            llm_requests.c.request_group_id.label("request_group_id"),
            llm_requests.c.provider.label("provider"),
            llm_requests.c.model.label("model"),
            revision_root_expr.label("response_revision_root_id"),
            revision_expr.label("response_revision"),
        ).where(
            and_(
                llm_requests.c.user_id == user_id,
                llm_requests.c.request_id == normalized_request_id,
                llm_requests.c.request_group_id.is_not(None),
            )
        )
    ).fetchone()
    if row is None:
        return None

    payload = dict(row._mapping)
    root_id = payload.get("response_revision_root_id") or payload["request_pk"]
    next_revision = int(payload.get("response_revision") or 1) + 1
    if "response_revision_root_id" in req_cols and "response_revision" in req_cols:
        latest_revision = db.execute(
            select(func.max(llm_requests.c.response_revision)).where(
                or_(
                    llm_requests.c.id == root_id,
                    llm_requests.c.response_revision_root_id == root_id,
                )
            )
        ).scalar_one_or_none()
        next_revision = int(latest_revision or 1) + 1

    return {
        "source_request_pk": payload["request_pk"],
        "response_revision_root_id": root_id,
        "response_revision": next_revision,
        "session_id": payload.get("session_id"),
        "request_group_id": payload.get("request_group_id"),
        "provider": str(payload.get("provider") or ""),
        "model": str(payload.get("model") or ""),
    }


def get_compare_analysis_sources(
    db: Session,
    user_id: UUID,
    request_group_id: UUID,
) -> dict[str, Any] | None:
    """Return the latest revision for every response slot in an owned Compare run."""
    from db.tables import get_table

    llm_requests = get_table("llm_requests")
    llm_responses = get_table("llm_responses")
    routing_decisions = get_table("routing_decisions")
    req_cols = {col.name for col in llm_requests.columns}
    resp_cols = {col.name for col in llm_responses.columns}
    routing_cols = {col.name for col in routing_decisions.columns}
    if "request_group_id" not in req_cols:
        return None

    revision_root_expr = (
        llm_requests.c.response_revision_root_id
        if "response_revision_root_id" in req_cols
        else literal(None)
    )
    revision_expr = (
        llm_requests.c.response_revision if "response_revision" in req_cols else literal(1)
    )
    response_text_expr = llm_responses.c.text if "text" in resp_cols else literal("")
    response_error_expr = (
        llm_responses.c.error_message if "error_message" in resp_cols else literal(None)
    )
    routing_trace_expr = routing_decisions.c.trace if "trace" in routing_cols else literal(None)
    created_expr = llm_requests.c.created_at if "created_at" in req_cols else llm_requests.c.id
    group_text_expr = func.lower(cast(llm_requests.c.request_group_id, String))
    group_match = or_(
        group_text_expr == str(request_group_id).lower(),
        func.replace(group_text_expr, "-", "") == request_group_id.hex.lower(),
    )

    rows = db.execute(
        select(
            llm_requests.c.id.label("request_pk"),
            llm_requests.c.request_id.label("request_id"),
            llm_requests.c.session_id.label("session_id"),
            llm_requests.c.request_group_id.label("request_group_id"),
            llm_requests.c.prompt_text.label("prompt"),
            llm_requests.c.provider.label("provider"),
            llm_requests.c.model.label("model"),
            revision_root_expr.label("response_revision_root_id"),
            revision_expr.label("response_revision"),
            response_text_expr.label("response_text"),
            response_error_expr.label("error_message"),
            routing_trace_expr.label("routing_trace"),
            created_expr.label("created_at"),
        )
        .select_from(
            llm_requests.outerjoin(
                llm_responses,
                llm_responses.c.llm_request_id == llm_requests.c.id,
            ).outerjoin(
                routing_decisions,
                routing_decisions.c.llm_request_id == llm_requests.c.id,
            )
        )
        .where(and_(llm_requests.c.user_id == user_id, group_match))
        .order_by(desc(created_expr), desc(revision_expr))
    ).fetchall()
    if not rows:
        return None

    current_rows: list[dict[str, Any]] = []
    seen_roots: set[str] = set()
    for row in rows:
        payload = dict(row._mapping)
        root_key = _revision_root_key(payload)
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        current_rows.append(payload)

    first = current_rows[0]
    return {
        "session_id": first.get("session_id"),
        "request_group_id": first.get("request_group_id"),
        "question": str(first.get("prompt") or ""),
        "responses": [
            {
                "request_id": str(item.get("request_id") or ""),
                "response_version": int(item.get("response_revision") or 1),
                "provider": str(item.get("provider") or "unknown"),
                "model": str(item.get("model") or "unknown"),
                "content": str(item.get("response_text") or ""),
                "web_source_items": _extract_history_web_source_items(
                    item.get("routing_trace")
                ),
                "web_search": (
                    _history_routing_trace_payload(item.get("routing_trace")).get(
                        "web_search", {}
                    )
                ),
                "error_message": (
                    str(item["error_message"]) if item.get("error_message") is not None else None
                ),
            }
            for item in reversed(current_rows)
        ],
    }


def get_prompt_optimization_cache(
    db: Session,
    *,
    user_id: UUID,
    cache_key: str,
) -> dict[str, Any] | None:
    """Return one unexpired shared optimizer result and refresh its usage time."""

    from db.tables import get_table

    cache = get_table("prompt_optimization_cache")
    row = db.execute(
        select(cache).where(
            and_(
                cache.c.user_id == user_id,
                cache.c.cache_key == str(cache_key),
                cache.c.expires_at > func.now(),
            )
        )
    ).first()
    if row is None:
        return None
    payload = dict(row._mapping)
    db.execute(
        update(cache)
        .where(cache.c.id == payload["id"])
        .values(last_used_at=func.now())
    )
    result = _decode_json_value(payload.get("result"), {})
    return result if isinstance(result, dict) else None


def record_cache_reuse_event(
    db: Session,
    *,
    user_id: UUID,
    request_id: str,
    operation_type: str,
    reused: bool,
) -> None:
    """Record one idempotent cache/reuse decision for dashboard rates."""

    from db.tables import get_table

    operation = str(operation_type or "").strip().lower()
    if operation not in ALLOWED_CACHE_REUSE_OPERATIONS:
        raise ValueError(f"Unsupported cache reuse operation: {operation_type}")
    normalized_request_id = str(request_id or "").strip()
    if not normalized_request_id:
        raise ValueError("request_id is required for a cache reuse event")
    normalized_request_id = normalized_request_id[:255]

    events = get_table("cache_reuse_events")
    existing = db.execute(
        select(events.c.id).where(
            and_(
                events.c.user_id == user_id,
                events.c.operation_type == operation,
                events.c.request_id == normalized_request_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.execute(
            insert(events).values(
                user_id=user_id,
                request_id=normalized_request_id,
                operation_type=operation,
                reused=bool(reused),
            )
        )
        return
    db.execute(
        update(events).where(events.c.id == existing).values(reused=bool(reused))
    )


def store_prompt_optimization_cache(
    db: Session,
    *,
    user_id: UUID,
    cache_key: str,
    optimizer_provider: str,
    optimizer_model: str,
    prompt_version: str,
    result: dict[str, Any],
    expires_at: datetime,
) -> None:
    """Persist one successful optimizer result for cross-worker reuse."""

    from db.tables import get_table

    cache = get_table("prompt_optimization_cache")
    existing = db.execute(
        select(cache.c.id).where(
            and_(cache.c.user_id == user_id, cache.c.cache_key == str(cache_key))
        )
    ).scalar_one_or_none()
    values = {
        "optimizer_provider": str(optimizer_provider),
        "optimizer_model": str(optimizer_model),
        "prompt_version": str(prompt_version),
        "result": dict(result),
        "last_used_at": func.now(),
        "expires_at": expires_at,
    }
    if existing is None:
        db.execute(
            insert(cache).values(
                id=uuid4(),
                user_id=user_id,
                cache_key=str(cache_key),
                **values,
            )
        )
    else:
        db.execute(update(cache).where(cache.c.id == existing).values(**values))


def create_cortex_analysis_run(
    db: Session,
    *,
    user_id: UUID,
    session_id: UUID,
    request_group_id: UUID,
    model: str,
    source_fingerprint: str,
    source_snapshot: list[dict[str, Any]],
    recommended_answer: str,
    agreements: list[str],
    disagreements: list[dict[str, Any]],
    disagreement_note: str | None,
    unique_insights: list[dict[str, Any]],
    confidence_level: str,
    confidence_reason: str,
    verify_items: list[str],
    high_stakes_domain: str | None,
    combined_response_count: int,
    failed_response_count: int,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    estimated_cost: float = 0.0,
    analysis_policy_version: str = "cortex-analysis-v1",
    cached_input_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    pricing_snapshot: dict[str, Any] | None = None,
    pricing_version: str | None = None,
) -> UUID:
    """Append one immutable Cortex Analysis run."""
    from db.tables import get_table

    analysis_runs = get_table("cortex_analysis_runs")
    analysis_id = uuid4()
    db.execute(
        insert(analysis_runs).values(
            id=analysis_id,
            user_id=user_id,
            session_id=session_id,
            request_group_id=request_group_id,
            model=model,
            source_fingerprint=source_fingerprint,
            source_snapshot=source_snapshot,
            recommended_answer=recommended_answer,
            agreements=agreements,
            disagreements=disagreements,
            disagreement_note=disagreement_note,
            unique_insights=unique_insights,
            confidence_level=confidence_level,
            confidence_reason=confidence_reason,
            verify_items=verify_items,
            high_stakes_domain=high_stakes_domain,
            combined_response_count=combined_response_count,
            failed_response_count=failed_response_count,
            prompt_tokens=max(0, int(prompt_tokens)),
            completion_tokens=max(0, int(completion_tokens)),
            total_tokens=max(0, int(total_tokens)),
            estimated_cost=max(0.0, float(estimated_cost)),
            analysis_policy_version=str(analysis_policy_version or "cortex-analysis-v1"),
            cached_input_tokens=max(0, int(cached_input_tokens)),
            cache_write_tokens=max(0, int(cache_write_tokens)),
            reasoning_tokens=max(0, int(reasoning_tokens)),
            pricing_snapshot=dict(pricing_snapshot or {}),
            pricing_version=str(pricing_version or "") or None,
        )
    )
    return analysis_id


def list_cortex_analysis_runs(
    db: Session,
    user_id: UUID,
    *,
    request_group_id: UUID | None = None,
    session_id: UUID | None = None,
) -> list[dict[str, Any]]:
    """List immutable Cortex Analysis runs newest-first for one owner."""
    from db.tables import get_table

    if request_group_id is None and session_id is None:
        return []

    analysis_runs = get_table("cortex_analysis_runs")

    def uuid_match(column, value: UUID):
        normalized = str(value).lower()
        normalized_hex = normalized.replace("-", "")
        column_text = func.lower(cast(column, String))
        return or_(
            column_text == normalized,
            func.replace(column_text, "-", "") == normalized_hex,
        )

    filters = [uuid_match(analysis_runs.c.user_id, user_id)]
    if request_group_id is not None:
        filters.append(uuid_match(analysis_runs.c.request_group_id, request_group_id))
    if session_id is not None:
        filters.append(uuid_match(analysis_runs.c.session_id, session_id))

    rows = db.execute(
        select(analysis_runs).where(and_(*filters)).order_by(desc(analysis_runs.c.created_at))
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row._mapping)
        result.append(
            {
                "analysis_id": str(payload.get("id") or ""),
                "request_group_id": str(payload.get("request_group_id") or ""),
                "session_id": str(payload.get("session_id") or ""),
                "model": str(payload.get("model") or ""),
                "source_fingerprint": str(payload.get("source_fingerprint") or ""),
                "analysis_policy_version": str(
                    payload.get("analysis_policy_version") or ""
                ),
                "source_snapshot": _decode_json_value(
                    payload.get("source_snapshot"),
                    [],
                ),
                "recommended_answer": str(payload.get("recommended_answer") or ""),
                "agreements": _decode_json_value(payload.get("agreements"), []),
                "disagreements": _decode_json_value(payload.get("disagreements"), []),
                "disagreement_note": (
                    str(payload["disagreement_note"])
                    if payload.get("disagreement_note") is not None
                    else None
                ),
                "unique_insights": _decode_json_value(
                    payload.get("unique_insights"),
                    [],
                ),
                "confidence_level": str(payload.get("confidence_level") or "limited"),
                "confidence_reason": str(payload.get("confidence_reason") or ""),
                "verify_items": _decode_json_value(payload.get("verify_items"), []),
                "high_stakes_domain": (
                    str(payload["high_stakes_domain"])
                    if payload.get("high_stakes_domain") is not None
                    else None
                ),
                "combined_response_count": int(payload.get("combined_response_count") or 0),
                "failed_response_count": int(payload.get("failed_response_count") or 0),
                "prompt_tokens": int(payload.get("prompt_tokens") or 0),
                "cached_input_tokens": int(payload.get("cached_input_tokens") or 0),
                "cache_write_tokens": int(payload.get("cache_write_tokens") or 0),
                "reasoning_tokens": int(payload.get("reasoning_tokens") or 0),
                "completion_tokens": int(payload.get("completion_tokens") or 0),
                "total_tokens": int(payload.get("total_tokens") or 0),
                "estimated_cost": float(payload.get("estimated_cost") or 0.0),
                "pricing_snapshot": _decode_json_value(
                    payload.get("pricing_snapshot"), {}
                ),
                "pricing_version": str(payload.get("pricing_version") or "") or None,
                "created_at": _to_history_timestamp(payload.get("created_at")),
            }
        )
    return result


def get_llm_history_entries(
    db: Session,
    user_id: UUID,
    limit: int = 100,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    Return API history entries from llm_requests/llm_responses for one user.

    Shape is compatible with server/routes/history.py HistoryEntry response model.
    """
    from db.tables import get_table

    llm_requests = get_table("llm_requests")
    llm_responses = get_table("llm_responses")
    sessions = get_table("sessions")
    routing_decisions = None
    try:
        routing_decisions = get_table("routing_decisions")
    except Exception:
        routing_decisions = None

    req_cols = {col.name for col in llm_requests.columns}
    resp_cols = {col.name for col in llm_responses.columns}
    session_cols = {col.name for col in sessions.columns}
    routing_cols = (
        {col.name for col in routing_decisions.columns} if routing_decisions is not None else set()
    )

    req_created_col = llm_requests.c.created_at if "created_at" in req_cols else None
    req_mode_col = llm_requests.c.route_mode if "route_mode" in req_cols else None
    req_prompt_col = llm_requests.c.prompt_text if "prompt_text" in req_cols else None
    req_prompt_hash_col = llm_requests.c.prompt_sha256 if "prompt_sha256" in req_cols else None
    req_session_col = llm_requests.c.session_id if "session_id" in req_cols else None
    req_group_col = llm_requests.c.request_group_id if "request_group_id" in req_cols else None
    req_provider_col = llm_requests.c.provider if "provider" in req_cols else None
    req_model_col = llm_requests.c.model if "model" in req_cols else None
    req_requested_model_col = (
        llm_requests.c.requested_model if "requested_model" in req_cols else req_model_col
    )
    req_revision_root_col = (
        llm_requests.c.response_revision_root_id
        if "response_revision_root_id" in req_cols
        else None
    )
    req_revision_col = llm_requests.c.response_revision if "response_revision" in req_cols else None
    req_generation_profile_col = (
        llm_requests.c.generation_profile if "generation_profile" in req_cols else None
    )
    req_effective_max_col = (
        llm_requests.c.effective_max_output_tokens
        if "effective_max_output_tokens" in req_cols
        else None
    )
    req_effective_reasoning_mode_col = (
        llm_requests.c.effective_reasoning_mode
        if "effective_reasoning_mode" in req_cols
        else None
    )
    req_effective_reasoning_effort_col = (
        llm_requests.c.effective_reasoning_effort
        if "effective_reasoning_effort" in req_cols
        else None
    )
    req_generation_policy_version_col = (
        llm_requests.c.generation_policy_version
        if "generation_policy_version" in req_cols
        else None
    )

    resp_text_col = llm_responses.c.text if "text" in resp_cols else None
    resp_latency_col = llm_responses.c.latency_ms if "latency_ms" in resp_cols else None
    resp_prompt_tokens_col = llm_responses.c.prompt_tokens if "prompt_tokens" in resp_cols else None
    resp_completion_tokens_col = (
        llm_responses.c.completion_tokens if "completion_tokens" in resp_cols else None
    )
    resp_cached_input_tokens_col = (
        llm_responses.c.cached_input_tokens if "cached_input_tokens" in resp_cols else None
    )
    resp_cache_write_tokens_col = (
        llm_responses.c.cache_write_tokens if "cache_write_tokens" in resp_cols else None
    )
    resp_reasoning_tokens_col = (
        llm_responses.c.reasoning_tokens if "reasoning_tokens" in resp_cols else None
    )
    resp_pricing_snapshot_col = (
        llm_responses.c.pricing_snapshot if "pricing_snapshot" in resp_cols else None
    )
    resp_tokens_col = llm_responses.c.total_tokens if "total_tokens" in resp_cols else None
    resp_cost_col = llm_responses.c.estimated_cost if "estimated_cost" in resp_cols else None
    resp_error_col = llm_responses.c.error_message if "error_message" in resp_cols else None
    resp_error_type_col = llm_responses.c.error_type if "error_type" in resp_cols else None
    resp_completion_status_col = (
        llm_responses.c.completion_status if "completion_status" in resp_cols else None
    )
    resp_stop_cause_col = llm_responses.c.stop_cause if "stop_cause" in resp_cols else None
    resp_served_model_col = llm_responses.c.served_model if "served_model" in resp_cols else None
    resp_pricing_model_col = llm_responses.c.pricing_model if "pricing_model" in resp_cols else None
    resp_lifecycle_col = (
        llm_responses.c.model_lifecycle_status
        if "model_lifecycle_status" in resp_cols
        else None
    )
    routing_req_col = (
        routing_decisions.c.llm_request_id
        if routing_decisions is not None and "llm_request_id" in routing_cols
        else None
    )
    routing_trace_col = (
        routing_decisions.c.trace
        if routing_decisions is not None and "trace" in routing_cols
        else None
    )
    routing_mode_col = (
        routing_decisions.c.routing_mode
        if routing_decisions is not None and "routing_mode" in routing_cols
        else None
    )

    prompt_expr = req_prompt_col if req_prompt_col is not None else literal(None)
    prompt_hash_expr = req_prompt_hash_col if req_prompt_hash_col is not None else literal(None)
    session_expr = req_session_col if req_session_col is not None else literal(None)
    request_group_expr = req_group_col if req_group_col is not None else literal(None)
    provider_expr = req_provider_col if req_provider_col is not None else literal("unknown")
    model_expr = req_model_col if req_model_col is not None else literal("unknown")
    requested_model_expr = (
        req_requested_model_col if req_requested_model_col is not None else model_expr
    )
    mode_expr = req_mode_col if req_mode_col is not None else literal("chat")
    timestamp_expr = req_created_col if req_created_col is not None else literal(None)
    response_text_expr = resp_text_col if resp_text_col is not None else literal("")
    latency_expr = resp_latency_col if resp_latency_col is not None else literal(None)
    prompt_tokens_expr = (
        resp_prompt_tokens_col if resp_prompt_tokens_col is not None else literal(None)
    )
    completion_tokens_expr = (
        resp_completion_tokens_col if resp_completion_tokens_col is not None else literal(None)
    )
    cached_input_tokens_expr = (
        resp_cached_input_tokens_col
        if resp_cached_input_tokens_col is not None
        else literal(0)
    )
    cache_write_tokens_expr = (
        resp_cache_write_tokens_col
        if resp_cache_write_tokens_col is not None
        else literal(0)
    )
    reasoning_tokens_expr = (
        resp_reasoning_tokens_col if resp_reasoning_tokens_col is not None else literal(0)
    )
    pricing_snapshot_expr = (
        resp_pricing_snapshot_col if resp_pricing_snapshot_col is not None else literal(None)
    )
    tokens_expr = resp_tokens_col if resp_tokens_col is not None else literal(None)
    cost_expr = resp_cost_col if resp_cost_col is not None else literal(None)
    error_expr = resp_error_col if resp_error_col is not None else literal(None)
    error_code_expr = resp_error_type_col if resp_error_type_col is not None else literal(None)
    served_model_expr = resp_served_model_col if resp_served_model_col is not None else model_expr
    pricing_model_expr = (
        resp_pricing_model_col if resp_pricing_model_col is not None else served_model_expr
    )
    lifecycle_expr = resp_lifecycle_col if resp_lifecycle_col is not None else literal(None)
    alias_redirected_expr = (
        llm_responses.c.alias_redirected
        if "alias_redirected" in resp_cols
        else literal(False)
    )
    replacement_model_expr = (
        llm_responses.c.replacement_model
        if "replacement_model" in resp_cols
        else literal(None)
    )
    migration_reason_expr = (
        llm_responses.c.model_migration_reason
        if "model_migration_reason" in resp_cols
        else literal(None)
    )
    pricing_rule_expr = (
        llm_responses.c.pricing_rule_applied
        if "pricing_rule_applied" in resp_cols
        else literal(None)
    )
    pricing_version_expr = (
        llm_responses.c.pricing_version if "pricing_version" in resp_cols else literal(None)
    )
    pricing_unknown_expr = (
        llm_responses.c.pricing_unknown if "pricing_unknown" in resp_cols else literal(False)
    )
    routing_trace_expr = routing_trace_col if routing_trace_col is not None else literal(None)
    routing_mode_expr = routing_mode_col if routing_mode_col is not None else literal(None)
    session_title_expr = sessions.c.title if "title" in session_cols else literal(None)
    response_revision_root_expr = (
        req_revision_root_col if req_revision_root_col is not None else literal(None)
    )
    response_revision_expr = req_revision_col if req_revision_col is not None else literal(1)
    generation_profile_expr = (
        req_generation_profile_col if req_generation_profile_col is not None else literal(None)
    )
    effective_max_expr = (
        req_effective_max_col if req_effective_max_col is not None else literal(None)
    )
    effective_reasoning_mode_expr = (
        req_effective_reasoning_mode_col
        if req_effective_reasoning_mode_col is not None
        else literal(None)
    )
    effective_reasoning_effort_expr = (
        req_effective_reasoning_effort_col
        if req_effective_reasoning_effort_col is not None
        else literal(None)
    )
    generation_policy_version_expr = (
        req_generation_policy_version_col
        if req_generation_policy_version_col is not None
        else literal(None)
    )
    completion_status_expr = (
        resp_completion_status_col if resp_completion_status_col is not None else literal("complete")
    )
    stop_cause_expr = resp_stop_cause_col if resp_stop_cause_col is not None else literal("unknown")

    order_col = req_created_col if req_created_col is not None else llm_requests.c.id
    from_clause = llm_requests.outerjoin(
        llm_responses,
        llm_responses.c.llm_request_id == llm_requests.c.id,
    )
    if req_session_col is not None and "id" in session_cols:
        from_clause = from_clause.outerjoin(sessions, sessions.c.id == req_session_col)
    if routing_req_col is not None and routing_decisions is not None:
        from_clause = from_clause.outerjoin(
            routing_decisions,
            routing_req_col == llm_requests.c.id,
        )

    stmt = (
        select(
            llm_requests.c.id.label("request_pk"),
            llm_requests.c.request_id.label("request_id"),
            session_expr.label("session_id"),
            session_title_expr.label("session_title"),
            request_group_expr.label("request_group_id"),
            mode_expr.label("mode"),
            prompt_expr.label("prompt_text"),
            prompt_hash_expr.label("prompt_hash"),
            provider_expr.label("provider"),
            model_expr.label("model"),
            requested_model_expr.label("requested_model"),
            served_model_expr.label("served_model"),
            pricing_model_expr.label("pricing_model"),
            lifecycle_expr.label("model_lifecycle_status"),
            alias_redirected_expr.label("alias_redirected"),
            replacement_model_expr.label("replacement_model"),
            migration_reason_expr.label("migration_reason"),
            pricing_rule_expr.label("pricing_rule_applied"),
            pricing_version_expr.label("pricing_version"),
            pricing_unknown_expr.label("pricing_unknown"),
            response_revision_root_expr.label("response_revision_root_id"),
            response_revision_expr.label("response_revision"),
            generation_profile_expr.label("generation_profile"),
            effective_max_expr.label("effective_max_output_tokens"),
            effective_reasoning_mode_expr.label("effective_reasoning_mode"),
            effective_reasoning_effort_expr.label("effective_reasoning_effort"),
            generation_policy_version_expr.label("generation_policy_version"),
            completion_status_expr.label("completion_status"),
            stop_cause_expr.label("stop_cause"),
            timestamp_expr.label("created_at"),
            response_text_expr.label("response_text"),
            latency_expr.label("latency_ms"),
            prompt_tokens_expr.label("prompt_tokens"),
            cached_input_tokens_expr.label("cached_input_tokens"),
            cache_write_tokens_expr.label("cache_write_tokens"),
            reasoning_tokens_expr.label("reasoning_tokens"),
            pricing_snapshot_expr.label("pricing_snapshot"),
            completion_tokens_expr.label("completion_tokens"),
            tokens_expr.label("tokens"),
            cost_expr.label("cost"),
            error_expr.label("error_message"),
            error_code_expr.label("error_code"),
            routing_mode_expr.label("routing_mode"),
            routing_trace_expr.label("routing_trace"),
        )
        .select_from(from_clause)
        .where(llm_requests.c.user_id == user_id)
        .order_by(desc(order_col), desc(response_revision_expr))
        .limit(max(1, int(limit)) * 4)
    )

    if session_id and req_session_col is not None:
        parsed_session_id = coerce_uuid(session_id)
        if parsed_session_id is None:
            return []

        session_text = str(parsed_session_id).lower()
        session_col_text = func.lower(cast(req_session_col, String))
        stmt = stmt.where(
            or_(
                session_col_text == session_text,
                func.replace(session_col_text, "-", "") == parsed_session_id.hex.lower(),
            )
        )

    rows = db.execute(stmt).fetchall()
    entries: list[dict[str, Any]] = []
    seen_revision_roots: set[str] = set()

    for row in rows:
        payload = dict(row._mapping)
        revision_root = _revision_root_key(payload)
        if revision_root in seen_revision_roots:
            continue
        seen_revision_roots.add(revision_root)
        mode = str(payload.get("mode") or "chat").lower()
        if mode == "ask":
            mode = "chat"

        prompt_text = payload.get("prompt_text")
        if not prompt_text:
            prompt_hash = payload.get("prompt_hash")
            prompt_text = "[prompt not stored]"
            if prompt_hash:
                prompt_text = f"[prompt hash: {prompt_hash}]"

        response_text = payload.get("response_text") or ""
        if not response_text and payload.get("error_message"):
            response_text = f"[error] {payload['error_message']}"
        web_source_items = _extract_history_web_source_items(payload.get("routing_trace"))
        credit_snapshot = _extract_history_credit_snapshot(payload.get("routing_trace"))

        entries.append(
            {
                "id": _history_entry_id(payload.get("request_pk"), payload.get("request_id")),
                "request_id": str(payload.get("request_id") or ""),
                "session_id": (
                    str(payload["session_id"]) if payload.get("session_id") is not None else None
                ),
                "session_title": (
                    str(payload["session_title"])
                    if payload.get("session_title") is not None
                    else None
                ),
                "request_group_id": (
                    str(payload["request_group_id"])
                    if payload.get("request_group_id") is not None
                    else None
                ),
                "timestamp": _to_history_timestamp(payload.get("created_at")),
                "mode": mode,
                "prompt": str(prompt_text),
                "provider": str(payload.get("provider") or "unknown"),
                "model": str(payload.get("model") or "unknown"),
                "requested_model": str(
                    payload.get("requested_model") or payload.get("model") or "unknown"
                ),
                "served_model": str(payload.get("served_model") or payload.get("model") or "unknown"),
                "pricing_model": str(
                    payload.get("pricing_model")
                    or payload.get("served_model")
                    or payload.get("model")
                    or "unknown"
                ),
                "model_lifecycle_status": payload.get("model_lifecycle_status"),
                "alias_redirected": bool(payload.get("alias_redirected", False)),
                "replacement_model": payload.get("replacement_model"),
                "migration_reason": payload.get("migration_reason"),
                "pricing_rule_applied": payload.get("pricing_rule_applied"),
                "pricing_version": payload.get("pricing_version"),
                "pricing_unknown": bool(payload.get("pricing_unknown", False)),
                "response_version": int(payload.get("response_revision") or 1),
                "generation_profile": payload.get("generation_profile"),
                "effective_max_output_tokens": payload.get("effective_max_output_tokens"),
                "effective_reasoning_mode": payload.get("effective_reasoning_mode"),
                "effective_reasoning_effort": payload.get("effective_reasoning_effort"),
                "routing_mode": payload.get("routing_mode"),
                "generation_policy_version": payload.get("generation_policy_version"),
                "completion_status": str(payload.get("completion_status") or "complete"),
                "stop_cause": str(payload.get("stop_cause") or "unknown"),
                "response": str(response_text),
                "error_code": (
                    str(payload["error_code"])
                    if payload.get("error_code") is not None
                    else None
                ),
                "latency_ms": payload.get("latency_ms"),
                "prompt_tokens": payload.get("prompt_tokens"),
                "cached_input_tokens": payload.get("cached_input_tokens"),
                "cache_write_tokens": payload.get("cache_write_tokens"),
                "reasoning_tokens": payload.get("reasoning_tokens"),
                "pricing_snapshot": _decode_json_value(
                    payload.get("pricing_snapshot"), {}
                ),
                "completion_tokens": payload.get("completion_tokens"),
                "tokens": payload.get("tokens"),
                "cost": float(payload["cost"]) if payload.get("cost") is not None else None,
                "web_source_items": web_source_items,
                **credit_snapshot,
            }
        )
        if len(entries) >= max(1, int(limit)):
            break

    return entries


def rename_history_session(
    db: Session,
    user_id: UUID,
    session_id: str,
    title: str,
) -> bool:
    """Rename one user-owned chat session without changing its activity timestamp."""
    from db.tables import get_table

    parsed_session_id = coerce_uuid(session_id)
    normalized_title = str(title or "").strip()
    if parsed_session_id is None or not normalized_title:
        return False

    sessions = get_table("sessions")
    result = db.execute(
        update(sessions)
        .where(
            and_(
                sessions.c.id == parsed_session_id,
                sessions.c.user_id == user_id,
            )
        )
        .values(title=normalized_title)
    )
    return bool(result.rowcount and result.rowcount > 0)


def _find_request_pk_by_history_id(db: Session, user_id: UUID, entry_id: int) -> Any | None:
    """Map HistoryEntry.id back to llm_requests primary key for one user."""
    from db.tables import get_table

    llm_requests = get_table("llm_requests")
    stmt = select(llm_requests.c.id, llm_requests.c.request_id).where(
        llm_requests.c.user_id == user_id
    )
    rows = db.execute(stmt).fetchall()
    for row in rows:
        request_pk = row._mapping["id"]
        request_id = row._mapping.get("request_id")
        if _history_entry_id(request_pk, request_id) == int(entry_id):
            return request_pk
    return None


def delete_llm_history_entry(db: Session, user_id: UUID, entry_id: int) -> bool:
    """
    Delete one history entry (llm request + response + routing telemetry) for a user.
    """
    from db.tables import get_table

    llm_requests = get_table("llm_requests")
    llm_responses = get_table("llm_responses")
    routing_decisions = get_table("routing_decisions")
    routing_attempts = get_table("routing_attempts")
    cortex_analysis_runs = get_table("cortex_analysis_runs")

    request_pk = _find_request_pk_by_history_id(db, user_id, entry_id)
    if request_pk is None:
        return False

    req_cols = {col.name for col in llm_requests.columns}
    source_row = db.execute(
        select(
            llm_requests.c.request_group_id.label("request_group_id"),
            (
                llm_requests.c.response_revision_root_id
                if "response_revision_root_id" in req_cols
                else literal(None)
            ).label("response_revision_root_id"),
        ).where(llm_requests.c.id == request_pk)
    ).fetchone()
    source_payload = dict(source_row._mapping) if source_row is not None else {}
    revision_root = source_payload.get("response_revision_root_id") or request_pk
    if "response_revision_root_id" in req_cols:
        request_family_filter = or_(
            llm_requests.c.id == revision_root,
            llm_requests.c.response_revision_root_id == revision_root,
        )
    else:
        request_family_filter = llm_requests.c.id == request_pk
    request_family_ids = select(llm_requests.c.id).where(
        and_(llm_requests.c.user_id == user_id, request_family_filter)
    )

    decision_ids_subq = select(routing_decisions.c.id).where(
        routing_decisions.c.llm_request_id.in_(request_family_ids)
    )
    db.execute(
        delete(routing_attempts).where(
            routing_attempts.c.routing_decision_id.in_(decision_ids_subq)
        )
    )
    db.execute(
        delete(routing_decisions).where(routing_decisions.c.llm_request_id.in_(request_family_ids))
    )
    db.execute(delete(llm_responses).where(llm_responses.c.llm_request_id.in_(request_family_ids)))
    result = db.execute(
        delete(llm_requests).where(and_(llm_requests.c.user_id == user_id, request_family_filter))
    )
    request_group_id = source_payload.get("request_group_id")
    if request_group_id is not None:
        remaining = db.execute(
            select(func.count())
            .select_from(llm_requests)
            .where(
                and_(
                    llm_requests.c.user_id == user_id,
                    llm_requests.c.request_group_id == request_group_id,
                )
            )
        ).scalar_one()
        if int(remaining or 0) == 0:
            db.execute(
                delete(cortex_analysis_runs).where(
                    and_(
                        cortex_analysis_runs.c.user_id == user_id,
                        cortex_analysis_runs.c.request_group_id == request_group_id,
                    )
                )
            )
    return bool(result.rowcount and result.rowcount > 0)


def clear_llm_history(db: Session, user_id: UUID, session_id: str | None = None) -> int:
    """
    Clear history entries for a user from llm/routing audit tables.

    When session_id is provided, only entries in that user-visible thread are
    deleted. Invalid session IDs are treated as an empty match.
    """
    from db.tables import get_table

    llm_requests = get_table("llm_requests")
    llm_responses = get_table("llm_responses")
    routing_decisions = get_table("routing_decisions")
    routing_attempts = get_table("routing_attempts")
    cortex_analysis_runs = get_table("cortex_analysis_runs")

    filters = [llm_requests.c.user_id == user_id]
    req_cols = {col.name for col in llm_requests.columns}
    if session_id:
        if "session_id" not in req_cols:
            return 0
        parsed_session_id = coerce_uuid(session_id)
        if parsed_session_id is None:
            return 0
        session_text = str(parsed_session_id).lower()
        session_col_text = func.lower(cast(llm_requests.c.session_id, String))
        filters.append(
            or_(
                session_col_text == session_text,
                func.replace(session_col_text, "-", "") == parsed_session_id.hex.lower(),
            )
        )

    user_request_ids_subq = select(llm_requests.c.id).where(and_(*filters))
    decision_ids_subq = select(routing_decisions.c.id).where(
        routing_decisions.c.llm_request_id.in_(user_request_ids_subq)
    )

    analysis_filters = [cortex_analysis_runs.c.user_id == user_id]
    if session_id:
        analysis_filters.append(cortex_analysis_runs.c.session_id == parsed_session_id)
    db.execute(delete(cortex_analysis_runs).where(and_(*analysis_filters)))
    db.execute(
        delete(routing_attempts).where(
            routing_attempts.c.routing_decision_id.in_(decision_ids_subq)
        )
    )
    db.execute(
        delete(routing_decisions).where(
            routing_decisions.c.llm_request_id.in_(user_request_ids_subq)
        )
    )
    db.execute(
        delete(llm_responses).where(llm_responses.c.llm_request_id.in_(user_request_ids_subq))
    )
    deleted = db.execute(delete(llm_requests).where(and_(*filters)))
    return int(deleted.rowcount or 0)


# ============================================================================
# USER PREFERENCES
# ============================================================================


def get_user_preferences(db: Session, user_id: UUID) -> dict[str, Any] | None:
    """
    Get user preferences.

    Args:
        db: Database session
        user_id: User ID

    Returns:
        dict: Preferences or None if not set
    """
    from db.tables import get_table

    user_preferences = get_table("user_preferences")

    stmt = select(user_preferences).where(user_preferences.c.user_id == user_id)

    result = db.execute(stmt).first()

    if result:
        return dict(result._mapping)
    return None


# ============================================================================
# COMPARE MODE
# ============================================================================


def save_compare_summary(
    db: Session, session_id: UUID, responses: list[UnifiedResponse], selected_model_index: int = 0
) -> UUID:
    """
    Build and save a compare summary message from multiple model responses.

    CRITICAL COMPARE MODE PERSISTENCE:
    - N llm_requests + llm_responses (one per model) - already persisted separately
    - ONE assistant message in messages table (this function)
    - Summary includes selected answer for conversation continuity
    - Compact results for all models with request IDs for traceability

    Args:
        db: Database session
        session_id: Session ID
        responses: List of UnifiedResponse objects from different models
        selected_model_index: Index of response to use as "selected answer" (default: 0)

    Returns:
        UUID: message_id of the assistant summary

    Note:
        Does NOT commit. Caller must commit.

    Summary Format:
        **Selected Answer:** (from Model A)
        [Answer text]

        **Comparison Results:**
        1. Model A: [cost, tokens, latency, request_id]
           [preview/full text]
        2. Model B: [cost, tokens, latency, request_id]
           [preview/full text]
    """
    if not responses:
        raise ValueError("Cannot create compare summary from empty responses list")

    # Validate index
    if selected_model_index >= len(responses):
        selected_model_index = 0

    selected = responses[selected_model_index]

    # Build summary in structured markdown format
    summary_parts = []

    # Part 1: Selected answer (for conversation continuity)
    summary_parts.append(
        f"**Selected Answer** (from {selected.provider.title()} {selected.model}):"
    )
    summary_parts.append(f"{selected.text}\n")
    summary_parts.append("---\n")

    # Part 2: Compact comparison results
    summary_parts.append("**Comparison Results:**\n")

    for i, resp in enumerate(responses, 1):
        # Model header with compact stats
        is_selected = (i - 1) == selected_model_index
        selected_marker = " ✓" if is_selected else ""

        summary_parts.append(f"\n**{i}. {resp.provider.title()} - {resp.model}{selected_marker}**")
        summary_parts.append(
            f"_Cost: ${resp.estimated_cost:.6f} | "
            f"Tokens: {resp.token_usage.total_tokens} | "
            f"Latency: {resp.latency_ms}ms | "
            f"Request ID: `{resp.request_id[:8]}...`_\n"
        )

        # Response preview (full text if selected, preview otherwise)
        if is_selected:
            summary_parts.append("(See selected answer above)")
        else:
            # Show preview (first 200 chars)
            preview = resp.text[:200] + "..." if len(resp.text) > 200 else resp.text
            summary_parts.append(f"{preview}")

        summary_parts.append("\n---")

    summary = "\n".join(summary_parts)

    # Save as single assistant message
    return save_message(db, session_id, role="assistant", content=summary)
