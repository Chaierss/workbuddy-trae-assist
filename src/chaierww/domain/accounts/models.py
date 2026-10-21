from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum


class CredentialState(StrEnum):
    USABLE = "usable"
    UNVERIFIED = "unverified"
    RELOGIN_REQUIRED = "relogin_required"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    scope: str
    external_id: str
    display_name: str
    note: str
    avatar_path: str | None
    source: str
    credential_state: CredentialState
    last_used_at: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Account:
        return cls(
            id=row["id"],
            scope=row["scope"],
            external_id=row["external_id"],
            display_name=row["display_name"],
            note=row["note"],
            avatar_path=row["avatar_path"],
            source=row["source"],
            credential_state=CredentialState(row["credential_state"]),
            last_used_at=row["last_used_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            deleted_at=row["deleted_at"],
        )


@dataclass(frozen=True, slots=True)
class UpsertResult:
    created: bool
    restored: bool
    account: Account
