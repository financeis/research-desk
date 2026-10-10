"""Flows service (수급): the DB preparation and the run records other features read.

Readiness. The DB is prepared on first use and kept for the life of the process:
``core.settings.load_env()``, then SUPABASE_URL and SUPABASE_SERVICE_KEY →
``core.db.supabase_client``. Without them: ``NotReady("수급", "DB 접속 설정(SUPABASE_URL,
SUPABASE_SERVICE_KEY)이 없습니다")``. A failed preparation is not remembered, so values added to
``.env`` count on the next call. Creating the service reads nothing; the preparation runs under a
lock, so it happens once however many threads ask at the same time.

Light imports only: no FastAPI here, and ``core.db`` (supabase-py, and with it an HTTP library) is
imported when the DB is prepared, never when this module is, because ``cli.py`` imports the flows
window, and with it this module, for every command. ``flows update`` opens its own client with
``connect()``.

No run yet is not a readiness problem: ``latest_run`` answers None.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from threading import Lock
from typing import Any, Optional

from research_desk.core import settings as core_settings
from research_desk.core.settings import NotReady

from . import logic, store

AREA = "수급"
DB_NOT_CONFIGURED = "DB 접속 설정(SUPABASE_URL, SUPABASE_SERVICE_KEY)이 없습니다"


def db_settings() -> tuple[str, str]:
    """``(SUPABASE_URL, SUPABASE_SERVICE_KEY)`` now; NotReady("수급", …) when either is missing."""
    url, key = core_settings.supabase_url(), core_settings.supabase_service_key()
    if not (url and key):
        raise NotReady(AREA, DB_NOT_CONFIGURED)
    return url, key


def connect() -> Any:
    """Re-read ``.env``, then open the Supabase REST client. NotReady without the DB settings."""
    core_settings.load_env()
    url, key = db_settings()
    from research_desk.core import db   # supabase-py loads only when the DB is used

    return db.supabase_client(url, key)


def instant(value: Any) -> datetime:
    """A DB time (ISO text, or a datetime) as an aware datetime in UTC; no offset means UTC."""
    moment = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def day(value: Any) -> Optional[date]:
    """A DB date (``YYYY-MM-DD`` text, or a date) as a date; None stays None."""
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


class FlowsService:
    """Flows reads for one process. Creating it reads nothing; the first read prepares the DB."""

    def __init__(self) -> None:
        self._client: Any = None
        self._prepare_lock = Lock()

    def client(self) -> Any:
        """The Supabase REST client. Prepares it now if needed; NotReady without DB settings."""
        client = self._client
        if client is None:
            with self._prepare_lock:
                client = self._client
                if client is None:
                    client = self._client = connect()
        return client

    def latest_run(self) -> Optional[dict]:
        """The latest run's start and status with the latest successful run's as_of; None without
        any run."""
        sb = self.client()
        last = store.latest_run(sb)
        if last is None:
            return None
        success = last if last["status"] in logic.SUCCESS_STATUSES else store.latest_successful_run(sb)
        return {
            "last_run_at": instant(last["started_at"]),
            "last_run_status": last["status"],
            "as_of": day(success["as_of"]) if success is not None else None,
        }


# ── the process-wide service and the window functions ────────────────────────

_service: Optional[FlowsService] = None
_service_lock = Lock()


def get_service() -> FlowsService:
    """The process-wide service, created on first use (creating it reads nothing)."""
    global _service
    with _service_lock:
        if _service is None:
            _service = FlowsService()
        return _service


def latest_run() -> Optional[dict]:
    """See the window (``research_desk.features.flows``)."""
    return get_service().latest_run()
