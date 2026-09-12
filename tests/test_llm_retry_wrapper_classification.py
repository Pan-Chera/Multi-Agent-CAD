"""BUG-022: _call_llm_json_with_retry used to retry only on "timeout" in
the error message and re-raise everything else. That meant 429 / 5xx /
connection errors -- which ARE transient and should be retried -- were
treated as non-retryable. Conversely, 401/403/invalid-model errors are
permanent and should NOT be retried, but a longer message containing one
of these patterns might accidentally be retried if it also said "timeout".

Fix: narrow retryability classification.
  * timeout / 429 / 5xx / connection errors -> retry (up to max_retries)
  * 401 / 403 / authentication / invalid-api-key / invalid-model /
    model-not-found -> do NOT retry (re-raise immediately)
  * Unrecognized errors -> existing conservative strategy (re-raise).

No external LLM calls; the OpenAI client is monkeypatched.
"""
from __future__ import annotations

import unittest
from unittest import mock


def _make_client(raise_exc):
    """Build a mock client whose chat.completions.create raises raise_exc."""
    client = mock.MagicMock()
    client.chat.completions.create.side_effect = raise_exc
    return client


def _run(client, retries: int = 3):
    from multi_agent_cad.nodes import _call_llm_json_with_retry

    return _call_llm_json_with_retry(
        client,
        [{"role": "user", "content": "x"}],
        model="m", max_tokens=1, temperature=0.0,
        max_retries=retries,
    )


class TestLLMRetryWrapperClassification(unittest.TestCase):
    def test_429_rate_limit_is_retried(self):
        """429 (rate limit) is transient -- retry until success."""
        # First 2 calls raise 429, third succeeds.
        success_resp = mock.MagicMock()
        success_resp.choices = [mock.MagicMock()]
        success_resp.choices[0].message.content = '```json\n{"x": 1}\n```'
        client = _make_client(Exception("Error code: 429 (Rate limit exceeded)"))
        client.chat.completions.create.side_effect = [
            Exception("Error code: 429 (Rate limit exceeded)"),
            Exception("Error code: 429 (Rate limit exceeded)"),
            success_resp,
        ]
        json_str, _ = _run(client, retries=3)
        self.assertEqual(client.chat.completions.create.call_count, 3,
                         "429 must be retried, not re-raised on first attempt")

    def test_5xx_server_error_is_retried(self):
        success_resp = mock.MagicMock()
        success_resp.choices = [mock.MagicMock()]
        success_resp.choices[0].message.content = '```json\n{"y": 2}\n```'
        client = _make_client(Exception("Error code: 503 (Service unavailable)"))
        client.chat.completions.create.side_effect = [
            Exception("Error code: 503"),
            success_resp,
        ]
        _run(client, retries=3)
        self.assertEqual(client.chat.completions.create.call_count, 2,
                         "5xx must be retried")

    def test_401_auth_failure_is_not_retried(self):
        """401 / auth failure is permanent -- do NOT burn retries."""
        client = _make_client(Exception("Error code: 401 (Unauthorized)"))
        with self.assertRaises(Exception) as ctx:
            _run(client, retries=3)
        self.assertIn("401", str(ctx.exception))
        self.assertEqual(client.chat.completions.create.call_count, 1,
                         "401 must NOT be retried (one call, immediate raise)")

    def test_invalid_api_key_is_not_retried(self):
        client = _make_client(Exception("Invalid API-key provided"))
        with self.assertRaises(Exception):
            _run(client, retries=3)
        self.assertEqual(client.chat.completions.create.call_count, 1,
                         "invalid api key must NOT be retried")

    def test_model_not_found_is_not_retried(self):
        client = _make_client(Exception("Model not exist: qwen-bogus"))
        with self.assertRaises(Exception):
            _run(client, retries=3)
        self.assertEqual(client.chat.completions.create.call_count, 1,
                         "model-not-found must NOT be retried")


if __name__ == "__main__":
    unittest.main()
