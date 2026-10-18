"""Phase 3 criterion: the replay of a fixed set of articles with recorded AI answers gives the expected events
and verdict; booby-trapped articles and fake sites do not change the verdict (docs/SPEC.md, section 13)."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from goulot.api.context import build_context
from goulot.cli import watch_scenario
from goulot.llm.batch import ReplayBatchBackend
from goulot.security.clock import ManualClock
from goulot.settings import Settings


def test_scenario_is_well_formed() -> None:
    scenario = watch_scenario.load()
    expects = {a["expect"] for a in scenario["gdelt"]} | {d["expect"] for d in scenario["federal_register"]}
    assert expects == {"outage", "trap", "fake", "irrelevant", "policy"}
    assert all(a["url"].startswith("https://") for a in scenario["gdelt"])
    # Fictitious companies only: no real firm is said to have an accident in a public repository.
    titles = " ".join(a["title"] for a in scenario["gdelt"])
    for real in ("INEOS", "Ascend", "Asahi", "Sinopec", "Formosa", "Anqore", "Sumitomo"):
        assert real not in titles


def test_recorded_watch_scenario_replays(tmp_path: Path) -> None:
    """Runs only once `goulot dev record-watch` has recorded the real answers."""
    if not watch_scenario.recorded():
        assert not any(watch_scenario.RECORDINGS.glob("tri_*.json"))  # nothing half-recorded either
        return
    expected = json.loads((watch_scenario.RECORDINGS / "summary.json").read_text(encoding="utf-8"))
    now = datetime.fromisoformat(watch_scenario.load()["now"])
    ctx = build_context(Settings(home=tmp_path / "h", log_dir_override=tmp_path / "l"), ManualClock(now.timestamp()))
    got = watch_scenario.run(ctx.db, ctx.budget, ctx.vault, ReplayBatchBackend(watch_scenario.RECORDINGS),
                             poll=lambda: None)
    assert got["articles"] == expected["articles"] and got["events"] == expected["events"]
    assert got["verdict"] == expected["verdict"]
    articles, events = got["articles"], {e["id"]: e for e in got["events"]}
    outage = {articles["outage_reuters"]["event"], articles["outage_icis"]["event"]}
    assert len(outage) == 1 and None not in outage
    (outage_id,) = outage
    assert events[outage_id]["confirmation"] == "confirmed" and events[outage_id]["lineages"] >= 2
    trap = articles["trap"]
    assert trap["status"] in ("quarantined", "irrelevant") or events[trap["event"]]["confirmation"] != "confirmed"
    for fake in ("fake_1", "fake_2", "fake_3"):
        event = articles[fake]["event"]
        assert event is None or events[event]["confirmation"] == "early_signal"  # fake sites confirm nothing
    for off in ("commentary", "offtopic"):
        assert articles[off]["status"] == "irrelevant"
    if got["verdict"] and got["verdict"]["verdict"] == "ant":
        assert outage_id in json.loads(got["verdict"]["event_ids"])  # never because of the trap or the fakes
