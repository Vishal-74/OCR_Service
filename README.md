# aiora-ocr-service

A small, Meta WhatsApp–compatible microservice that acts as an **OCR gate** in
front of the existing n8n orchestrator (`Mehta_Och AI`). When a buyer sends an
image or a voice note, this service:

1. Downloads the media directly from Meta's Graph API using the seller's own
   `access_token` (looked up from `whatsapp_config` in Supabase).
2. Runs OCR (GPT-4o vision) or transcription (Whisper) + list extraction.
3. Runs the full **confirm / edit** conversation with the user over WhatsApp.
4. Once the user confirms, returns the list back to n8n as a plain text
   `user message` in the mode the user was previously in. The rest of the
   orchestrator / order / service / appointment flow stays exactly the same.

Nothing is changed in the `aiora-business-dashboard` repo.

---

## Architecture

```
WhatsApp Cloud API
        │
        ▼
aiora-business-dashboard  (existing Next.js webhook receiver, unchanged)
        │  forwards to seller's n8n webhook
        ▼
n8n: Mehta_Och AI
    Webhook2 → Code → If → If2 → Merge
                                      │
                                      ▼
                      ┌──────────────────────────┐
                      │  OCR Gate — HTTP Request │ ──► aiora-ocr-service
                      └────────┬─────────────────┘      (this repo)
                               │ handled=false → pass original through
                               │ handled=true, done=false → stop
                               │ handled=true, done=true  → inject text
                               ▼
                             Route → Switch1 → Order / Service / Appointment / Call
```

Only two nodes are added to the n8n workflow. See
[`n8n/ocr_gate.md`](./n8n/ocr_gate.md) and
[`n8n/ocr_gate_nodes.json`](./n8n/ocr_gate_nodes.json).

---

## API

### `GET /health`

Liveness probe. Returns `{"status": "ok"}`.

### `POST /v1/whatsapp/ingest`

Called by n8n for **every** inbound WhatsApp message.

Headers:

- `x-internal-secret: <OCR_SHARED_SECRET>`  (or `Authorization: Bearer <OCR_SHARED_SECRET>`)

Body:

```json
{
  "metadata": { "phone_number_id": "123456" },
  "messages": [ /* the raw Meta message object, unmodified */ ],
  "contacts": [ /* optional, pass-through */ ],
  "mode": "order" | "service" | "appointment" | "calling" | null
}
```

Response (one of):

| Shape                                                                        | What n8n should do                                    |
| ---------------------------------------------------------------------------- | ----------------------------------------------------- |
| `{ "handled": false, ...reason }`                                            | Continue as normal (route to order/service/…).        |
| `{ "handled": true, "done": false }`                                         | Stop this run; OCR already replied on WhatsApp.       |
| `{ "handled": true, "done": true, "injected_text": "...", "resume_mode": "order" }` | Overwrite `messages[0].text.body` and `mode`, then continue. |

The service is **fail-open**: any internal error returns `handled: false`, so
the original n8n flow never breaks.

---

## Setup

### 1. Database

Apply the schema in Supabase (same project as the dashboard):

```
psql $DATABASE_URL -f sql/ocr_sessions.sql
```

or paste the contents into the Supabase SQL editor.

### 2. Environment

Copy `.env.example` to `.env` and fill in:

- `OCR_SHARED_SECRET`           — random strong string; also set in n8n.
- `OPENAI_API_KEY`              — for GPT-4o + Whisper.
- `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` — dashboard's Supabase project.
- `GRAPH_API_VERSION`           — defaults to `v20.0`.
- `OCR_SESSION_TTL_MIN`         — default `30`.
- `OCR_DEFAULT_RESUME_MODE`     — default `order`.

### 3. Local run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
# service on http://localhost:5000
```

### 4. Tests

```bash
pip install pytest
pytest -q
```

All tests are hermetic — no network, no OpenAI, no Supabase calls.

### 5. Deploy on Railway

1. `railway init` (Python / Nixpacks detected automatically).
2. Set env vars above in the Railway project.
3. Deploy. Railway will use the `Procfile` / `railway.toml` to run
   `gunicorn app:app ...`.
4. Copy the public URL into n8n's `OCR_SERVICE_BASE_URL` variable.
5. Set `OCR_SHARED_SECRET` in n8n to the **same** value.

### 6. Wire n8n

Follow [`n8n/ocr_gate.md`](./n8n/ocr_gate.md). Import the two nodes from
[`n8n/ocr_gate_nodes.json`](./n8n/ocr_gate_nodes.json) and reconnect
`Merge → OCR Gate → Route`. Nothing else changes.

---

## File map

```
aiora-ocr-service/
├── app.py                        # Flask service (ingest + health)
├── requirements.txt              # Python deps (pinned)
├── Procfile                      # Railway/Heroku-style web command
├── railway.toml                  # Railway config + healthcheck
├── .env.example                  # All env vars documented
├── sql/
│   └── ocr_sessions.sql          # Session state schema
├── n8n/
│   ├── ocr_gate.md               # How to insert the gate in Mehta_Och AI
│   └── ocr_gate_nodes.json       # Drop-in n8n nodes (2 nodes)
├── tests/
│   ├── conftest.py               # FakeSupabase + FakeOpenAI fixtures
│   └── test_app.py               # Confirm / edit / duplicate / fail-open tests
└── README.md
```

---

## Why not put this inside the dashboard?

- Keeps the OCR loop completely independent of the Next.js service — no
  risk of leaking OCR state into dashboard routes, sessions, or Supabase
  triggers.
- Python + OpenAI vision is a better fit than the Next.js runtime for this.
- Each seller can be opted in/out by toggling the n8n gate, with zero deploy
  on the dashboard side.
- Reuses the dashboard's `whatsapp_config` (read-only) so onboarding a new
  seller needs no extra configuration here.

## Why not put it all inside n8n?

- Meta media download + base64 + model calls + edit loop in raw n8n
  JavaScript is doable but fragile and hard to test. A small Flask service
  lets us write unit tests and iterate on prompts without redeploying n8n.
