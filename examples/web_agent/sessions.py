from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from agents import (
    OpenAIResponsesCompactionSession,
    SQLiteSession,
    is_openai_responses_compaction_aware_session,
)
from agents.items import TResponseInputItem
from agents.memory.session import Session

from .config import CompactionConfig, WebAgentConfig

SerializedInterruption = dict[str, Any]


@dataclass
class SessionMetadata:
    session_id: str
    title: str
    created_at: str
    updated_at: str


class WebAgentSessionStore:
    def __init__(self, config: WebAgentConfig):
        self.config = config
        self.data_dir = config.data_dir
        self.db_path = config.resolved_sessions_db
        self.metadata_path = self.data_dir / "sessions.json"
        self.pending_dir = self.data_dir / "pending"
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}
        self._metadata = self._load_metadata()

        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def list_sessions(self) -> list[SessionMetadata]:
        with self._lock:
            sessions = list(self._metadata.values())
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def create_session(self, title: str | None = None) -> SessionMetadata:
        session_id = self._new_session_id()
        now = _utc_now()
        metadata = SessionMetadata(
            session_id=session_id,
            title=title or "New chat",
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._metadata[session_id] = metadata
            self._save_metadata()
        return metadata

    def get_or_create_metadata(
        self,
        session_id: str | None,
        *,
        title: str | None = None,
    ) -> SessionMetadata:
        if session_id:
            with self._lock:
                metadata = self._metadata.get(session_id)
            if metadata is not None:
                return metadata

        return self.create_session(title=title)

    def touch_session(self, session_id: str, *, title: str | None = None) -> SessionMetadata:
        with self._lock:
            metadata = self._metadata.get(session_id)
            if metadata is None:
                metadata = SessionMetadata(
                    session_id=session_id,
                    title=title or "New chat",
                    created_at=_utc_now(),
                    updated_at=_utc_now(),
                )
                self._metadata[session_id] = metadata
            if title and metadata.title == "New chat":
                metadata.title = _title_from_message(title)
            metadata.updated_at = _utc_now()
            self._save_metadata()
            return metadata

    async def get_session(self, session_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                return session

            session = self._build_session(session_id)
            self._sessions[session_id] = session
            return session

    async def get_items(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> list[TResponseInputItem]:
        session = await self.get_session(session_id)
        return await session.get_items(limit=limit)

    async def clear_session(self, session_id: str) -> bool:
        session = await self.get_session(session_id)
        await session.clear_session()
        with self._lock:
            existed = session_id in self._metadata
            self._metadata.pop(session_id, None)
            self._sessions.pop(session_id, None)
            self._save_metadata()
        self.clear_pending_state(session_id)
        close = getattr(session, "close", None)
        if callable(close):
            close()
        underlying = getattr(session, "underlying_session", None)
        underlying_close = getattr(underlying, "close", None)
        if callable(underlying_close):
            underlying_close()
        return existed

    async def compact_session(self, session_id: str, *, force: bool = True) -> bool:
        session = await self.get_session(session_id)
        if not is_openai_responses_compaction_aware_session(session):
            return False
        await session.run_compaction(
            {"force": force, "compaction_mode": self.config.compaction.mode}
        )
        self.touch_session(session_id)
        return True

    def save_pending_state(
        self,
        session_id: str,
        *,
        state_json: dict[str, Any],
        interruptions: list[SerializedInterruption],
    ) -> None:
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "session_id": session_id,
            "state": state_json,
            "interruptions": interruptions,
            "updated_at": _utc_now(),
        }
        with self._pending_path(session_id).open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, default=str)

    def load_pending_state(self, session_id: str) -> dict[str, Any] | None:
        path = self._pending_path(session_id)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def clear_pending_state(self, session_id: str) -> None:
        path = self._pending_path(session_id)
        if path.exists():
            path.unlink()

    def _build_session(self, session_id: str) -> Session:
        underlying = SQLiteSession(session_id, self.db_path)
        compaction = self.config.compaction
        if not compaction.enabled or self.config.model_api == "chat_completions":
            return underlying

        return OpenAIResponsesCompactionSession(
            session_id=session_id,
            underlying_session=underlying,
            model=compaction.model,
            compaction_mode=compaction.mode,
            should_trigger_compaction=_build_compaction_trigger(compaction),
        )

    def _load_metadata(self) -> dict[str, SessionMetadata]:
        if not self.metadata_path.exists():
            return {}
        try:
            with self.metadata_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, list):
            return {}

        loaded: dict[str, SessionMetadata] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                metadata = SessionMetadata(
                    session_id=str(item["session_id"]),
                    title=str(item.get("title") or "New chat"),
                    created_at=str(item["created_at"]),
                    updated_at=str(item["updated_at"]),
                )
            except KeyError:
                continue
            loaded[metadata.session_id] = metadata
        return loaded

    def _save_metadata(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        sessions = sorted(
            self._metadata.values(),
            key=lambda item: item.updated_at,
            reverse=True,
        )
        payload = [asdict(item) for item in sessions]
        with self.metadata_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    def _new_session_id(self) -> str:
        while True:
            session_id = f"session_{uuid4().hex[:12]}"
            if session_id not in self._metadata:
                return session_id

    def _pending_path(self, session_id: str) -> Path:
        safe_name = "".join(
            char if char.isalnum() or char in {"_", "-"} else "_" for char in session_id
        )
        return self.pending_dir / f"{safe_name}.json"


def _build_compaction_trigger(compaction: CompactionConfig):
    def should_trigger(context: dict[str, Any]) -> bool:
        if not compaction.auto:
            return False
        candidates = context.get("compaction_candidate_items", [])
        return isinstance(candidates, list) and len(candidates) >= compaction.candidate_threshold

    return should_trigger


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _title_from_message(message: str) -> str:
    compact = " ".join(message.strip().split())
    if not compact:
        return "New chat"
    return compact[:48]
