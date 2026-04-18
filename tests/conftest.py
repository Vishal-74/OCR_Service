"""Shared test fixtures + env stubbing.

The production `app` module does eager env validation and builds OpenAI /
Supabase clients at import time. For unit tests we stub everything before
`app` is ever imported, and replace the module's global `supabase`,
`openai_client`, and HTTP helpers with fakes.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest


# Make the service root importable.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------- required env (must be set before `import app`) ----------
os.environ.setdefault("OCR_SHARED_SECRET", "test-secret")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault(
    "SUPABASE_SERVICE_ROLE_KEY",
    # Shape-only fake JWT — the supabase-py client validates the format at
    # init time, but we replace the `supabase` global with a fake below.
    "aaaa.bbbb.cccc",
)
os.environ.setdefault("OCR_SESSION_TTL_MIN", "30")


# ---------- Fake Supabase client ----------

class _FakeQuery:
    def __init__(self, store, table_name):
        self._store = store
        self._table = table_name
        self._op = None
        self._filters = []
        self._payload = None
        self._on_conflict = None
        self._limit = None

    # select / delete / upsert entry points
    def select(self, *_cols):
        self._op = "select"
        return self

    def delete(self):
        self._op = "delete"
        return self

    def upsert(self, payload, on_conflict=None):
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def _matches(self, row):
        for col, val in self._filters:
            if row.get(col) != val:
                return False
        return True

    def execute(self):
        table = self._store.setdefault(self._table, [])

        if self._op == "select":
            rows = [r for r in table if self._matches(r)]
            if self._limit is not None:
                rows = rows[: self._limit]
            return types.SimpleNamespace(data=rows)

        if self._op == "delete":
            kept, removed = [], []
            for r in table:
                (removed if self._matches(r) else kept).append(r)
            self._store[self._table] = kept
            return types.SimpleNamespace(data=removed)

        if self._op == "upsert":
            payload = self._payload or {}
            # naive on_conflict: match all conflict columns
            conflict_cols = [
                c.strip() for c in (self._on_conflict or "").split(",") if c.strip()
            ]
            idx = None
            if conflict_cols:
                for i, r in enumerate(table):
                    if all(r.get(c) == payload.get(c) for c in conflict_cols):
                        idx = i
                        break
            if idx is not None:
                table[idx] = {**table[idx], **payload}
            else:
                table.append({**payload})
            return types.SimpleNamespace(data=[payload])

        if self._op == "insert":
            payload = self._payload or {}
            table.append({**payload})
            return types.SimpleNamespace(data=[payload])

        raise RuntimeError(f"Unknown op: {self._op}")


class FakeSupabase:
    def __init__(self):
        self._store: dict = {}

    def table(self, name):
        return _FakeQuery(self._store, name)

    # test helpers
    def seed_whatsapp_config(
        self,
        phone_number_id="PNI-123",
        seller_id="seller_42",
        access_token="META_TOKEN",
    ):
        self._store.setdefault("whatsapp_config", []).append(
            {
                "seller_id": seller_id,
                "access_token": access_token,
                "phone_number_id": phone_number_id,
            }
        )

    def ocr_sessions(self):
        return list(self._store.get("ocr_sessions", []))


# ---------- Fake OpenAI client ----------

class _FakeMsg:
    def __init__(self, content):
        self.message = types.SimpleNamespace(content=content)


class _FakeCompletion:
    def __init__(self, content):
        self.choices = [_FakeMsg(content)]


class _FakeChatCompletions:
    def __init__(self):
        self.next_content: str = "[]"
        # If set, each create() pops the next string (for multi-step vision flows).
        self.queue: list[str] = []
        self.last_messages = None
        self.call_count = 0

    def create(self, *, model, messages, **_kwargs):
        self.call_count += 1
        self.last_messages = messages
        if self.queue:
            content = self.queue.pop(0)
        else:
            content = self.next_content
        return _FakeCompletion(content)


class _FakeAudio:
    def __init__(self):
        self.next_text = ""

    @property
    def transcriptions(self):
        return self

    def create(self, *, model, file, **_kwargs):
        return types.SimpleNamespace(text=self.next_text)


class FakeOpenAI:
    def __init__(self):
        self._chat = _FakeChatCompletions()
        self._audio = _FakeAudio()

    @property
    def chat(self):
        return types.SimpleNamespace(completions=self._chat)

    @property
    def audio(self):
        return self._audio


# ---------- pytest fixtures ----------

@pytest.fixture()
def fake_supabase():
    return FakeSupabase()


@pytest.fixture()
def fake_openai():
    return FakeOpenAI()


@pytest.fixture()
def app_module(monkeypatch, fake_supabase, fake_openai):
    """Import the Flask app freshly with fakes injected."""
    # Remove any cached import.
    sys.modules.pop("app", None)
    import app as _app  # noqa: WPS433 (lazy import on purpose)

    monkeypatch.setattr(_app, "supabase", fake_supabase)
    monkeypatch.setattr(_app, "openai_client", fake_openai)

    # Stub network effects.
    sent = []

    def _fake_send_wa_text(phone_number_id, access_token, to, body):
        sent.append(
            {"phone_number_id": phone_number_id, "to": to, "body": body}
        )

    def _fake_download_media(media_id, access_token, phone_number_id=None):
        return b"\x00\x00", "image/jpeg"

    monkeypatch.setattr(_app, "_send_wa_text", _fake_send_wa_text)
    monkeypatch.setattr(_app, "_download_meta_media", _fake_download_media)

    _app.app.testing = True
    _app._sent_messages = sent  # expose to tests
    return _app


@pytest.fixture()
def client(app_module):
    return app_module.app.test_client()
