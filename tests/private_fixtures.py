"""The fictitious Ma chaîne of `tests/fixtures/private/` (test CAS 107-13-1 only) and helpers to put it in a test
database. Its canary values must never leave the vault (docs/SPEC.md, section 13)."""

from __future__ import annotations

import copy
import json
from typing import Any

from goulot.api.context import AppContext
from goulot.private import service
from goulot.private.mychain import MyChain
from goulot.settings import REPO_ROOT
from tests.conftest import setup_vault

FIXTURE: dict[str, Any] = json.loads(
    (REPO_ROOT / "tests" / "fixtures" / "private" / "my_chain_107-13-1.json").read_text(encoding="utf-8"))
CANARIES: list[str] = FIXTURE["canaries"]


def chain_data(shares: tuple[float, float] | None = None) -> dict[str, Any]:
    """The fixture's document; `shares`: (via the distributor, via the European producer) in % of the volume."""
    data = copy.deepcopy(FIXTURE["chain"])
    if shares is not None:
        data["links"][1]["share_pct"], data["links"][2]["share_pct"] = shares
    return data


def doc(shares: tuple[float, float] | None = None) -> MyChain:
    return MyChain.model_validate(chain_data(shares))


def validated_material(ctx: AppContext) -> str:
    from tests.test_collect import validated_chain

    return validated_chain(ctx)


def with_my_chain(ctx: AppContext, shares: tuple[float, float] | None = None, *, vault: bool = True) -> str:
    """A validated test material with the fixture's Ma chaîne saved in a fresh vault. Returns the material id."""
    if vault:
        setup_vault(ctx)
    material_id = validated_material(ctx)
    revision, result = service.save(ctx.db, ctx.vault, material_id, doc(shares))
    assert revision == 1, result.errors
    return material_id


def raw_contains_canary(raw: bytes | str) -> list[str]:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    return [c for c in CANARIES if c.encode("utf-8") in data]
