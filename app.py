"""
Aiora OCR Service (Meta WhatsApp Cloud API compatible).

Purpose
-------
A tiny, stateless-looking HTTP service that the n8n ingress workflow calls as a
"gate" for every inbound WhatsApp message. It:

  1. Handles image/voice inputs (OCR / transcription + structuring into lines
     the user can confirm).
  2. Runs the confirm/edit conversation end-to-end over WhatsApp (direct
     Graph API sends) until the user confirms.
  3. Returns `{done: true, injected_text, resume_mode}` once the user
     confirms. The n8n workflow injects `injected_text` as a normal user
     message — for any flow (orders, appointments, services, etc.), not only
     shopping lists.

Design goals
------------
- Works with Meta WhatsApp Cloud API (not Twilio). We resolve the seller from
  `metadata.phone_number_id` against the dashboard's `whatsapp_config` table
  and reuse the stored `access_token` to download media and send replies.
- Multi-seller, clean structure, minimal dashboard changes.
- Fail-open: if anything inside this service errors, we return
  `handled: false` so n8n can keep running the existing flow untouched.
- Idempotent: duplicate Meta deliveries (same `message_id`) are ignored.
- Session state persisted in Supabase (`ocr_sessions`), keyed by
  `(seller_id, from_phone)`.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from flask import Flask, jsonify, request
from openai import OpenAI
from supabase import Client, create_client

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _req_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required env var: {name}")
    return val


OCR_SHARED_SECRET = _req_env("OCR_SHARED_SECRET")
OPENAI_API_KEY = _req_env("OPENAI_API_KEY")
SUPABASE_URL = _req_env("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = _req_env("SUPABASE_SERVICE_ROLE_KEY")

OPENAI_VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o")
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o")
OPENAI_WHISPER_MODEL = os.getenv("OPENAI_WHISPER_MODEL", "whisper-1")
GRAPH_API_VERSION = os.getenv("GRAPH_API_VERSION", "v20.0")
OCR_SESSION_TTL_MIN = int(os.getenv("OCR_SESSION_TTL_MIN", "30"))
OCR_DEFAULT_RESUME_MODE = os.getenv("OCR_DEFAULT_RESUME_MODE", "order")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("aiora-ocr")


# ---------------------------------------------------------------------------
# Clients (module-level so tests can monkey-patch them)
# ---------------------------------------------------------------------------

openai_client: OpenAI = OpenAI(api_key=OPENAI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


app = Flask(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_AWAIT_CONFIRM = "awaiting_confirmation"
STATE_EDIT_MODE = "edit_mode"

ALLOWED_RESUME_MODES = {"order", "service", "appointment", "calling"}

# Max chars per line in stored JSON (WhatsApp + DB safety).
_MAX_LINE_CHARS = 3900


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _authorized(req) -> bool:
    """n8n authenticates with a shared secret header.

    We accept either `x-internal-secret` (to match the dashboard's downstream
    convention) or `Authorization: Bearer ...`.
    """
    header_secret = req.headers.get("x-internal-secret")
    if header_secret and header_secret == OCR_SHARED_SECRET:
        return True
    auth = req.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == OCR_SHARED_SECRET:
        return True
    return False


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _resolve_seller(phone_number_id: str) -> Optional[dict]:
    """Look up seller_id + access_token from the dashboard's `whatsapp_config`."""
    res = (
        supabase.table("whatsapp_config")
        .select("seller_id,access_token,phone_number_id")
        .eq("phone_number_id", phone_number_id)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    row = rows[0]
    if not row.get("seller_id") or not row.get("access_token"):
        return None
    return row


def _get_session(seller_id: str, from_phone: str) -> Optional[dict]:
    res = (
        supabase.table("ocr_sessions")
        .select("*")
        .eq("seller_id", seller_id)
        .eq("from_phone", from_phone)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    return rows[0] if rows else None


def _upsert_session(
    seller_id: str,
    from_phone: str,
    *,
    state: str,
    items_json: list,
    resume_mode: Optional[str],
    last_message_id: Optional[str],
) -> None:
    now = datetime.now(timezone.utc)
    payload = {
        "seller_id": seller_id,
        "from_phone": from_phone,
        "state": state,
        "items_json": items_json,
        "resume_mode": resume_mode,
        "last_message_id": last_message_id,
        "updated_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=OCR_SESSION_TTL_MIN)).isoformat(),
    }
    supabase.table("ocr_sessions").upsert(
        payload, on_conflict="seller_id,from_phone"
    ).execute()


def _clear_session(seller_id: str, from_phone: str) -> None:
    (
        supabase.table("ocr_sessions")
        .delete()
        .eq("seller_id", seller_id)
        .eq("from_phone", from_phone)
        .execute()
    )


def _session_expired(session: dict) -> bool:
    exp = session.get("expires_at")
    if not exp:
        return False
    try:
        dt = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
    except Exception:
        return False
    return dt < datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Meta Graph API — media download + WA sends
# ---------------------------------------------------------------------------

def _download_meta_media(
    media_id: str,
    access_token: str,
    phone_number_id: Optional[str] = None,
) -> tuple[bytes, str]:
    """Meta's two-step media download.

    1) GET /<version>/<media_id>?phone_number_id=<pni>   -> { url, mime_type, ... }
    2) GET <url>  (Authorization: Bearer)                -> raw bytes

    Including `phone_number_id` is Meta's documented best practice for Graph
    v17+ — it helps the API scope the token check to the right number and
    avoids 400s when a business has multiple numbers under one WABA.
    """
    meta_url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}"
    params = {"phone_number_id": phone_number_id} if phone_number_id else None
    r1 = requests.get(
        meta_url,
        params=params,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=20,
    )
    if not r1.ok:
        # Surface the actual Meta error so we can see *why* (expired token,
        # wrong WABA, unknown media, rate limit, etc.) in the Railway logs.
        log.error(
            "Meta media lookup failed: status=%s media_id=%s pni=%s body=%s",
            r1.status_code,
            media_id,
            phone_number_id,
            r1.text[:600],
        )
        r1.raise_for_status()
    meta = r1.json()
    url = meta.get("url")
    mime = meta.get("mime_type") or "application/octet-stream"
    if not url:
        raise RuntimeError(f"Meta media lookup missing url: {meta}")

    r2 = requests.get(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if not r2.ok:
        log.error(
            "Meta media bytes fetch failed: status=%s body=%s",
            r2.status_code,
            r2.text[:600],
        )
        r2.raise_for_status()
    return r2.content, mime


def _send_wa_text(
    phone_number_id: str, access_token: str, to: str, body: str
) -> None:
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": body, "preview_url": False},
    }
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        if not r.ok:
            log.error("WA send failed %s: %s", r.status_code, r.text[:400])
    except Exception as e:
        log.exception("WA send error: %s", e)


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------

def _strip_code_fence(raw: str) -> str:
    s = raw.strip()
    if s.startswith("```"):
        # strip ```json ... ```  or ``` ... ```
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[: -3]
    return s.strip()


def _parse_items_json(raw: str) -> list[dict]:
    try:
        data = json.loads(_strip_code_fence(raw))
    except json.JSONDecodeError:
        log.warning("LLM returned non-JSON (first 400 chars): %s", raw[:400])
        return []
    if not isinstance(data, list):
        log.warning(
            "LLM returned non-array (first 400 chars): %s", str(data)[:400]
        )
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        qty = str(item.get("quantity") or "").strip()
        if name:
            out.append({"name": name, "quantity": qty})
    if not out:
        log.info("LLM returned empty items list; raw=%s", raw[:400])
    return out


def _ocr_image_to_items(image_bytes: bytes, mime: str) -> list[dict]:
    """Extract readable lines from an image (lists, notes, screenshots, etc.)."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt = (
        "Read this image. Extract what the user is communicating as separate "
        "lines for confirmation before sending to a business assistant.\n"
        "- Handwritten or printed lists: one product or idea per element.\n"
        "- Notes, forms, screenshots: split distinct facts onto separate lines.\n"
        "- If there is one clear message, use a single element.\n"
        "Use \"quantity\" only when it is a count/weight/size for an order; "
        "otherwise use an empty string.\n"
        "Return ONLY a JSON array, no markdown, no explanation.\n"
        'Each element: {"name": "...", "quantity": "..."}.'
    )
    resp = openai_client.chat.completions.create(
        model=OPENAI_VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        max_tokens=800,
        temperature=0,
    )
    return _parse_items_json(resp.choices[0].message.content or "")


def _vision_describe_image(image_bytes: bytes, mime: str) -> str:
    """Fallback when OCR returns no structured lines — short plain-text summary."""
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt = (
        "In 1–4 short lines, describe what the user is showing or what they "
        "likely want to communicate. Plain text only — no JSON, no markdown."
    )
    resp = openai_client.chat.completions.create(
        model=OPENAI_VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"},
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        max_tokens=400,
        temperature=0,
    )
    return (resp.choices[0].message.content or "").strip()


def _ensure_image_items(
    items: list[dict], image_bytes: bytes, mime: str
) -> list[dict]:
    if items:
        return items
    try:
        desc = _vision_describe_image(image_bytes, mime)
        if desc:
            return [{"name": desc[:_MAX_LINE_CHARS], "quantity": ""}]
    except Exception as e:
        log.warning("vision describe fallback failed: %s", e)
    return [
        {
            "name": (
                "(No clear text in this image — reply *2* to edit and type "
                "what you meant.)"
            ),
            "quantity": "",
        }
    ]


def _transcribe_audio(audio_bytes: bytes, mime: str) -> str:
    ext = (mime.split("/")[-1].split(";")[0] or "ogg").strip()
    buf = io.BytesIO(audio_bytes)
    buf.name = f"voice.{ext}"
    t = openai_client.audio.transcriptions.create(
        model=OPENAI_WHISPER_MODEL, file=buf
    )
    text = (t.text or "").strip()
    log.info("Whisper transcript (%s, %dB): %r", ext, len(audio_bytes), text[:400])
    return text


def _transcript_to_structured_items(text: str) -> list[dict]:
    """Split a voice transcript into lines for confirm/edit (any business context)."""
    prompt = (
        "You convert a customer's message (usually a voice-note transcript) "
        "into lines they can confirm before sending to a business assistant. "
        "The assistant may handle orders, appointments, services, or general "
        "questions — not only shopping.\n\n"
        "Instructions:\n"
        "1. Preserve meaning. You may trim filler (\"um\", \"please\", \"okay\") "
        "   but keep names, dates, products, and requests.\n"
        "2. Put each distinct fact, request, or item on its own line (name field). "
        "   Examples: appointment time, product interest, address, phone number.\n"
        "3. Use \"quantity\" only for countable order-style amounts "
        "   (e.g. 2 kg, 3 packs); otherwise empty string.\n"
        "4. Return ONLY a JSON array, no markdown, no explanation.\n"
        '   Format: [{"name": "...", "quantity": "..."}, ...]\n'
        "5. If the message is one coherent utterance, a single element is fine.\n\n"
        f"Transcript:\n{text}"
    )
    resp = openai_client.chat.completions.create(
        model=OPENAI_TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800,
        temperature=0,
    )
    raw = resp.choices[0].message.content or ""
    return _parse_items_json(raw)


def _ensure_voice_items(transcript: str, items: list[dict]) -> list[dict]:
    """Always return at least one line so voice always stays in the OCR loop."""
    if items:
        return items
    t = (transcript or "").strip()
    if t:
        return [{"name": t[:_MAX_LINE_CHARS], "quantity": ""}]
    return [
        {
            "name": "(Empty voice note — reply *2* to edit and type your message.)",
            "quantity": "",
        }
    ]


def _apply_edits(items: list[dict], edit_text: str) -> list[dict]:
    numbered = "\n".join(
        f"{i+1}. {it['name']}"
        + (f" — {it['quantity']}" if it.get("quantity") else "")
        for i, it in enumerate(items)
    ) or "(no lines yet)"
    prompt = (
        "You are editing the user's draft message (numbered lines) based on "
        "their instruction. This can be orders, appointments, or any request.\n\n"
        f"Current lines (numbered):\n{numbered}\n\n"
        f"Current JSON:\n{json.dumps(items, ensure_ascii=False)}\n\n"
        f"User instruction:\n{edit_text}\n\n"
        "Supported edits:\n"
        "- add:       'add <text>' or 'add <name> - <qty>' -> append a line\n"
        "- update:    'update <N> ...' or replace a line\n"
        "- delete:    'delete <N>' or 'remove <text>' -> remove (case-insensitive)\n\n"
        "Return ONLY the updated JSON array, no markdown, no explanation.\n"
        'Each element: {"name": "...", "quantity": "..."}.'
    )
    resp = openai_client.chat.completions.create(
        model=OPENAI_TEXT_MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=800,
        temperature=0,
    )
    return _parse_items_json(resp.choices[0].message.content or "")


# ---------------------------------------------------------------------------
# UX strings + helpers
# ---------------------------------------------------------------------------

def _format_confirm_message(items: list[dict]) -> str:
    lines = "\n".join(
        f"{i+1}. {it['name']}"
        + (f" — {it['quantity']}" if it.get("quantity") else "")
        for i, it in enumerate(items)
    )
    return (
        f"📝 Here's what we'll send (*{len(items)} line(s)*):\n\n"
        f"{lines}\n\n"
        "Reply *1* to confirm and continue ✅\n"
        "Reply *2* to edit ✏️"
    )


def _edit_help_message() -> str:
    return (
        "✏️ *Edit mode*\n\n"
        "Send changes, for example:\n"
        "• _add Tuesday 3pm_\n"
        "• _delete 2_\n"
        "• _remove silver rings_\n\n"
        "Reply *done* when finished."
    )


def _items_to_injected_text(items: list[dict]) -> str:
    """Confirmed text passed to n8n as a normal user message."""
    return "\n".join(
        f"{it['name']}" + (f" - {it['quantity']}" if it.get("quantity") else "")
        for it in items
    )


def _extract_text_from_message(msg: dict) -> str:
    mtype = msg.get("type")
    if mtype == "text":
        return ((msg.get("text") or {}).get("body") or "").strip()
    if mtype == "interactive":
        interactive = msg.get("interactive") or {}
        btn = interactive.get("button_reply") or interactive.get("list_reply") or {}
        return (btn.get("id") or btn.get("title") or "").strip()
    if mtype == "button":
        return ((msg.get("button") or {}).get("text") or "").strip()
    return ""


def _normalise_mode(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    v = str(value).strip().lower()
    return v if v in ALLOWED_RESUME_MODES else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "aiora-ocr"}), 200


@app.route("/v1/whatsapp/ingest", methods=["POST"])
def ingest():
    """Single endpoint the n8n ingress calls for every inbound message.

    Request body (from n8n):
        {
          "metadata":    { "phone_number_id": "..." },
          "messages":    [ <raw Meta message object> ],
          "mode":        "order" | "service" | "appointment" | "calling"  (optional;
                         n8n's currently active mode for this user, so we can
                         resume it after the user confirms)
        }

    Response:
        { "handled": false }                     -> n8n should run its
                                                    normal routing.
        { "handled": true, "done": false }       -> OCR is mid-conversation;
                                                    n8n should stop this run
                                                    (we already sent the WA
                                                    reply to the user).
        { "handled": true, "done": true,
          "injected_text": "...",
          "resume_mode": "order" }               -> OCR finished. n8n
                                                    should replace the user's
                                                    message text with
                                                    `injected_text` and
                                                    continue as usual.
    """
    if not _authorized(request):
        return jsonify({"error": "unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    metadata = body.get("metadata") or {}
    messages = body.get("messages") or []
    msg = messages[0] if messages else {}

    phone_number_id = metadata.get("phone_number_id")
    from_phone = msg.get("from")
    message_type = msg.get("type")
    message_id = msg.get("id")
    current_mode = _normalise_mode(body.get("mode"))

    if not phone_number_id or not from_phone or not message_id or not message_type:
        return jsonify({"handled": False, "reason": "missing_fields"}), 200

    # Extra safety: if n8n re-sends the injected text after confirm, do not
    # re-enter OCR. The gate should not send us that request, but we honour it.
    if body.get("ocr_bypass") is True:
        return jsonify({"handled": False, "reason": "ocr_bypass"}), 200

    # Resolve seller and WhatsApp credentials from Supabase.
    try:
        seller = _resolve_seller(phone_number_id)
    except Exception as e:
        log.exception("seller lookup failed: %s", e)
        return jsonify({"handled": False, "reason": "seller_lookup_error"}), 200

    if not seller:
        # Unknown seller. Fail-open so n8n can still handle the message.
        return jsonify({"handled": False, "reason": "seller_not_found"}), 200

    seller_id = seller["seller_id"]
    access_token = seller["access_token"]

    # Load session (if any), clear if expired.
    try:
        session = _get_session(seller_id, from_phone)
    except Exception as e:
        log.exception("session load failed: %s", e)
        session = None

    if session and _session_expired(session):
        try:
            _clear_session(seller_id, from_phone)
        except Exception:
            pass
        session = None

    # Idempotency — duplicate Meta delivery of the same message_id.
    if session and session.get("last_message_id") == message_id:
        return jsonify({"handled": True, "done": False, "reason": "duplicate"}), 200

    try:
        # --------------------------------------------------------------
        # Case A) Media message -> start (or restart) OCR.
        # --------------------------------------------------------------
        if message_type in ("image", "audio", "voice"):
            media_key = "image" if message_type == "image" else "audio"
            media = msg.get(message_type) or msg.get(media_key) or {}
            media_id = media.get("id")
            if not media_id:
                return (
                    jsonify({"handled": False, "reason": "no_media_id"}),
                    200,
                )

            raw_bytes, mime = _download_meta_media(
                media_id, access_token, phone_number_id=phone_number_id
            )

            if message_type == "image":
                items = _ocr_image_to_items(raw_bytes, mime)
                items = _ensure_image_items(items, raw_bytes, mime)
                heading = "📸 Got your photo!"
            else:
                transcript = _transcribe_audio(raw_bytes, mime)
                structured = (
                    _transcript_to_structured_items(transcript) if transcript else []
                )
                items = _ensure_voice_items(transcript, structured)
                log.info(
                    "voice: transcript_len=%d -> %d line(s)",
                    len(transcript or ""),
                    len(items),
                )
                heading = (
                    f"🎙️ I heard: _{transcript}_" if transcript else "🎙️ Got your voice note!"
                )

            # Carry forward resume_mode if we already had a session, otherwise
            # use the mode n8n told us about for this user.
            resume_mode = (
                (session or {}).get("resume_mode")
                or current_mode
                or OCR_DEFAULT_RESUME_MODE
            )

            _upsert_session(
                seller_id,
                from_phone,
                state=STATE_AWAIT_CONFIRM,
                items_json=items,
                resume_mode=resume_mode,
                last_message_id=message_id,
            )
            _send_wa_text(
                phone_number_id,
                access_token,
                from_phone,
                f"{heading}\n\n{_format_confirm_message(items)}",
            )
            return jsonify({"handled": True, "done": False}), 200

        # --------------------------------------------------------------
        # Case B) Non-media message, but an OCR session is active.
        # --------------------------------------------------------------
        if session and session.get("state") in (STATE_AWAIT_CONFIRM, STATE_EDIT_MODE):
            text = _extract_text_from_message(msg)
            items = list(session.get("items_json") or [])
            resume_mode = session.get("resume_mode") or OCR_DEFAULT_RESUME_MODE

            if session["state"] == STATE_AWAIT_CONFIRM:
                if text == "1" or text.lower() in ("confirm", "yes", "ok", "okay"):
                    injected_text = _items_to_injected_text(items)
                    _clear_session(seller_id, from_phone)
                    _send_wa_text(
                        phone_number_id,
                        access_token,
                        from_phone,
                        "✅ Confirmed! Continuing…",
                    )
                    return (
                        jsonify(
                            {
                                "handled": True,
                                "done": True,
                                "injected_text": injected_text,
                                "resume_mode": resume_mode,
                            }
                        ),
                        200,
                    )
                if text == "2" or text.lower() in ("edit", "change", "modify"):
                    _upsert_session(
                        seller_id,
                        from_phone,
                        state=STATE_EDIT_MODE,
                        items_json=items,
                        resume_mode=resume_mode,
                        last_message_id=message_id,
                    )
                    _send_wa_text(
                        phone_number_id, access_token, from_phone, _edit_help_message()
                    )
                    return jsonify({"handled": True, "done": False}), 200

                # Unknown reply -> re-show draft.
                _upsert_session(
                    seller_id,
                    from_phone,
                    state=STATE_AWAIT_CONFIRM,
                    items_json=items,
                    resume_mode=resume_mode,
                    last_message_id=message_id,
                )
                _send_wa_text(
                    phone_number_id,
                    access_token,
                    from_phone,
                    "Please reply *1* to confirm or *2* to edit.\n\n"
                    + _format_confirm_message(items),
                )
                return jsonify({"handled": True, "done": False}), 200

            # state == edit_mode
            if text.lower() in ("done", "finish", "finished", "ok", "okay"):
                _upsert_session(
                    seller_id,
                    from_phone,
                    state=STATE_AWAIT_CONFIRM,
                    items_json=items,
                    resume_mode=resume_mode,
                    last_message_id=message_id,
                )
                _send_wa_text(
                    phone_number_id, access_token, from_phone, _format_confirm_message(items)
                )
                return jsonify({"handled": True, "done": False}), 200

            if text.lower() in ("cancel", "stop", "exit"):
                _clear_session(seller_id, from_phone)
                _send_wa_text(
                    phone_number_id,
                    access_token,
                    from_phone,
                    "OK, cancelled. What would you like to do next?",
                )
                return jsonify({"handled": True, "done": False, "reason": "cancelled"}), 200

            try:
                new_items = _apply_edits(items, text)
            except Exception as e:
                log.exception("apply_edits failed: %s", e)
                _send_wa_text(
                    phone_number_id,
                    access_token,
                    from_phone,
                    "❌ I couldn't apply that edit. Please try again.",
                )
                return jsonify({"handled": True, "done": False, "reason": "edit_failed"}), 200

            if not new_items:
                new_items = items  # don't wipe the list on an empty LLM response

            _upsert_session(
                seller_id,
                from_phone,
                state=STATE_EDIT_MODE,
                items_json=new_items,
                resume_mode=resume_mode,
                last_message_id=message_id,
            )
            _send_wa_text(
                phone_number_id,
                access_token,
                from_phone,
                "✅ Updated! Current draft:\n\n"
                + _format_confirm_message(new_items)
                + "\n\nKeep editing, or reply *done* to confirm.",
            )
            return jsonify({"handled": True, "done": False}), 200

        # --------------------------------------------------------------
        # Case C) Plain text, no active session -> not our concern.
        # --------------------------------------------------------------
        return jsonify({"handled": False}), 200

    except requests.HTTPError as e:
        log.exception("HTTP error in ingest: %s", e)
        return jsonify({"handled": False, "reason": "http_error"}), 200
    except Exception as e:
        log.exception("Unhandled error in ingest: %s", e)
        # Fail-open: let n8n handle the message normally.
        return jsonify({"handled": False, "reason": "internal_error"}), 200


# ---------------------------------------------------------------------------
# Local dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "5000")),
        debug=os.getenv("FLASK_DEBUG") == "1",
    )
