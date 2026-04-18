# n8n OCR Gate — How to wire it into `Mehta_Och AI`

This is the **only** change needed in n8n to turn OCR on for a seller. It keeps
the existing routing (order / service / appointment / call agent) untouched.

## Where to insert

The orchestrator workflow's main path today is:

```
Webhook2 → Code in JavaScript3 → If → If2 → Merge → Route → If1 → Switch1 → …
                                             ↑
                                             │
                                Get row(s) in sheet
```

Insert the gate **between `Merge` and `Route`**:

```
… → Merge → [OCR Gate — HTTP Request] → [OCR Gate — Apply Response] → Route → …
```

Why here?

- After `Merge`, both pieces of context are available on a single item:
  - The raw Meta WhatsApp payload (`messages`, `metadata`, `contacts`) from
    `Webhook2` / `Code in JavaScript3`.
  - The user's persisted `mode` column from the Google Sheet lookup.
- Before `Route`, so we can either pass a transformed payload (confirmed list
  as a plain text message in the user's previous mode) or stop the execution
  entirely while OCR is still talking to the user.

## Behaviour contract

The gate calls `POST ${OCR_SERVICE_BASE_URL}/v1/whatsapp/ingest` with:

```json
{
  "metadata": { "phone_number_id": "<from Meta payload>" },
  "messages": [ /* pass through msg 0 verbatim */ ],
  "contacts": [ /* optional; pass through */ ],
  "mode": "order" | "service" | "appointment" | "calling" | null
}
```

and handles the response:

| Response                                           | What the gate does                                                                                 |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `{ handled: false }`                               | Emit the original Merge output unchanged. `Route` and the rest of the flow run normally.           |
| `{ handled: true, done: false }`                   | Emit **no items**. The workflow run stops. The OCR service already replied to the user on WhatsApp.|
| `{ handled: true, done: true, injected_text, resume_mode }` | Emit the original payload **with `messages[0].text` rewritten** to `injected_text` and `mode` set to `resume_mode`. `Route` then continues exactly as if the user had typed the list. |

The OCR service is fail-open: if it errors internally it returns
`handled: false`, so the existing flow never breaks.

## Environment variables to add in n8n

Set these at the instance level (or the workflow's variables):

- `OCR_SERVICE_BASE_URL` — e.g. `https://aiora-ocr.up.railway.app`
- `OCR_SHARED_SECRET`    — same value as the service's `OCR_SHARED_SECRET`

## Drop-in nodes (copy-paste)

Paste the JSON in [`ocr_gate_nodes.json`](./ocr_gate_nodes.json) into
`Mehta_Och AI` via **Import from file** or copy the two nodes one at a time.

After importing:

1. Delete the existing connection `Merge → Route`.
2. Connect `Merge → OCR Gate — Call Service`.
3. Connect `OCR Gate — Call Service → OCR Gate — Apply Response`.
4. Connect `OCR Gate — Apply Response → Route`.

That's it. No other node, credential, or workflow changes are required.

## Rollout tip

If you want to gate this per seller, add a leading `IF` node that only enters
the OCR gate when `phone_number_id` (or `seller_id` from the sheet) is in an
allow-list. Everyone else flows `Merge → Route` as before.

## Rollback

Remove the two gate nodes and reconnect `Merge → Route`. The dashboard is
never in the loop — there is nothing else to revert.
