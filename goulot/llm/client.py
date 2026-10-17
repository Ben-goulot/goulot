"""Anthropic SDK client on the allowlist transport (CLAUDE.md, rule 6).

Budget, Batch, prompt construction and output validation arrive with the first AI calls (phase 2).
Every AI call goes through goulot/llm/ (CLAUDE.md, rule 8).
"""

from __future__ import annotations

import anthropic
import httpx2

from goulot.net.allowlist import Allowlist
from goulot.net.client import build_client

MAX_RETRIES = 2
# The SDK would otherwise inherit the sources' 30 s read timeout: a structured-output call can stay silent longer
# than that while its schema compiles and the model thinks. Between two streamed chunks, 5 minutes at most.
TIMEOUT = httpx2.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)


def make_anthropic_client(api_key: str, http_client: httpx2.Client) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key, http_client=http_client, max_retries=MAX_RETRIES, timeout=TIMEOUT)


def build_anthropic_client(api_key: str, allowlist: Allowlist) -> anthropic.Anthropic:
    return make_anthropic_client(api_key, build_client(allowlist))
