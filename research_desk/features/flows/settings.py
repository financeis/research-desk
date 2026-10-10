"""Flows settings (수급): the KIS account and the KIS call pace of ``flows update``.

Read when called, never at import (callers run ``core.settings.load_env()`` first). The DB settings
are the shared ones (``core.settings.supabase_url()`` / ``supabase_service_key()``), and so is the
KIS account: the price update and this command read the same variables through
``core.settings.kis_app_key()`` / ``kis_app_secret()`` / ``kis_base_url()`` / ``kis_token_cache()``.
Only the call pace is a flows setting.

- ``KIS_APP_KEY`` / ``KIS_APP_SECRET``: the 실전 account's app key and secret. An empty value
  counts as unset. Both are needed (``flows update`` stops with exit code 4 without them).
- ``KIS_BASE_URL``: default the real service (실전 서비스) address.
- ``FLOWS_MAX_CALLS_PER_SEC``: KIS calls started per second at most, default 10. A value that is
  not a number above 0 is a ValueError (a general error, exit 1, not "not ready").
- ``KIS_TOKEN_CACHE``: the access-token file the price update and the user's other project share
  (one token a day for the app key). Unset: the token stays in memory and every run asks KIS.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from research_desk.core import settings as core_settings

DEFAULT_KIS_BASE_URL = core_settings.DEFAULT_KIS_BASE_URL
DEFAULT_MAX_CALLS_PER_SEC = 10.0


@dataclass(frozen=True)
class FlowsSettings:
    app_key: Optional[str] = field(repr=False)      # KIS_APP_KEY; never shown in the text of the settings
    app_secret: Optional[str] = field(repr=False)   # KIS_APP_SECRET
    base_url: str                                   # KIS_BASE_URL
    max_calls_per_sec: float                        # FLOWS_MAX_CALLS_PER_SEC
    token_cache: Optional[str] = None               # KIS_TOKEN_CACHE

    @property
    def has_kis_keys(self) -> bool:
        return bool(self.app_key and self.app_secret)


def app_key() -> Optional[str]:
    return core_settings.kis_app_key()


def app_secret() -> Optional[str]:
    return core_settings.kis_app_secret()


def base_url() -> str:
    return core_settings.kis_base_url()


def max_calls_per_sec() -> float:
    """``FLOWS_MAX_CALLS_PER_SEC`` (default 10); ValueError unless it is a number above 0."""
    rate = core_settings.get_float("FLOWS_MAX_CALLS_PER_SEC", DEFAULT_MAX_CALLS_PER_SEC)
    if not (math.isfinite(rate) and rate > 0):
        raise ValueError(f"FLOWS_MAX_CALLS_PER_SEC must be a number above 0, got {rate!r}")
    return rate


def token_cache() -> Optional[str]:
    return core_settings.kis_token_cache()


def load_settings() -> FlowsSettings:
    """Current values from the process environment."""
    return FlowsSettings(app_key=app_key(), app_secret=app_secret(), base_url=base_url(),
                         max_calls_per_sec=max_calls_per_sec(), token_cache=token_cache())
