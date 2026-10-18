"""Deferred processing (Message Batches, −50 %), the default mode for the watch (CLAUDE.md, rule 8).

A batch is submitted after a budget check on its worst case; results are collected at a later cycle (most
batches end within minutes, at most 24 hours). Each result is billed at the Batch rate and recorded in
`llm_calls`. Replay and recording mirror `goulot.llm.backend` for the tests.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import anthropic

from goulot.db.connection import Database, utcnow
from goulot.llm.budget import Budget, CallRecord
from goulot.llm.pricing import Usage, cost_usd

log = logging.getLogger("goulot.llm.batch")


@dataclass
class BatchResult:
    custom_id: str
    ok: bool
    message: dict[str, Any] | None  # Message as a JSON-safe dict when ok
    error: str = ""


class BatchBackend(Protocol):
    def submit(self, requests: list[tuple[str, dict[str, Any]]]) -> str: ...

    def ended(self, batch_id: str) -> bool: ...

    def results(self, batch_id: str) -> list[BatchResult]: ...


class AnthropicBatchBackend:
    def __init__(self, client: anthropic.Anthropic) -> None:
        self.client = client

    def submit(self, requests: list[tuple[str, dict[str, Any]]]) -> str:
        batch = self.client.messages.batches.create(
            requests=[{"custom_id": cid, "params": params} for cid, params in requests]  # type: ignore[typeddict-item]
        )
        return str(batch.id)

    def ended(self, batch_id: str) -> bool:
        return str(self.client.messages.batches.retrieve(batch_id).processing_status) == "ended"

    def results(self, batch_id: str) -> list[BatchResult]:
        out = []
        for entry in self.client.messages.batches.results(batch_id):
            result = entry.result
            if result.type == "succeeded":
                out.append(BatchResult(entry.custom_id, True, result.message.to_dict(mode="json")))
            else:
                out.append(BatchResult(entry.custom_id, False, None, str(result.type)))
        return out


class ReplayBatchBackend:
    """Answers from recordings (`<directory>/<custom_id>.json`, same format as RecordingBackend) or a dict."""

    def __init__(self, directory: Path | None = None, responses: dict[str, dict[str, Any]] | None = None) -> None:
        self.directory = directory
        self.responses = dict(responses or {})
        self.batches: dict[str, list[tuple[str, dict[str, Any]]]] = {}

    def submit(self, requests: list[tuple[str, dict[str, Any]]]) -> str:
        batch_id = "msgbatch_replay_" + uuid.uuid4().hex[:12]
        self.batches[batch_id] = list(requests)
        return batch_id

    def ended(self, batch_id: str) -> bool:
        return True

    def _answer(self, custom_id: str) -> dict[str, Any] | None:
        if custom_id in self.responses:
            answer: dict[str, Any] = self.responses[custom_id]
            return answer
        if self.directory is not None and (self.directory / f"{custom_id}.json").is_file():
            data: dict[str, Any] = json.loads((self.directory / f"{custom_id}.json").read_text(encoding="utf-8"))
            response: dict[str, Any] = data["response"]
            return response
        return None

    def results(self, batch_id: str) -> list[BatchResult]:
        out = []
        for cid, _ in self.batches.get(batch_id, []):
            answer = self._answer(cid)
            out.append(BatchResult(cid, answer is not None, answer, "" if answer else "errored"))
        return out


class RecordingBatchBackend:
    """`goulot dev record`: forwards to a live backend and writes each result as `<custom_id>.json`."""

    def __init__(self, inner: BatchBackend, directory: Path) -> None:
        self.inner = inner
        self.directory = directory
        self.models: dict[str, str] = {}

    def submit(self, requests: list[tuple[str, dict[str, Any]]]) -> str:
        for cid, params in requests:
            self.models[cid] = str(params.get("model", ""))
        return self.inner.submit(requests)

    def ended(self, batch_id: str) -> bool:
        return self.inner.ended(batch_id)

    def results(self, batch_id: str) -> list[BatchResult]:
        results = self.inner.results(batch_id)
        self.directory.mkdir(parents=True, exist_ok=True)
        for r in results:
            if r.ok and r.message is not None:
                record = {"key": r.custom_id, "model": self.models.get(r.custom_id, ""), "response": r.message}
                (self.directory / f"{r.custom_id}.json").write_text(
                    json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True, default=str) + "\n",
                    encoding="utf-8")
        return results


def submit(db: Database, budget: Budget, backend: BatchBackend, purpose: str, budget_name: str,
           requests: list[tuple[str, dict[str, Any]]], max_cost_usd: float) -> str:
    budget.check(budget_name, max_cost_usd)
    batch_id = backend.submit(requests)
    with db.transaction() as conn:
        conn.execute("INSERT INTO llm_batches (id, purpose, status, request_count, submitted_at) VALUES "
                     "(?, ?, 'submitted', ?, ?)", (batch_id, purpose, len(requests), utcnow()))
    log.info("batch submitted: purpose=%s id=%s requests=%s", purpose, batch_id, len(requests))
    return batch_id


def collect(db: Database, budget: Budget, backend: BatchBackend, batch_id: str, purpose: str, budget_name: str,
            material_of: dict[str, str] | None = None) -> list[BatchResult] | None:
    """Results of an ended batch, billed at the Batch rate; None while it is still running."""
    if not backend.ended(batch_id):
        return None
    results = backend.results(batch_id)
    for r in results:
        message = r.message or {}
        usage = Usage.from_api(message.get("usage")) if r.ok else Usage()
        model = str(message.get("model") or "claude-haiku-5-5")
        cost = cost_usd(model, usage, batch=True) if r.ok else 0.0
        budget.record(CallRecord(budget_name, purpose, model, "batch", usage, cost,
                                 request_id=str(message.get("id") or ""),
                                 material_id=(material_of or {}).get(r.custom_id),
                                 stop_reason=str(message.get("stop_reason") or r.error), ok=r.ok))
    with db.transaction() as conn:
        conn.execute("UPDATE llm_batches SET status = 'collected', collected_at = ? WHERE id = ?",
                     (utcnow(), batch_id))
    return results


class ResumingBatchBackend:
    """`goulot dev record-watch` after an interruption: requests whose answer is already recorded are not sent
    again; their recorded answer is returned, marked `_replayed` (not billed twice)."""

    def __init__(self, inner: BatchBackend, directory: Path) -> None:
        self.inner = inner
        self.directory = directory
        self.local: dict[str, list[str]] = {}

    def submit(self, requests: list[tuple[str, dict[str, Any]]]) -> str:
        missing = [(c, p) for c, p in requests if not (self.directory / f"{c}.json").is_file()]
        known = [c for c, _ in requests if (self.directory / f"{c}.json").is_file()]
        batch_id = self.inner.submit(missing) if missing else "msgbatch_resumed_" + uuid.uuid4().hex[:12]
        self.local[batch_id] = known
        return batch_id

    def ended(self, batch_id: str) -> bool:
        return batch_id.startswith("msgbatch_resumed_") or self.inner.ended(batch_id)

    def results(self, batch_id: str) -> list[BatchResult]:
        out = [] if batch_id.startswith("msgbatch_resumed_") else self.inner.results(batch_id)
        for cid in self.local.get(batch_id, []):
            data = json.loads((self.directory / f"{cid}.json").read_text(encoding="utf-8"))
            out.append(BatchResult(cid, True, {**data["response"], "usage": {}}))
        return out
