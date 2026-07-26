from __future__ import annotations

import pwd
from typing import Any

from .config import LegacyImport
from .database import Database
from .process_monitor import is_primary_user


def list_system_login_users() -> list[tuple[str, int]]:
    users: list[tuple[str, int]] = []
    for entry in pwd.getpwall():
        if is_primary_user(entry.pw_name, entry.pw_uid):
            users.append((entry.pw_name, entry.pw_uid))
    return sorted(users, key=lambda item: (item[1], item[0]))


def sync_user_settings(database: Database) -> dict[str, Any]:
    return database.sync_user_settings(list_system_login_users())


def apply_legacy_user_import(
    database: Database, legacy: LegacyImport | None
) -> None:
    if legacy is None:
        return
    database.apply_legacy_user_import(
        legacy.users, legacy.admin_emails, legacy.process_end_users
    )
