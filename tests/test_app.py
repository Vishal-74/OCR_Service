"""End-to-end-ish tests for the OCR service without external services."""

from __future__ import annotations

import json


AUTH = {"x-internal-secret": "test-secret"}


def _image_msg(phone="+15550001111", msg_id="wamid.IMG1", pni="PNI-123"):
    return {
        "metadata": {"phone_number_id": pni},
        "messages": [
            {
                "from": phone,
                "id": msg_id,
                "type": "image",
                "image": {"id": "MEDIA-1", "mime_type": "image/jpeg"},
            }
        ],
        "mode": "order",
    }


def _text_msg(text, phone="+15550001111", msg_id="wamid.T1", pni="PNI-123", mode="order"):
    return {
        "metadata": {"phone_number_id": pni},
        "messages": [
            {
                "from": phone,
                "id": msg_id,
                "type": "text",
                "text": {"body": text},
            }
        ],
        "mode": mode,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.get_json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def test_ingest_requires_auth(client):
    r = client.post("/v1/whatsapp/ingest", json={})
    assert r.status_code == 401


def test_ingest_accepts_bearer(app_module, client, fake_supabase):
    fake_supabase.seed_whatsapp_config()
    r = client.post(
        "/v1/whatsapp/ingest",
        json=_text_msg("hello"),
        headers={"Authorization": "Bearer test-secret"},
    )
    assert r.status_code == 200
    assert r.get_json()["handled"] is False


# ---------------------------------------------------------------------------
# Unknown seller -> fail open
# ---------------------------------------------------------------------------

def test_unknown_seller_fails_open(client):
    r = client.post(
        "/v1/whatsapp/ingest",
        json=_text_msg("hi", pni="UNKNOWN"),
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["handled"] is False
    assert body["reason"] == "seller_not_found"


# ---------------------------------------------------------------------------
# Image flow: start OCR
# ---------------------------------------------------------------------------

def test_image_starts_ocr_session_and_sends_list(app_module, client, fake_supabase, fake_openai):
    fake_supabase.seed_whatsapp_config()
    fake_openai._chat.next_content = json.dumps(
        [
            {"name": "Milk", "quantity": "1L"},
            {"name": "Bread", "quantity": ""},
        ]
    )

    r = client.post("/v1/whatsapp/ingest", json=_image_msg(), headers=AUTH)
    body = r.get_json()
    assert r.status_code == 200
    assert body == {"handled": True, "done": False}

    sessions = fake_supabase.ocr_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s["state"] == "awaiting_confirmation"
    assert s["resume_mode"] == "order"
    assert [i["name"] for i in s["items_json"]] == ["Milk", "Bread"]

    sent = app_module._sent_messages
    assert len(sent) == 1
    assert "Milk" in sent[0]["body"] and "Reply *1*" in sent[0]["body"]


def test_image_with_no_items_sends_retry_message(app_module, client, fake_supabase, fake_openai):
    fake_supabase.seed_whatsapp_config()
    fake_openai._chat.next_content = "[]"

    r = client.post("/v1/whatsapp/ingest", json=_image_msg(), headers=AUTH)
    assert r.status_code == 200
    body = r.get_json()
    assert body["handled"] is True
    assert body["done"] is False
    assert body["reason"] == "no_items"
    assert fake_supabase.ocr_sessions() == []


# ---------------------------------------------------------------------------
# Duplicate delivery idempotency
# ---------------------------------------------------------------------------

def test_duplicate_message_is_idempotent(app_module, client, fake_supabase, fake_openai):
    fake_supabase.seed_whatsapp_config()
    fake_openai._chat.next_content = json.dumps([{"name": "Eggs", "quantity": "6"}])

    p = _image_msg(msg_id="wamid.DUP")
    r1 = client.post("/v1/whatsapp/ingest", json=p, headers=AUTH)
    r2 = client.post("/v1/whatsapp/ingest", json=p, headers=AUTH)
    assert r1.get_json() == {"handled": True, "done": False}
    assert r2.get_json()["reason"] == "duplicate"
    # Only one list reply should have been sent.
    assert len(app_module._sent_messages) == 1


# ---------------------------------------------------------------------------
# Confirm path
# ---------------------------------------------------------------------------

def test_confirm_returns_done_with_injected_text(app_module, client, fake_supabase, fake_openai):
    fake_supabase.seed_whatsapp_config()
    fake_openai._chat.next_content = json.dumps(
        [{"name": "Milk", "quantity": "1L"}, {"name": "Bread", "quantity": ""}]
    )

    client.post("/v1/whatsapp/ingest", json=_image_msg(msg_id="img-1"), headers=AUTH)
    r = client.post(
        "/v1/whatsapp/ingest",
        json=_text_msg("1", msg_id="confirm-1"),
        headers=AUTH,
    )
    body = r.get_json()
    assert body["handled"] is True
    assert body["done"] is True
    assert body["resume_mode"] == "order"
    assert "Milk - 1L" in body["injected_text"]
    assert "Bread" in body["injected_text"]

    # Session is cleared after confirmation.
    assert fake_supabase.ocr_sessions() == []


# ---------------------------------------------------------------------------
# Edit path
# ---------------------------------------------------------------------------

def test_edit_flow_applies_llm_output_then_confirms(app_module, client, fake_supabase, fake_openai):
    fake_supabase.seed_whatsapp_config()
    fake_openai._chat.next_content = json.dumps(
        [{"name": "Milk", "quantity": "1L"}]
    )
    client.post("/v1/whatsapp/ingest", json=_image_msg(msg_id="img-e"), headers=AUTH)

    # User picks edit mode.
    r = client.post("/v1/whatsapp/ingest", json=_text_msg("2", msg_id="edit-1"), headers=AUTH)
    assert r.get_json() == {"handled": True, "done": False}

    # Apply an edit.
    fake_openai._chat.next_content = json.dumps(
        [
            {"name": "Milk", "quantity": "1L"},
            {"name": "Butter", "quantity": "500g"},
        ]
    )
    r = client.post(
        "/v1/whatsapp/ingest",
        json=_text_msg("add Butter - 500g", msg_id="edit-2"),
        headers=AUTH,
    )
    assert r.get_json() == {"handled": True, "done": False}
    session = fake_supabase.ocr_sessions()[0]
    assert [i["name"] for i in session["items_json"]] == ["Milk", "Butter"]

    # Done -> back to awaiting_confirmation.
    r = client.post(
        "/v1/whatsapp/ingest",
        json=_text_msg("done", msg_id="edit-3"),
        headers=AUTH,
    )
    assert r.get_json() == {"handled": True, "done": False}
    assert fake_supabase.ocr_sessions()[0]["state"] == "awaiting_confirmation"

    # Confirm -> done with injected text including the new item.
    r = client.post(
        "/v1/whatsapp/ingest",
        json=_text_msg("1", msg_id="edit-4"),
        headers=AUTH,
    )
    body = r.get_json()
    assert body["done"] is True
    assert "Butter - 500g" in body["injected_text"]


# ---------------------------------------------------------------------------
# Plain text with no active session -> not our concern.
# ---------------------------------------------------------------------------

def test_plain_text_no_session_is_unhandled(app_module, client, fake_supabase):
    fake_supabase.seed_whatsapp_config()
    r = client.post(
        "/v1/whatsapp/ingest",
        json=_text_msg("just a normal message"),
        headers=AUTH,
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["handled"] is False


# ---------------------------------------------------------------------------
# Missing fields
# ---------------------------------------------------------------------------

def test_missing_fields_returns_unhandled(client):
    r = client.post("/v1/whatsapp/ingest", json={}, headers=AUTH)
    assert r.status_code == 200
    assert r.get_json()["reason"] == "missing_fields"
