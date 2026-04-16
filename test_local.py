"""
Local tests for railway-service app.py (mock OpenAI / Twilio).

Run from this folder:
  python test_local.py
"""
import json
import sys
import unittest
from unittest.mock import MagicMock, patch

import os

os.environ.setdefault("TWILIO_ACCOUNT_SID", "ACtest")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test_token")
os.environ.setdefault("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")

with patch("twilio.rest.Client"), patch("openai.OpenAI"):
    import app as grocery_app


def _mock_openai_text(content: str):
    choice = MagicMock()
    choice.message.content = content
    resp = MagicMock()
    resp.choices = [choice]
    return resp


SAMPLE_ITEMS = [
    {"name": "Milk", "quantity": "2L"},
    {"name": "Eggs", "quantity": "12"},
    {"name": "Bread", "quantity": ""},
]


class TestParseItemsFromText(unittest.TestCase):

    def test_returns_list_of_dicts(self):
        expected = [{"name": "Sugar", "quantity": "1kg"}, {"name": "Oil", "quantity": ""}]
        grocery_app.openai_client.chat.completions.create = MagicMock(
            return_value=_mock_openai_text(json.dumps(expected))
        )

        result = grocery_app.parse_items_from_text("Sugar 1kg and some Oil")

        self.assertIsInstance(result, list)
        self.assertEqual(result[0]["name"], "Sugar")
        self.assertEqual(result[0]["quantity"], "1kg")
        self.assertEqual(result[1]["name"], "Oil")


class TestApplyEdits(unittest.TestCase):

    def test_add_item(self):
        updated = SAMPLE_ITEMS + [{"name": "Butter", "quantity": "500g"}]
        grocery_app.openai_client.chat.completions.create = MagicMock(
            return_value=_mock_openai_text(json.dumps(updated))
        )

        result = grocery_app.apply_edits(SAMPLE_ITEMS, "add Butter - 500g")
        self.assertEqual(len(result), 4)
        self.assertEqual(result[-1]["name"], "Butter")


class TestWebhookStateMachine(unittest.TestCase):

    def setUp(self):
        grocery_app.sessions.clear()
        grocery_app.app.config["TESTING"] = True
        self.client = grocery_app.app.test_client()
        grocery_app.send_whatsapp = MagicMock()

    def _post(self, **form_data):
        defaults = {"From": "whatsapp:+19999999999", "Body": "", "NumMedia": "0"}
        defaults.update(form_data)
        return self.client.post("/webhook", data=defaults)

    def test_health(self):
        for path in ("/", "/health"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn(b"ok", r.data)

    def test_confirm_clears_session(self):
        sender = "whatsapp:+19999999999"
        grocery_app.sessions[sender] = {"state": "awaiting_confirmation", "items": SAMPLE_ITEMS}
        grocery_app.dispatch_order = MagicMock()

        self._post(Body="1")

        self.assertNotIn(sender, grocery_app.sessions)
        grocery_app.dispatch_order.assert_called_once()


if __name__ == "__main__":
    suite = unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
