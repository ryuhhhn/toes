"""Versioned Agent Profile persistence.

data/profiles/{merchant_id}/v{n}.json plus a current.json pointer holding both the latest
version and the latest *approved* version. Keeping those separate is what lets a merchant
re-ingest a catalog without the agent immediately serving an unreviewed draft.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.audit import record
from app.config import get_settings
from app.models.profile import AgentProfile

log = logging.getLogger(__name__)

POINTER_FILE = "current.json"


class ProfileStore:
    def __init__(self, root: Path | None = None):
        self._root = root or get_settings().profiles_dir

    # --- paths ---------------------------------------------------------------

    def _dir(self, merchant_id: str) -> Path:
        return self._root / merchant_id

    def _pointer_path(self, merchant_id: str) -> Path:
        return self._dir(merchant_id) / POINTER_FILE

    def _version_path(self, merchant_id: str, version: int) -> Path:
        return self._dir(merchant_id) / f"v{version}.json"

    # --- reads ---------------------------------------------------------------

    def list_versions(self, merchant_id: str) -> list[int]:
        directory = self._dir(merchant_id)
        if not directory.exists():
            return []
        versions = []
        for path in directory.glob("v*.json"):
            try:
                versions.append(int(path.stem.lstrip("v")))
            except ValueError:
                continue
        return sorted(versions)

    def _pointer(self, merchant_id: str) -> dict:
        path = self._pointer_path(merchant_id)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def load_version(self, merchant_id: str, version: int) -> AgentProfile | None:
        path = self._version_path(merchant_id, version)
        if not path.exists():
            return None
        try:
            return AgentProfile.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.error("could not read profile %s v%s: %s", merchant_id, version, exc)
            return None

    def load(self, merchant_id: str) -> AgentProfile | None:
        """The newest profile, approved or not. For the approval screen."""
        version = self._pointer(merchant_id).get("version")
        if version is None:
            versions = self.list_versions(merchant_id)
            if not versions:
                return None
            version = versions[-1]
        return self.load_version(merchant_id, version)

    def load_approved(self, merchant_id: str) -> AgentProfile | None:
        """What the agent is allowed to serve. None means fall back to bootstrap."""
        version = self._pointer(merchant_id).get("approved_version")
        return self.load_version(merchant_id, version) if version is not None else None

    def load_for_runtime(self, merchant_id: str) -> AgentProfile | None:
        """Approved if it exists, otherwise the latest draft so a demo is never blocked."""
        return self.load_approved(merchant_id) or self.load(merchant_id)

    def next_version(self, merchant_id: str) -> int:
        versions = self.list_versions(merchant_id)
        return (versions[-1] + 1) if versions else 1

    def list_merchants(self) -> list[str]:
        if not self._root.exists():
            return []
        return sorted(p.name for p in self._root.iterdir() if p.is_dir())

    # --- writes --------------------------------------------------------------

    def save(self, profile: AgentProfile) -> AgentProfile:
        directory = self._dir(profile.merchant_id)
        directory.mkdir(parents=True, exist_ok=True)

        path = self._version_path(profile.merchant_id, profile.version)
        path.write_text(
            profile.model_dump_json(indent=2, by_alias=True), encoding="utf-8"
        )

        pointer = self._pointer(profile.merchant_id)
        pointer["version"] = profile.version
        if profile.status == "approved":
            pointer["approved_version"] = profile.version
        self._pointer_path(profile.merchant_id).write_text(
            json.dumps(pointer, indent=2), encoding="utf-8"
        )

        log.info(
            "saved profile %s v%s (%s)", profile.merchant_id, profile.version, profile.status
        )
        return profile

    def approve(
        self, profile: AgentProfile, *, approved_by: str, edited_fields: list[str] | None = None
    ) -> AgentProfile:
        approved = profile.model_copy(deep=True)
        approved.status = "approved"
        approved.approved_by = approved_by
        approved.approved_at = datetime.now(timezone.utc)
        if edited_fields:
            approved.edited_fields = sorted(set(approved.edited_fields) | set(edited_fields))

        self.save(approved)
        record(
            "profile_approved",
            merchant_id=approved.merchant_id,
            version=approved.version,
            approved_by=approved_by,
            category=approved.category,
            edited_fields=approved.edited_fields,
            approved_rules=[r.message for r in approved.approved_rules()],
        )
        return approved


_store: ProfileStore | None = None


def get_profile_store() -> ProfileStore:
    global _store
    if _store is None:
        _store = ProfileStore()
    return _store
