"""Thin wrappers reusing MAC's LLM infrastructure for assembly stages.

No new client code: ``_llm_client`` / ``_call_llm_json_with_retry`` /
multimodal fallback keywords all come from ``multi_agent_cad.nodes`` so
token accounting (token_tracker) and provider switching keep working.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from multi_agent_cad.nodes import (  # noqa: E402
    _call_llm_json_with_retry,
    _llm_client,
)
from multi_agent_cad.token_tracker import tracker  # noqa: F401  (patched client)


def call_llm_json(
    system_prompt: str,
    user_content,
    *,
    model: str,
    temperature: float,
    max_tokens: int,
    extra_kwargs: dict | None = None,
    max_retries: int = 3,
) -> dict:
    """Call the LLM and parse the fenced-JSON response into a dict.

    ``user_content`` is either a plain string or an OpenAI-compatible
    content list (text + image_url blocks) for multimodal stages.
    """
    client = _llm_client()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    json_str, _raw = _call_llm_json_with_retry(
        client,
        messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        extra_kwargs=extra_kwargs,
        max_retries=max_retries,
    )
    return json.loads(json_str)


def build_multimodal_content(text: str, image_data_urls: list[str]):
    """Build an OpenAI-compatible content list (text first, then images)."""
    content: list[dict] = [{"type": "text", "text": text}]
    for url in image_data_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})
    return content


def encode_png_data_url(png_bytes: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
