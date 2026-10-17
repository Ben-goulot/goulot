"""Where a Messages API request goes: the real API (streaming), a recorder, or recorded answers (tests).

Everything downstream works on the response as a plain dict (`Message.to_dict()`), so recorded and live
answers follow the same path. Recordings hold model answers about public data only (the 3 test CAS).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

import anthropic


class MessageBackend(Protocol):
    def create(self, key: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one Messages API request; `key` names the call for recording and replay."""
        ...


def params_digest(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class AnthropicBackend:
    """Streams every request (long research turns, large outputs) and returns the final message."""

    def __init__(self, client: anthropic.Anthropic) -> None:
        self.client = client

    def create(self, key: str, params: dict[str, Any]) -> dict[str, Any]:
        with self.client.messages.stream(**params) as stream:
            message = stream.get_final_message()
        data: dict[str, Any] = message.to_dict(mode="json")  # JSON-safe: dates become ISO strings
        data["_request_id"] = getattr(message, "_request_id", None) or ""
        return data


class RecordingBackend:
    """Forwards to a live backend and writes each answer to `<directory>/<key>.json` (goulot dev record)."""

    def __init__(self, inner: MessageBackend, directory: Path) -> None:
        self.inner = inner
        self.directory = directory

    def create(self, key: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self.inner.create(key, params)
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {
            "key": key, "params_sha256": params_digest(params), "model": params.get("model"), "response": response,
        }
        path = self.directory / f"{key}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n",
                        encoding="utf-8")  # default=str: a billed answer is never lost to a serialization error
        return response


class ResumingBackend:
    """`goulot dev record` after a failure: answers already recorded (and paid) are reused, marked `_replayed`
    so that they are not billed twice; only the missing calls go to the live backend."""

    def __init__(self, inner: MessageBackend, directory: Path) -> None:
        self.inner = inner
        self.directory = directory

    def create(self, key: str, params: dict[str, Any]) -> dict[str, Any]:
        path = self.directory / f"{key}.json"
        if path.is_file():
            response: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["response"]
            return {**response, "_replayed": True}
        return self.inner.create(key, params)


class ReplayMissing(LookupError):
    pass


class ReplayBackend:
    """Answers from recordings; also keeps the requests it received, for the prompt tests."""

    def __init__(self, directory: Path | None = None, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.directory = directory
        self.responses = dict(responses or {})
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def create(self, key: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((key, params))
        if key in self.responses:
            return self.responses[key]
        if self.directory is not None:
            path = self.directory / f"{key}.json"
            if path.is_file():
                response: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))["response"]
                return response
        raise ReplayMissing(key)
