"""Lot 8: the Fiabilité screen as in the mockup (docs/SPEC.md, sections 7 and 8). Advices put in words, calibration on
a known set, « Rejouer cette journée », public export. Everything from the ledger, no AI call."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.ledger import chain, outcomes, replay, report
from goulot.ledger.rules import Series
from tests.authenticator import SoftAuthenticator
from tests.conftest import enroll, login, setup_vault, unlock
from tests.test_collect import NOW
from tests.test_screens_api import prepared

ISSUED = datetime(2026, 8, 13, 12, tzinfo=UTC)


def rising() -> Series:
    """1 000 for a month, then +1 % a session from 14 August: the 5 % threshold is crossed on 18 August."""
    days = [date(2026, 7, 1) + timedelta(days=i) for i in range(120)]
    points = [(d, 1000.0 if d <= date(2026, 8, 13) else 1000.0 * 1.01 ** (d - date(2026, 8, 13)).days)
              for d in days]
    return Series(points, rank=1, name="Test")


def _material(conn: object, name: str) -> str:
    material_id = "00000000-0000-4000-8000-000000000001"
    conn.execute("INSERT INTO materials (id, name, cas, status, created_at, updated_at) "  # type: ignore[attr-defined]
                 "VALUES (?, ?, '107-13-1', 'validated', ?, ?)", (material_id, name, ISSUED.isoformat(),
                                                                  ISSUED.isoformat()))
    return material_id


def test_advices_in_words_with_their_range_and_hashes(ctx: AppContext) -> None:
    with ctx.db.transaction() as conn:
        m = _material(conn, "Acrylonitrile")
        h = chain.append(conn, "hypothesis", {
            "analysis_id": "a1", "event_id": "EVT-1", "hypothesis_id": "h1", "instrument_id": "fred:ACN",
            "metric": "outright_price", "direction": "up", "horizon": "20d", "probability": 0.6, "move_p10": 3.0,
            "move_p90": 9.0, "move_unit": "percent", "versions": {"model": "sonnet", "prompt": "impact_v1",
                                                                  "schema": "v1"}}, material_id=m, at=ISSUED)
        v = chain.append(conn, "verdict", {"scope": "market", "verdict": "pot", "label": "Problème potentiel",
                                           "pressure": 1, "x_weeks": 5.0, "event_ids": ["EVT-1"],
                                           "versions": {"analyses": ["sonnet|impact_v1|v1"]}}, material_id=m,
                         at=ISSUED)
        outcomes.resolve(conn, date(2026, 11, 1), lambda _: rising())
        rows = {r["seq"]: r for r in report.rows(conn, m, lambda _: rising())}
    verdict = rows[v]
    assert verdict["announced"] == "hausse de 5 % avant le 1 oct."  # 13 August + max(X, 4) + 2 weeks
    assert verdict["deadline"] == "2026-10-01" and verdict["good"] is True
    assert verdict["observed"] == "+5,1 % franchi le 18 août"
    assert verdict["band"] == {"seq": h, "p10": 3.0, "p90": 9.0, "probability": 0.6, "start": "2026-08-13",
                               "observed_pct": rows[h]["outcome"]["observed_pct"], "hit": False}
    hyp = rows[h]
    assert hyp["announced"] == "+3 % à +9 % sous 20 séances, probabilité 60 %"
    assert hyp["deadline"] == "2026-09-02" and hyp["deadline_estimated"] is False  # 20th day after the advice
    assert hyp["version"] == "sonnet · impact_v1 · v1"
    assert verdict["prev_hash"] == hyp["hash"]  # chained to the entry before it


def test_version_change_is_marked_once() -> None:
    def hyp(day: str, version: str) -> dict[str, object]:
        return {"kind": "hypothesis", "at": day + "T00:00:00+00:00", "version": version}

    advices = [hyp("2026-08-01", "a"), hyp("2026-08-02", "a"), hyp("2026-09-01", "b"), hyp("2026-09-02", "b")]
    assert report.version_changes(advices) == [{"day": "2026-09-01", "version": "b"}]


def _hypotheses(ctx: AppContext, groups: list[tuple[float, int, int]]) -> None:
    """(probability, how many, how many went the announced way)."""
    with ctx.db.transaction() as conn:
        for p, n, right in groups:
            for i in range(n):
                seq = chain.append(conn, "hypothesis", {"event_id": "E", "horizon": "20d", "probability": p,
                                                        "move_p10": 0, "move_p90": 1, "direction": "up"},
                                   material_id="m", at=ISSUED)
                chain.append(conn, "outcome", {"of_seq": seq, "of_kind": "hypothesis", "result": "range_hit",
                                               "detail": "sens juste" if i < right else "sens non confirmé",
                                               "observed_pct": 1.0, "on_index": False}, material_id="m", at=ISSUED)


def test_calibration_on_a_known_set(ctx: AppContext) -> None:
    _hypotheses(ctx, [(0.8, 20, 16), (0.2, 20, 4)])
    with ctx.db.connect() as conn:
        c = report.calibration(conn, None)
    # Brier: 32 × 0.2² + 8 × 0.8² over 40 = 0.16; always announcing the mean rate (0.5) scores 0.25.
    assert (c["resolved"], c["brier"], c["base_brier"], c["base_rate"]) == (40, 0.16, 0.25, 0.5)
    assert c["bins"] == [{"p": 0.2, "observed": 0.2, "n": 20}, {"p": 0.8, "observed": 0.8, "n": 20}]
    assert c["show_brier"] is True and c["show_diagram"] is False  # diagram from 50
    assert c["verdict"].startswith("Mieux que le taux de base") and "50" in c["verdict"]
    _hypotheses(ctx, [(0.9, 10, 5)])
    with ctx.db.connect() as conn:
        c = report.calibration(conn, None)
    assert c["show_diagram"] is True and "surestimer" in c["verdict"]


def test_too_few_hypotheses_show_no_score(ctx: AppContext) -> None:
    _hypotheses(ctx, [(0.6, 12, 7)])
    with ctx.db.connect() as conn:
        c = report.calibration(conn, None)
    assert c["show_brier"] is False and c["verdict"] == "12 hypothèses résolues : le score s'affiche à partir de 30."


def _backdate(ctx: AppContext) -> None:
    """The test pipeline stamps analyses and chain rows with the real clock, after the frozen NOW of the verdict;
    on a Mac they come before it."""
    before = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE analyses SET done_at = ? WHERE done_at > ?", (before, before))
        conn.execute("UPDATE chain_nodes SET valid_from = ? WHERE valid_from > ?", (before, before))
        conn.execute("UPDATE chain_edges SET valid_from = ? WHERE valid_from > ?", (before, before))


def test_replay_finds_the_same_verdict_and_shows_a_change(client: TestClient, ctx: AppContext) -> None:
    material_id, event_id = prepared(ctx)
    _backdate(ctx)
    login(client, ctx)
    data = client.get(f"/api/reliability?material_id={material_id}").json()
    (seq,) = [a["seq"] for a in data["advices"] if a["kind"] == "verdict" and a["verdict"] == "pot"]
    same = client.get(f"/api/reliability/advice/{seq}/replay").json()
    assert same["same"] is True and same["recorded"] == same["replayed"] and same["events"] >= 1
    dossier = client.get(f"/api/reliability/advice/{seq}").json()
    assert [k["id"] for k in dossier["knew"]] == [event_id] and dossier["knew"][0]["sources"]
    # The test copy changes: the analysis is withdrawn. The replay says so instead of matching.
    with ctx.db.transaction() as conn:
        conn.execute("UPDATE analyses SET state = 'rejected' WHERE event_id = ?", (event_id,))
    changed = client.get(f"/api/reliability/advice/{seq}/replay").json()
    assert changed["same"] is False and changed["differences"] and changed["replayed"] != changed["recorded"]
    with ctx.db.connect() as conn:
        assert chain.verify(conn).ok  # read only


def test_inputs_check_needs_the_vault_and_finds_a_change(client: TestClient, ctx: AppContext,
                                                         authenticator: SoftAuthenticator) -> None:
    setup_vault(ctx)
    prepared(ctx)
    csrf = login(client, ctx)
    enroll(client, ctx, csrf, authenticator)
    token = unlock(client, csrf, authenticator)
    with ctx.db.connect() as conn:
        (seq,) = [e["seq"] for e in chain.entries(conn, kinds=("verdict",)) if e["verdict"] == "pot"]
    url = f"/api/private/reliability/advice/{seq}/inputs-check"
    assert client.get(url).status_code == 403  # no vault token
    assert client.get(url, headers={"X-Vault-Token": token}).json()["status"] == "intact"
    with ctx.db.transaction() as conn:
        blob = bytearray(conn.execute("SELECT inputs FROM verdicts WHERE current = 1").fetchone()[0])
        blob[-1] ^= 1
        conn.execute("UPDATE verdicts SET inputs = ? WHERE current = 1", (bytes(blob),))
    assert client.get(url, headers={"X-Vault-Token": token}).json()["status"] == "altered"


def test_replay_refuses_what_is_not_a_market_verdict(ctx: AppContext) -> None:
    with ctx.db.transaction() as conn:
        seq = chain.append(conn, "verdict", {"scope": "personal"}, material_id="m", at=ISSUED)
        try:
            replay.replay(conn, seq)
        except replay.ReplayError:
            pass
        else:
            raise AssertionError("a personal verdict must not replay")


def test_export_lists_market_advices_with_their_hashes(client: TestClient, ctx: AppContext) -> None:
    prepared(ctx)
    login(client, ctx)
    r = client.get("/api/reliability/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    table = list(csv.reader(io.StringIO(r.text.lstrip("﻿")), delimiter=";"))
    assert table[0] == list(report.CSV_COLUMNS) and len(table) >= 2
    with ctx.db.connect() as conn:
        heads = {str(s): h for s, _, h in conn.execute("SELECT seq, prev_hash, hash FROM ledger")}
    assert all(row[11] == heads[row[0]] for row in table[1:])


def test_a_spreadsheet_formula_is_neutralized() -> None:
    assert report._cell("=HYPERLINK(\"x\")") == "'=HYPERLINK(\"x\")"
    assert report._cell("-cmd") == "'-cmd" and report._cell("@x") == "'@x"
    assert report._cell("+5,3 %") == "+5,3 %" and report._cell("-2 %") == "-2 %"


def test_replay_words() -> None:
    assert replay._words("pot", 5.5, 1) == "Problème potentiel à 5,5 sem. (pression +1)"
    assert replay._words("ras", None, 0) == "Rien à signaler"
