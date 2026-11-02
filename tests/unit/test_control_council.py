"""Council engine tests.

Every provider call here is a *mocked* browser runtime: a fake connector that
returns canned JSON. That is deliberate - these tests prove the engine's logic
(scheduling rounds, scoring, contracts, honest failure recording) and never
claim that a real ChatGPT/DeepSeek/Claude/Gemini/Perplexity/Sarvam round-trip
was verified. Real council runs need signed-in browser accounts and are checked
manually; see tests/integration for the HTTP surface.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from forgemesh_control.accounts import AccountError
from forgemesh_control.audit import AuditService
from forgemesh_control.browser import BrowserUnavailable
from forgemesh_control.config import ControlSettings
from forgemesh_control.council import (
    COUNCIL_PROVIDERS,
    DISAGREEMENT_PENALTY,
    HACKATHON_MIN_STATEMENTS,
    MAX_DIVERGENCE_ROUNDS,
    CouncilError,
    CouncilService,
    build_decision,
    decision_contract,
    extract_json,
    normalise_critique,
    normalise_statements,
    proposal_details,
    score_proposals,
)
from forgemesh_control.events import EventStore
from forgemesh_control.models import MeetingRow, MessageRow, ProposalRow
from forgemesh_control.projects import ProjectService
from forgemesh_control.storage import ControlDatabase


def _settings(tmp_path: Path, **overrides: Any) -> ControlSettings:
    base: dict[str, Any] = {
        "data_dir": tmp_path / "data",
        "config_dir": tmp_path / "config",
        "auto_install_agents": False,
        "council_round_timeout_seconds": 20,
        "council_rounds": 4,
        "council_max_parallel": 2,
    }
    base.update(overrides)
    return ControlSettings(**base)


class FakeBrowser:
    """Stands in for BrowserConnector; records every prompt it was given."""

    def __init__(
        self,
        *,
        accounts: list[dict[str, Any]] | None = None,
        answer: Any = None,
        down: bool = False,
    ) -> None:
        self._accounts = accounts if accounts is not None else []
        self._answer = answer
        self.down = down
        self.calls: list[dict[str, Any]] = []

    async def providers(self) -> list[dict[str, Any]]:
        if self.down:
            raise BrowserUnavailable("Cannot reach ForgeMesh Browser at http://127.0.0.1:8765.")
        return [{"id": row["provider_id"], "name": row["provider_id"]} for row in self._accounts]

    async def accounts(self, *, provider: str | None = None) -> list[dict[str, Any]]:
        if self.down:
            raise BrowserUnavailable("Cannot reach ForgeMesh Browser at http://127.0.0.1:8765.")
        if provider:
            return [row for row in self._accounts if row["provider_id"] == provider]
        return list(self._accounts)

    async def ask(
        self, provider_id: str, prompt: str, *, profile_id: str | None = None, **_: Any
    ) -> dict[str, Any]:
        self.calls.append({"provider_id": provider_id, "prompt": prompt, "profile": profile_id})
        if self.down:
            raise BrowserUnavailable("Cannot reach ForgeMesh Browser.")
        answer = self._answer
        if callable(answer):
            answer = answer(provider_id, prompt)
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict) and answer.get("__raise__"):
            raise BrowserUnavailable(str(answer["__raise__"]))
        return {"text": answer if isinstance(answer, str) else json.dumps(answer)}


def _account(provider_id: str, status: str = "available") -> dict[str, Any]:
    return {
        "provider_id": provider_id,
        "profile_id": f"prof_{provider_id}",
        "status": status,
    }


class FakeBrain:
    """Returns different text per purpose and records the prompts it saw."""

    enabled = True

    def __init__(self, brief: str = "SHARED BRIEF: be precise.", polish: str = "POLISHED ANSWER") -> None:
        self.brief = brief
        self.polish = polish
        self.prompts: list[tuple[str, str]] = []

    async def run(self, prompt: str, *, purpose: str, timeout_seconds: int | None = None):
        from forgemesh_control.brain import BrainResult

        self.prompts.append((purpose, prompt))
        if purpose == "meeting brief":
            return BrainResult(ok=True, text=self.brief, kind="qoder")
        return BrainResult(ok=True, text=self.polish, kind="opencode")



def _live(provider_id: str, profile_id: str, status: str = "available") -> dict[str, Any]:
    return {"provider_id": provider_id, "profile_id": profile_id, "status": status}


def _configured(
    account_id: str,
    provider_id: str,
    label: str,
    *,
    enabled: bool = True,
    active: bool = False,
    role: str = "",
) -> dict[str, Any]:
    """One row as AiAccountService.list() hands it to the council."""
    return {
        "id": account_id,
        "provider_id": provider_id,
        "label": label,
        "email": "",
        "profile_id": f"prof_{account_id}",
        "council_role": role,
        "active": active,
        "enabled": enabled,
        "status": "verified" if enabled else "logged_out",
        "verified": enabled,
        "last_login_at": None,
        "last_verified_at": None,
        "last_error": None if enabled else "session expired",
        "last_error_at": None,
        "created_at": None,
        "updated_at": None,
    }


class FakeAccounts:
    """Stands in for AiAccountService: only the accounts the operator registered."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = {row["id"]: row for row in rows}

    async def list(self, *, provider_id: str | None = None) -> list[dict[str, Any]]:
        return [
            row
            for row in self._rows.values()
            if provider_id is None or row["provider_id"] == provider_id
        ]

    async def get(self, account_id: str) -> dict[str, Any]:
        row = self._rows.get(account_id)
        if row is None:
            raise AccountError(f"Unknown account: {account_id}")
        return row


def _service(
    tmp_path: Path,
    browser: FakeBrowser,
    accounts: Any = None,
    brain: Any = None,
    **overrides: Any,
):
    settings = _settings(tmp_path, **overrides)
    db = ControlDatabase(settings.db_path)
    events = EventStore(db)
    audit = AuditService(db, settings.logs_dir)
    projects = ProjectService(settings, db, events, audit)
    council = CouncilService(
        settings, db, events, audit, browser, projects, accounts=accounts, brain=brain
    )
    return settings, db, events, audit, projects, council


# --------------------------------------------------------------------- pure logic
def test_extract_json_survives_fences_and_prose() -> None:
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! Here it is:\n{"a": {"b": 2}}\nHope that helps.') == {
        "a": {"b": 2}
    }
    assert extract_json("I have no structured answer for you.") is None


def test_normalise_statements_drops_entries_without_a_claim() -> None:
    payload = {
        "statements": [
            {"claim": "", "title": "empty"},
            {"claim": "Use SQLite WAL"},
            "bare string becomes a claim",
            {"description": "prompts go through the browser runtime"},
        ]
    }
    rows = normalise_statements(payload)
    assert len(rows) == 3
    assert rows[0]["claim"] == "Use SQLite WAL"
    assert rows[0]["title"] == "Use SQLite WAL"[:80]
    assert rows[1]["claim"] == "bare string becomes a claim"
    assert rows[2]["title"] == "prompts go through the browser runtime"[:80]


def test_normalise_statements_keeps_evidence_source_or_marks_unspecified() -> None:
    rows = normalise_statements(
        {
            "statements": [
                {
                    "claim": "Ship the council engine",
                    "evidence": [
                        {"fact": "council.py exists", "source": "repo"},
                        {"fact": "no provider is simulated"},
                        "a plain string fact",
                    ],
                }
            ]
        }
    )
    evidence = rows[0]["evidence"]
    assert evidence[0] == {"fact": "council.py exists", "source": "repo"}
    assert evidence[1]["source"] == "unspecified"
    assert evidence[2] == {"fact": "a plain string fact", "source": "unspecified"}


def test_normalise_critique_keeps_only_scoreable_and_argued_items() -> None:
    payload = {
        "assessments": [
            {"proposal": "prp_1", "feasibility": 80, "impact": 70, "risk": 60, "evidence": 90},
            {"feasibility": 50},
            "not a dict",
        ],
        "disagreements": [
            {"proposal": "prp_1", "counter_claim": "single process cannot stream", "severity": "HIGH"},
            {"proposal": "prp_2", "counter_claim": "", "severity": "low"},
            {"proposal": "prp_3", "counter_claim": "ok", "severity": "nonsense"},
        ],
        "revisions": [{"proposal": "prp_1", "change": ""}, {"proposal": "prp_1", "change": "add gate"}],
    }
    critique = normalise_critique(payload)
    assert len(critique["assessments"]) == 1
    assert len(critique["disagreements"]) == 2
    assert critique["disagreements"][0]["severity"] == "high"
    assert critique["disagreements"][1]["severity"] == "medium"
    assert critique["revisions"] == [{"proposal": "prp_1", "change": "add gate"}]


def test_score_proposals_uses_weights_and_disagreement_penalty_not_votes() -> None:
    proposals = [
        {"id": "prp_a", "title": "A", "author": "gemini", "feasibility": 90, "confidence": 90},
        {"id": "prp_b", "title": "B", "author": "claude", "feasibility": 50, "confidence": 50},
    ]
    critiques = [
        {
            "provider_id": "claude",
            "assessments": [
                {"proposal": "prp_a", "feasibility": 90, "impact": 90, "risk": 90, "evidence": 90},
                {"proposal": "prp_b", "feasibility": 90, "impact": 90, "risk": 90, "evidence": 90},
            ],
            "disagreements": [
                {"proposal": "prp_a", "counter_claim": "x", "severity": "high"},
                {"proposal": "prp_b", "counter_claim": "y", "severity": "medium"},
            ],
        }
    ]
    ranking = score_proposals(proposals, critiques)
    by_id = {row["proposal_id"]: row for row in ranking}
    # identical rubric; different penalty -> ordering is driven by disagreement
    assert by_id["prp_a"]["rubric"] == by_id["prp_b"]["rubric"] == 90.0
    assert by_id["prp_a"]["penalty"] == pytest.approx(DISAGREEMENT_PENALTY["high"])
    assert by_id["prp_b"]["penalty"] == pytest.approx(DISAGREEMENT_PENALTY["medium"])
    assert ranking[0]["proposal_id"] == "prp_b"
    assert ranking[0]["rank"] == 1
    assert by_id["prp_a"]["objections"][0]["raised_by"] == "claude"
    assert by_id["prp_a"]["raters"] == ["claude"]


def test_score_proposals_leaves_unscored_proposal_at_the_bottom() -> None:
    ranking = score_proposals(
        [{"id": "prp_a", "title": "A", "author": "x"}, {"id": "prp_b", "title": "B", "author": "y"}],
        [
            {
                "provider_id": "z",
                "assessments": [
                    {"proposal": "prp_b", "feasibility": 40, "impact": 40, "risk": 40, "evidence": 40}
                ],
                "disagreements": [],
            }
        ],
    )
    assert [row["proposal_id"] for row in ranking] == ["prp_b", "prp_a"]
    assert ranking[1]["final"] is None


def test_build_decision_reports_method_and_keeps_votes_as_context() -> None:
    meeting = {"id": "mtg_1", "mode": "normal", "statements_required": 2}
    proposals = [
        {"id": "prp_a", "title": "A", "author": "gemini", "description": "do a"},
        {"id": "prp_b", "title": "B", "author": "claude", "description": "do b"},
    ]
    critiques = [
        {
            "provider_id": "claude",
            "assessments": [
                {"proposal": "prp_a", "feasibility": 70, "impact": 70, "risk": 70, "evidence": 70},
                {"proposal": "prp_b", "feasibility": 60, "impact": 60, "risk": 60, "evidence": 60},
            ],
            "disagreements": [],
        }
    ]
    # both participants *vote* for prp_b: the rubric still picks prp_a
    syntheses = [
        {"provider_id": "claude", "final_order": ["prp_b"], "unresolved": ["who deploys?"],
         "mitigations": [{"proposal": "prp_a", "action": "add a rollback step"}]},
        {"provider_id": "gemini", "final_order": ["prp_b"], "unresolved": [], "mitigations": []},
    ]
    decision = build_decision(meeting, proposals, critiques, syntheses)
    assert decision["selection_method"] == "weighted_rubric_with_disagreement_penalty"
    assert decision["ranking"][0]["proposal_id"] == "prp_a"
    assert decision["first_preference_counts"] == {"prp_b": 2}
    assert "not a vote" in decision["rationale"]
    assert decision["unresolved"] == ["who deploys?"]
    assert decision["mitigations"][0]["raised_by"] == "claude"
    assert decision["statements_collected"] == 2


def test_build_decision_without_any_proposal_has_no_winner() -> None:
    decision = build_decision(
        {"id": "mtg_2", "mode": "hackathon", "statements_required": 10},
        [],
        [],
        [],
    )
    assert decision["summary"].startswith("No decision")
    assert decision["selected_proposal"] is None
    assert decision["requires_mitigation"] is True
    contract = decision_contract(decision)
    assert contract["ok"] is False
    assert "no proposal was selected by this council" in contract["missing"]


def test_sole_proposal_is_selected_and_labelled_unvalidated() -> None:
    """One account and no peer critique is a real state, not a fake failure.

    The council must not claim "no proposal was produced" when a proposal exists,
    and must not imply the rubric approved it either.
    """
    decision = build_decision(
        {"id": "mtg_2", "mode": "normal", "statements_required": 1},
        [
            {
                "id": "prp_a",
                "title": "A",
                "author": "deepseek",
                "account_label": "Account 2",
                "description": "do a",
            }
        ],
        [],
        [],
    )
    assert decision["selected_proposal"] == "prp_a"
    assert decision["unvalidated"] is True
    assert "no other participant rated it" in decision["summary"]
    assert "Account 2" in decision["summary"]
    assert "not a vote" in decision["rationale"] or "never the decision" in decision["rationale"]
    assert decision["requires_mitigation"] is False
    contract = decision_contract(decision)
    assert contract["selected_account"] == "Account 2"
    assert contract["unvalidated"] is True
    assert "no peer critique" in contract["warnings"][0]


def test_decision_contract_blocks_a_winner_without_acceptance() -> None:
    decision = {
        "ranking": [{"proposal_id": "prp_a", "title": "A", "final": 80.0}],
        "requires_mitigation": False,
        "proposal_details": proposal_details(
            [
                {
                    "id": "prp_a",
                    "title": "A",
                    "author": "gemini",
                    "description": "do a",
                    "risks": [],
                    "responses": [{"steps": ["write the route"], "acceptance": []}],
                }
            ]
        ),
    }
    contract = decision_contract(decision)
    assert contract["ok"] is False
    assert contract["missing"] == ["winning proposal has no acceptance criteria"]
    assert contract["steps"] == ["write the route"]


def test_decision_contract_passes_only_on_a_complete_winner() -> None:
    details = proposal_details(
        [
            {
                "id": "prp_a",
                "title": "A",
                "author": "gemini",
                "description": "do a",
                "risks": ["rate limits"],
                "responses": [
                    {"steps": ["write route", "add tests"], "acceptance": ["pytest passes"]}
                ],
            }
        ]
    )
    contract = decision_contract(
        {
            "ranking": [{"proposal_id": "prp_a", "title": "A", "final": 77.0}],
            "requires_mitigation": False,
            "proposal_details": details,
        }
    )
    assert contract == {
        "ok": True,
        "missing": [],
        "warnings": [],
        "unvalidated": False,
        "selected_account": None,
        "selected_proposal": "prp_a",
        "selected_title": "A",
        "steps": ["write route", "add tests"],
        "acceptance": ["pytest passes"],
        "risks": ["rate limits"],
    }


# ------------------------------------------------------------------------- roster
@pytest.mark.asyncio
async def test_roster_reports_available_and_unavailable_with_real_reasons(
    tmp_path: Path,
) -> None:
    accounts = [
        _account("gemini"),
        _account("claude", "unconfigured"),
        {"provider_id": "deepseek", "profile_id": "", "status": "available"},
    ]
    _s, _db, _e, _a, _p, council = _service(tmp_path, FakeBrowser(accounts=accounts))
    await _db.init()
    try:
        roster = await council.roster()
        by_id = {row["provider_id"]: row for row in roster["providers"]}
        assert [row["provider_id"] for row in roster["providers"]] == list(COUNCIL_PROVIDERS)
        assert by_id["gemini"]["status"] == "available"
        assert by_id["gemini"]["profile_id"] == "prof_gemini"
        assert by_id["claude"]["status"] == "unavailable"
        assert "unconfigured" in by_id["claude"]["reason"]
        # an "available" account with no profile cannot be driven
        assert by_id["deepseek"]["status"] == "unavailable"
        assert by_id["perplexity"]["reason"] == "no account registered in ForgeMesh Browser"
        assert roster["connected"] is True
        assert [row["provider_id"] for row in roster["available"]] == ["gemini"]
    finally:
        await _db.close()


@pytest.mark.asyncio
async def test_roster_says_unknown_when_the_browser_runtime_is_down(tmp_path: Path) -> None:
    _s, db, _e, _a, _p, council = _service(tmp_path, FakeBrowser(down=True))
    await db.init()
    try:
        roster = await council.roster()
        assert roster["connected"] is False
        assert roster["available"] == []
        assert all(row["status"] == "unknown" for row in roster["providers"])
        assert "ForgeMesh Browser" in roster["error"]
    finally:
        await db.close()


# ---------------------------------------------------------------------- meetings
@pytest.mark.asyncio
async def test_hackathon_mode_asks_for_ten_statements_and_normal_does_not(
    tmp_path: Path,
) -> None:
    _s, db, _e, _a, _p, council = _service(tmp_path, FakeBrowser(accounts=[_account("gemini")]))
    await db.init()
    try:
        hackathon = await council.create_meeting(agenda="Build the control center", mode="hackathon")
        normal = await council.create_meeting(agenda="Build the control center", mode="normal")
        assert hackathon["mode"] == "hackathon"
        assert hackathon["rounds_total"] >= 3
        assert normal["rounds_total"] <= 3
        assert hackathon["id"] != normal["id"]
        with pytest.raises(Exception) as bad_mode:
            await council.create_meeting(agenda="x", mode="vibes")
        assert "Unknown council mode" in str(bad_mode.value)
        with pytest.raises(Exception) as no_agenda:
            await council.create_meeting(agenda="   ")
        assert "needs an agenda" in str(no_agenda.value)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_meeting_fails_without_simulating_a_council(tmp_path: Path) -> None:
    browser = FakeBrowser(accounts=[_account("gemini", "unconfigured")])
    _s, db, _e, _a, _p, council = _service(tmp_path, browser)
    await db.init()
    try:
        meeting = await council.create_meeting(agenda="Decide the storage layer")
        await council._run(meeting["id"])
        done = await council.get_meeting(meeting["id"])
        assert done["status"] == "failed"
        assert "fmb accounts login" in (done["summary"] or "")
        # the roster verdict is stored per provider, so the UI can show why
        assert {row["provider_id"]: row["status"] for row in done["participants"]}["gemini"] == (
            "unavailable"
        )
        assert browser.calls == []
        assert done["proposals"] == []
        assert done["decision"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_ask_json_retries_once_and_keeps_the_unstructured_reply(tmp_path: Path) -> None:
    calls: list[str] = []

    def answer(_provider: str, prompt: str) -> str:
        calls.append(prompt)
        if "STRICT RETRY" in prompt:
            return "still prose, no json"
        return "Here is my answer, honestly."

    browser = FakeBrowser(accounts=[_account("gemini")], answer=answer)
    _s, db, _e, _a, _p, council = _service(tmp_path, browser)
    await db.init()
    try:
        payload, result = await council._ask_json(
            "mtg_x", "gemini", "prof_gemini", "original prompt", "statements"
        )
        assert payload is None
        assert len(calls) == 2
        assert "STRICT RETRY" in calls[1]
        assert "not parseable as statements JSON" in result["error"]
        # The dashboard shows the CLI's own words instead of a bare verdict.
        assert "reply starts: still prose, no json" in result["error"]
        assert result["unstructured"] == "still prose, no json"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_provider_timeout_is_recorded_as_a_failure_not_a_guess(tmp_path: Path) -> None:
    async def slow_ask(*_a: Any, **_k: Any) -> dict[str, Any]:
        import asyncio

        await asyncio.sleep(0.5)
        return {"text": "{}"}

    browser = FakeBrowser(accounts=[_account("gemini")])
    browser.ask = slow_ask  # type: ignore[method-assign]
    settings, db, _e, _a, _p, council = _service(tmp_path, browser)
    # the configured floor is 20s; this test only needs the timeout branch
    settings.council_timeout_seconds = 0.2
    await db.init()
    try:
        result = await council._call_provider("mtg_x", "gemini", "prof_gemini", "prompt")
        assert result["ok"] is False
        assert "within 0.2s" in result["error"]
    finally:
        await db.close()


# --------------------------------------------------------- end-to-end (mocked AI)
def _statements_json(provider: str, count: int) -> dict[str, Any]:
    return {
        "statements": [
            {
                "title": f"{provider} approach {index}",
                "claim": f"Ship the council engine via {provider} take {index}.",
                "reasoning": "It reuses the browser runtime that is already running.",
                "evidence": [{"fact": "council.py already exists", "source": "repository"}],
                "steps": ["write the route", "wire the runtime", "add tests"],
                "acceptance": [".venv/bin/pytest -q passes", "ruff check is clean"],
                "feasibility": 70 + index,
                "confidence": 60 + index,
                "risks": ["provider layout drift"],
            }
            for index in range(1, count + 1)
        ]
    }


def _critique_json(proposal_ids: list[str]) -> dict[str, Any]:
    if not proposal_ids:
        return {"assessments": [], "agreements": [], "disagreements": [], "revisions": []}
    return {
        "assessments": [
            {
                "proposal": pid,
                "feasibility": 80,
                "impact": 70,
                "risk": 60,
                "evidence": 75,
            }
            for pid in proposal_ids
        ],
        "agreements": [{"proposal": proposal_ids[0], "why": "matches the existing runtime"}],
        "disagreements": [
            {
                "proposal": proposal_ids[-1],
                "counter_claim": "it assumes a single process can stream every provider",
                "evidence": "per-profile locking is required",
                "severity": "high",
            }
        ],
        "revisions": [{"proposal": proposal_ids[0], "change": "add an approval gate before runs"}],
        "own_position": "I would keep the browser runtime untouched.",
    }


def _synthesis_json(proposal_ids: list[str]) -> dict[str, Any]:
    if not proposal_ids:
        return {"final_order": [], "unresolved": [], "mitigations": [], "narrative": "nothing"}
    return {
        "final_order": proposal_ids,
        "unresolved": ["who runs the migrations in production?"],
        "mitigations": [{"proposal": proposal_ids[0], "action": "gate implementation on approval"}],
        "narrative": "The first proposal delivers most of the agenda; the last one is blocked.",
    }


def _routing_answer(provider: str, prompt: str) -> dict[str, Any]:
    if "Propose" in prompt and "distinct solution statement" in prompt:
        return _statements_json(provider, 2)
    if "Final round." in prompt:
        ids = _ids_from_digest(prompt)
        return _synthesis_json(ids)
    ids = _ids_from_digest(prompt)
    return _critique_json(ids)


def _ids_from_digest(prompt: str) -> list[str]:
    import re

    return re.findall(r"id=(prop_[0-9a-f]+)", prompt)


@pytest.mark.asyncio
async def test_mocked_full_meeting_produces_a_scored_decision(tmp_path: Path) -> None:
    accounts = [_account("gemini"), _account("claude")]
    browser = FakeBrowser(accounts=accounts, answer=_routing_answer)
    _s, db, _e, _a, _p, council = _service(tmp_path, browser)
    await db.init()
    try:
        meeting = await council.create_meeting(
            agenda="Design the council engine", mode="normal", providers=["gemini", "claude"], rounds=3
        )
        await council.start(meeting["id"])
        # start() launches a background task; wait for it the way the server does
        task = council._tasks.get(meeting["id"])
        assert task is not None
        await task

        done = await council.get_meeting(meeting["id"])
        assert done["status"] == "completed", done["summary"]
        assert len(done["proposals"]) == 4, "two statements from each of two participants"
        chosen = {
            row["provider_id"]: row["status"] for row in done["participants"] if row["included"]
        }
        assert chosen == {"gemini": "responded", "claude": "responded"}
        # providers with no signed-in account are recorded, never simulated
        assert any(
            row["status"] == "unavailable" and not row["included"] for row in done["participants"]
        )
        assert done["decision"] is not None
        scores = done["decision"]["scores"]
        assert scores["selection_method"] == "weighted_rubric_with_disagreement_penalty"
        assert any(row["final"] is not None for row in scores["ranking"])
        # the high-severity disagreement is preserved, not averaged away
        assert scores["disagreements"]
        assert scores["unresolved"] == ["who runs the migrations in production?"]
        assert council.is_running(meeting["id"]) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_a_silent_provider_does_not_cost_the_critique_round(tmp_path: Path) -> None:
    """One provider failing statements used to turn R2 into a divergence round,
    leaving R3 as synthesis - so nobody ever reviewed the proposals."""

    def answer(provider: str, prompt: str) -> Any:
        if provider == "deepseek" and "Propose" in prompt:
            return "DeepSeek returned prose instead of the statements contract"
        return _routing_answer(provider, prompt)

    accounts = [_account("chatgpt"), _account("gemini"), _account("deepseek")]
    browser = FakeBrowser(accounts=accounts, answer=answer)
    _s, db, _e, _a, _p, council = _service(tmp_path, browser)
    await db.init()
    try:
        meeting = await council.create_meeting(
            agenda="Suggest a unique name",
            providers=["chatgpt", "gemini", "deepseek"],
            rounds=3,
        )
        await council._run(meeting["id"])
        done = await council.get_meeting(meeting["id"])
        kinds_by_round = {
            (row["round"], row["kind"])
            for row in done["messages"]
            if row["provider_id"] != "system"
        }
        assert (1, "statements") in kinds_by_round
        assert (2, "critique") in kinds_by_round, "the critique round must survive"
        assert (3, "synthesis") in kinds_by_round
        assert len(done["proposals"]) == 4, "chatgpt and gemini each proposed twice"
        assert done["decision"] is not None
        assert done["decision"]["scores"]["unvalidated"] is False
        assert all(row["final"] is not None for row in done["decision"]["scores"]["ranking"])
        assert "only proposal" not in done["decision"]["summary"]
        assert council.is_running(meeting["id"]) is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_every_round_message_is_stored_with_provider_and_round(tmp_path: Path) -> None:
    accounts = [_account("gemini"), _account("claude")]
    browser = FakeBrowser(accounts=accounts, answer=_routing_answer)
    _s, db, _e, _a, _p, council = _service(tmp_path, browser)
    await db.init()
    try:
        meeting = await council.create_meeting(
            agenda="Design the council engine", mode="normal", rounds=3
        )
        await council._run(meeting["id"])
        async with db.session_scope() as session:
            messages = (
                (
                    await session.execute(
                        select(MessageRow).where(MessageRow.meeting_id == meeting["id"])
                    )
                )
                .scalars()
                .all()
            )
            proposals = (
                (
                    await session.execute(
                        select(ProposalRow).where(ProposalRow.meeting_id == meeting["id"])
                    )
                )
                .scalars()
                .all()
            )
            meeting_row = await session.get(MeetingRow, meeting["id"])
        kinds = {row.kind for row in messages}
        assert {"statements", "critique", "synthesis"} <= kinds
        assert all(row.provider_id in {"gemini", "claude"} for row in messages)
        assert all(row.content for row in messages), "no empty transcript rows"
        # revisions are attached to the proposal they amend, keeping the authorship
        assert proposals
        assert meeting_row is not None and meeting_row.status == "completed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_hackathon_run_diverges_until_it_has_ten_statements(tmp_path: Path) -> None:
    accounts = [_account("gemini")]
    browser = FakeBrowser(accounts=accounts, answer=_routing_answer)
    _s, db, _e, _a, _p, council = _service(tmp_path, browser)
    await db.init()
    try:
        meeting = await council.create_meeting(
            agenda="Ten ways to build the mesh", mode="hackathon", rounds=4
        )
        await council._run(meeting["id"])
        done = await council.get_meeting(meeting["id"])
        assert done["context"]["statements_required"] == HACKATHON_MIN_STATEMENTS
        async with db.session_scope() as session:
            statement_rounds = (
                await session.execute(
                    select(MessageRow.round).where(
                        MessageRow.meeting_id == meeting["id"], MessageRow.kind == "statements"
                    )
                )
            ).all()
        # 2 statements per round from a single provider cannot reach ten, so the
        # engine re-runs the divergence round up to its cap instead of pretending.
        assert len({row[0] for row in statement_rounds}) == MAX_DIVERGENCE_ROUNDS + 1
        assert len(done["proposals"]) == 2 * (MAX_DIVERGENCE_ROUNDS + 1)
        assert done["decision"]["scores"]["statements_required"] == HACKATHON_MIN_STATEMENTS
        assert done["decision"]["scores"]["statements_collected"] == len(done["proposals"])
        assert done["decision"]["scores"]["divergence_rounds_used"] == MAX_DIVERGENCE_ROUNDS
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_provider_failure_is_recorded_and_the_meeting_continues(tmp_path: Path) -> None:
    accounts = [_account("gemini"), _account("claude")]

    def answer(provider: str, prompt: str) -> Any:
        if provider == "claude":
            return {"__raise__": "Chrome profile for claude is locked"}
        return _routing_answer(provider, prompt)

    browser = FakeBrowser(accounts=accounts, answer=answer)
    _s, db, _e, _a, _p, council = _service(tmp_path, browser)
    await db.init()
    try:
        meeting = await council.create_meeting(agenda="Design the council engine", rounds=3)
        await council._run(meeting["id"])
        done = await council.get_meeting(meeting["id"])
        assert done["status"] == "completed"
        claude = next(row for row in done["participants"] if row["provider_id"] == "claude")
        assert claude["status"] == "failed"
        assert "profile" in (claude["last_error"] or "").lower()
        assert len(done["proposals"]) == 2, "only gemini answered"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_to_blueprint_needs_a_decision_and_requests_approval(tmp_path: Path) -> None:
    accounts = [_account("gemini")]
    browser = FakeBrowser(accounts=accounts, answer=_routing_answer)
    _s, db, _e, _a, projects, council = _service(tmp_path, browser)
    await db.init()
    try:
        meeting = await council.create_meeting(agenda="Design the council engine", rounds=3)
        with pytest.raises(CouncilError) as too_early:
            await council.to_blueprint(meeting["id"], project_id="prj_missing")
        assert "no decision" in str(too_early.value)

        project = await projects.create(name="Control Center")
        await council._run(meeting["id"])
        done = await council.get_meeting(meeting["id"])
        assert done["status"] == "completed", done["summary"]

        result = await council.to_blueprint(meeting["id"], project_id=project["id"])
        assert result["blueprint"]["version"] >= 1
        assert result["approval"]["stage"] == "blueprint"
        assert result["approval"]["status"] == "pending"
        assert result["contract"]["selected_proposal"] in {
            row["id"] for row in done["proposals"]
        }
        # the gate is only *requested* - the service never approves itself
        assert await projects.approval_state(project["id"], "blueprint") == "pending"
        assert await projects.approval_state(project["id"], "implementation") == "missing"
    finally:
        await db.close()


# ------------------------------------------------------- account-aware council seats
@pytest.mark.asyncio
async def test_roster_keeps_two_accounts_of_one_provider_apart(tmp_path: Path) -> None:
    """A provider is not one seat: each configured account is tracked separately."""
    registry = FakeAccounts(
        [
            _configured("acc_work", "chatgpt", "Work", role="architect"),
            _configured("acc_home", "chatgpt", "Home", enabled=False),
        ]
    )
    browser = FakeBrowser(
        accounts=[
            _live("chatgpt", "prof_acc_work"),
            _live("chatgpt", "prof_acc_home", "logged_out"),
        ]
    )
    _s, db, _e, _a, _p, council = _service(tmp_path, browser, registry)
    await db.init()
    try:
        entry = next(row for row in (await council.roster())["providers"] if row["provider_id"] == "chatgpt")
        assert entry["status"] == "available"
        assert entry["account_id"] == "acc_work"
        assert entry["account_label"] == "Work"
        assert entry["council_role"] == "architect"
        seats = {row["account_id"]: row for row in entry["seats"]}
        assert seats["acc_work"]["status"] == "available"
        assert seats["acc_work"]["profile_id"] == "prof_acc_work"
        # a disabled account is not a seat; the browser still reports the profile,
        # so it shows up as an unclaimed profile rather than pretending to be usable
        assert seats[""]["label"] == "prof_acc_home (browser profile)"
        assert seats[""]["status"] == "logged_out"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pinned_account_speaks_and_is_attributed_to_itself(tmp_path: Path) -> None:
    registry = FakeAccounts(
        [_configured("acc_a", "chatgpt", "Alpha"), _configured("acc_b", "chatgpt", "Beta", active=True)]
    )
    browser = FakeBrowser(
        accounts=[_live("chatgpt", "prof_acc_a"), _live("chatgpt", "prof_acc_b")],
        answer=_routing_answer,
    )
    _s, db, _e, _a, _p, council = _service(tmp_path, browser, registry)
    await db.init()
    try:
        meeting = await council.create_meeting(
            agenda="Design the storage layer",
            providers=["chatgpt"],
            seats=[{"provider_id": "chatgpt", "account_id": "acc_a"}],
            rounds=2,
        )
        await council._run(meeting["id"])
        done = await council.get_meeting(meeting["id"])
        seat = {row["provider_id"]: row for row in done["participants"]}["chatgpt"]
        # acc_b is the registry's active account; the pinned one must still be the
        # speaker, with its own label rather than the active account's.
        assert seat["included"] is True
        assert seat["account_id"] == "acc_a"
        assert seat["account_label"] == "Alpha"
        assert seat["profile_id"] == "prof_acc_a"
        assert {call["profile"] for call in browser.calls} == {"prof_acc_a"}
        provider_messages = [row for row in done["messages"] if row["provider_id"] != "system"]
        assert provider_messages and {row["account_id"] for row in provider_messages} == {"acc_a"}
        assert {row["profile_id"] for row in provider_messages} == {"prof_acc_a"}
        assert {row["account_label"] for row in done["proposals"]} == {"Alpha"}
        assert "account 'Alpha'" in (done["decision"]["summary"] or "")
        assert "account 'Alpha'" in (done["decision"]["summary"] or "")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pinned_unavailable_account_is_refused_not_replaced(tmp_path: Path) -> None:
    registry = FakeAccounts(
        [_configured("acc_a", "chatgpt", "Alpha"), _configured("acc_b", "chatgpt", "Beta")]
    )
    browser = FakeBrowser(
        accounts=[_live("chatgpt", "prof_acc_a"), _live("chatgpt", "prof_acc_b", "logged_out")],
        answer=_routing_answer,
    )
    _s, db, _e, _a, _p, council = _service(tmp_path, browser, registry)
    await db.init()
    try:
        meeting = await council.create_meeting(
            agenda="Design the storage layer",
            providers=["chatgpt"],
            seats=[{"provider_id": "chatgpt", "account_id": "acc_b"}],
            rounds=2,
        )
        await council._run(meeting["id"])
        done = await council.get_meeting(meeting["id"])
        seat = {row["provider_id"]: row for row in done["participants"]}["chatgpt"]
        # the refused seat keeps its own identity instead of borrowing acc_a's
        assert seat["included"] is False
        assert seat["status"] == "refused"
        assert seat["account_id"] == "acc_b"
        assert seat["account_label"] == "Beta"
        assert seat["profile_id"] == "prof_acc_b"
        assert "logged_out" in (seat["last_error"] or "")
        assert "will not switch" in (seat["last_error"] or "")
        # nothing was asked, so no account answered
        assert browser.calls == []
        assert done["status"] == "failed"
        assert "No council seat could be honoured" in (done["summary"] or "")
        seat_notes = [row for row in done["messages"] if row["kind"] == "seats"]
        assert seat_notes and "Beta" in seat_notes[0]["content"]
        assert "chatgpt" in seat_notes[0]["content"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_seat_choices_are_validated_before_the_meeting_starts(tmp_path: Path) -> None:
    registry = FakeAccounts(
        [
            _configured("acc_a", "chatgpt", "Alpha"),
            _configured("acc_off", "gemini", "Offline", enabled=False),
        ]
    )
    _s, db, _e, _a, _p, council = _service(tmp_path, FakeBrowser(accounts=[]), registry)
    await db.init()
    try:
        with pytest.raises(CouncilError) as unknown:
            await council.create_meeting(agenda="x", seats=[{"provider_id": "chatgpt", "account_id": "nope"}])
        assert unknown.value.code == "bad_seat"
        assert "Unknown account" in str(unknown.value)

        with pytest.raises(CouncilError) as wrong_provider:
            await council.create_meeting(
                agenda="x", seats=[{"provider_id": "chatgpt", "account_id": "acc_off"}]
            )
        assert wrong_provider.value.code == "bad_seat"
        assert "belongs to gemini" in str(wrong_provider.value)

        with pytest.raises(CouncilError) as disabled:
            await council.create_meeting(
                agenda="x", seats=[{"provider_id": "gemini", "account_id": "acc_off"}]
            )
        assert disabled.value.code == "seat_disabled"
        assert "is disabled" in str(disabled.value)

        with pytest.raises(CouncilError) as unpinned:
            await council.create_meeting(agenda="x", seats=[{"provider_id": "chatgpt"}])
        assert unpinned.value.code == "bad_seat"
        assert "needs an account_id" in str(unpinned.value)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_rate_limited_provider_is_marked_unavailable(tmp_path: Path) -> None:
    """A throttled session is reported as it is; nothing is simulated in its place."""
    browser = FakeBrowser(accounts=[_live("claude", "prof_claude", "rate_limited")])
    _s, db, _e, _a, _p, council = _service(tmp_path, browser)
    await db.init()
    try:
        entry = next(row for row in (await council.roster())["providers"] if row["provider_id"] == "claude")
        assert entry["status"] == "unavailable"
        assert "rate_limited" in entry["reason"]
        assert [row["provider_id"] for row in (await council.roster())["available"]] == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_brain_brief_is_used_by_every_seat_and_polish_is_stored(tmp_path: Path) -> None:
    """Qoder writes the brief, OpenCode polishes, and the decision stays authoritative."""
    accounts = [_account("gemini"), _account("claude")]
    browser = FakeBrowser(accounts=accounts, answer=_routing_answer)
    brain = FakeBrain()
    _s, db, _e, _a, _p, council = _service(tmp_path, browser, brain=brain)
    await db.init()
    try:
        meeting = await council.create_meeting(
            agenda="Design the council engine", mode="normal", rounds=3
        )
        await council._run(meeting["id"])
        done = await council.get_meeting(meeting["id"])
        brain_meta = done["context"]["brain"]
        assert brain_meta["brief"] == "SHARED BRIEF: be precise."
        assert brain_meta["brief_by"] == "qoder"
        assert brain_meta["brief_error"] is None
        assert brain_meta["polished"] == "POLISHED ANSWER"
        assert brain_meta["polished_by"] == "opencode"
        assert brain_meta["order"] == ["qoder", "opencode"]
        # The brief really reached the browser prompts, not just the database.
        assert browser.calls
        assert all("COUNCIL BRIEF" in call["prompt"] for call in browser.calls)
        # Polishing never rewrites the deterministic decision.
        assert done["decision"]["summary"]
        assert done["decision"]["scores"]["ranking"]
        assert [purpose for purpose, _ in brain.prompts] == ["meeting brief", "final polish"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_brain_failure_does_not_stop_the_meeting(tmp_path: Path) -> None:
    """Both CLIs down: the meeting still runs and records why there is no polish."""
    from forgemesh_control.brain import BrainResult

    class DownBrain(FakeBrain):
        async def run(self, prompt: str, *, purpose: str, timeout_seconds: int | None = None):
            self.prompts.append((purpose, prompt))
            return BrainResult(
                ok=False,
                error="Neither Qoder nor OpenCode produced an answer.",
                attempts=[],
            )

    accounts = [_account("gemini"), _account("claude")]
    browser = FakeBrowser(accounts=accounts, answer=_routing_answer)
    brain = DownBrain()
    _s, db, _e, _a, _p, council = _service(tmp_path, browser, brain=brain)
    await db.init()
    try:
        meeting = await council.create_meeting(
            agenda="Design the council engine", mode="normal", rounds=3
        )
        await council._run(meeting["id"])
        done = await council.get_meeting(meeting["id"])
        assert done["status"] == "completed"
        assert done["decision"] is not None
        assert done["context"]["brain"]["brief"] is None
        assert "Neither Qoder" in done["context"]["brain"]["brief_error"]
        assert done["context"]["brain"]["polished"] is None
        assert done["decision"]["summary"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconcile_closes_meetings_stranded_by_a_restart(tmp_path: Path) -> None:
    accounts = [_account("gemini"), _account("claude")]
    browser = FakeBrowser(accounts=accounts, answer=_routing_answer)
    _s, db, _e, _a, _p, council = _service(tmp_path, browser)
    await db.init()
    try:
        async with db.session_scope() as session:
            session.add(
                MeetingRow(
                    id="mtg_stranded",
                    mode="normal",
                    agenda="Was running at shutdown",
                    status="running",
                    rounds_total=3,
                )
            )
            session.add(
                MeetingRow(
                    id="mtg_done",
                    mode="normal",
                    agenda="Already finished",
                    status="completed",
                    rounds_total=3,
                )
            )
        assert await council.reconcile() == 1
        stranded = await council.get_meeting("mtg_stranded")
        assert stranded["status"] == "failed"
        assert stranded["ended_at"] is not None
        assert "restarted" in stranded["summary"]
        assert (await council.get_meeting("mtg_done"))["status"] == "completed"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_start_refuses_a_meeting_that_is_not_a_draft(tmp_path: Path) -> None:
    accounts = [_account("gemini"), _account("claude")]
    browser = FakeBrowser(accounts=accounts, answer=_routing_answer)
    _s, db, _e, _a, _p, council = _service(tmp_path, browser)
    await db.init()
    try:
        async with db.session_scope() as session:
            session.add(
                MeetingRow(
                    id="mtg_running",
                    mode="normal",
                    agenda="Already running",
                    status="running",
                    rounds_total=3,
                )
            )
        with pytest.raises(CouncilError) as excinfo:
            await council.start("mtg_running")
        assert excinfo.value.code == "not_draft"
    finally:
        await db.close()
