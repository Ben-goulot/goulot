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


def make_anthropic_client(api_key: str, http_client: httpx2.Client) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=api_key, http_client=http_client, max_retries=MAX_RETRIES)


def build_anthropic_client(api_key: str, allowlist: Allowlist) -> anthropic.Anthropic:
    return make_anthropic_client(api_key, build_client(allowlist))
