"""Control-server integration tests.

These boot the real FastAPI app (real runtime, real database, real router
wiring) against a throwaway data directory. The browser runtime is pointed at a
closed port on purpose: the council must then report the providers as unknown
rather than inventing answers. Nothing here mocks a provider response - that
would be a unit test, and tests/unit/test_control_council.py does exactly that.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from forgemesh_control.api.server import create_app
from forgemesh_control.config import ControlSettings
from forgemesh_control.council import COUNCIL_PROVIDERS

pytestmark = pytest.mark.integration


def _settings(tmp_path: Path) -> ControlSettings:
    return ControlSettings(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        workspaces=tmp_path / "workspaces",
        reports_dir=tmp_path / "reports",
        frontend_dist=tmp_path / "no-dist",
        auto_install_agents=False,
        # an address that refuses connections: no ForgeMesh Browser is running here
        browser_api_url="http://127.0.0.1:1",
    )


@contextmanager
def control_app(tmp_path: Path) -> Iterator[tuple[TestClient, dict[str, str]]]:
    settings = _settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        token = settings.token_path.read_text(encoding="utf-8").strip()
        yield client, {"X-API-Token": token}


def test_health_is_public_and_api_needs_the_control_token(tmp_path: Path) -> None:
    with control_app(tmp_path) as (client, headers):
        assert client.get("/health").status_code == 200
        assert client.get("/api/council/providers").status_code == 401
        assert client.get("/api/scheduler/snapshot", headers=headers).status_code == 200


def test_council_routes_exist_and_report_the_browser_runtime_as_unreachable(
    tmp_path: Path,
) -> None:
    with control_app(tmp_path) as (client, headers):
        schema = client.get("/openapi.json").json()
        for path in (
            "/api/council/providers",
            "/api/council/meetings",
            "/api/council/meetings/{meeting_id}/decision",
            "/api/problems",
            "/api/scheduler/plan",
            "/api/projects/{project_id}/gate",
            "/api/tasks/{task_id}/worker",
            "/api/workspaces/status",
            "/api/delivery/run",
            "/api/workers",
            "/api/workers/pairing",
            "/api/mesh/register",
        ):
            assert path in schema["paths"], f"{path} missing from the live app"

        roster = client.get("/api/council/providers", headers=headers).json()
        assert roster["connected"] is False
        assert roster["expected"] == list(COUNCIL_PROVIDERS)
        assert roster["available"] == []
        assert {row["status"] for row in roster["providers"]} == {"unknown"}
        assert "ForgeMesh Browser" in roster["error"]


def test_meeting_without_any_signed_in_account_fails_instead_of_simulating(
    tmp_path: Path,
) -> None:
    with control_app(tmp_path) as (client, headers):
        meeting = client.post(
            "/api/council/meetings",
            headers=headers,
            json={"agenda": "Decide the storage layer", "mode": "normal", "rounds": 3},
        )
        assert meeting.status_code == 200, meeting.text
        meeting_id = meeting.json()["id"]

        started = client.post(f"/api/council/meetings/{meeting_id}/start", headers=headers)
        assert started.status_code == 200, started.text

        status = None
        for _ in range(60):
            status = client.get(f"/api/council/meetings/{meeting_id}", headers=headers).json()
            if status["status"] in {"failed", "completed"}:
                break
            time.sleep(0.1)
        assert status is not None and status["status"] == "failed", status
        # the operator gets the actionable reason, not a fabricated deliberation
        assert "fmb serve" in (status["summary"] or ""), status["summary"]
        assert status["proposals"] == []
        assert status["decision"] is None

        decision = client.get(f"/api/council/meetings/{meeting_id}/decision", headers=headers)
        assert decision.status_code == 400 or decision.json().get("decision") is None


def test_planning_and_execution_are_locked_behind_the_approval_gate(
    tmp_path: Path,
) -> None:
    with control_app(tmp_path) as (client, headers):
        project = client.post(
            "/api/projects", headers=headers, json={"name": "Control Center", "mode": "normal"}
        )
        assert project.status_code == 200, project.text
        project_id = project.json()["id"]

        # a meeting with no decision can never become a plan
        meeting = client.post(
            "/api/council/meetings", headers=headers, json={"agenda": "Ship the gate"}
        ).json()
        plan = client.post(
            "/api/scheduler/plan",
            headers=headers,
            json={"project_id": project_id, "meeting_id": meeting["id"]},
        )
        assert plan.status_code == 400
        assert "no recorded decision" in plan.json()["error"]["message"]

        gate = client.post(
            f"/api/projects/{project_id}/gate", headers=headers, json={"summary": "review please"}
        )
        assert gate.status_code == 200
        assert len(gate.json()["requested"]) == 3

        queue = client.post(
            f"/api/projects/{project_id}/tasks/queue", headers=headers, json={}
        )
        assert queue.status_code == 409, queue.text
        assert queue.json()["error"]["code"] == "approval_required"


def test_delivery_and_workspace_routes_refuse_unsafe_input(tmp_path: Path) -> None:
    with control_app(tmp_path) as (client, headers):
        project = client.post(
            "/api/projects", headers=headers, json={"name": "Guard rails"}
        ).json()

        refused = client.post(
            "/api/delivery/run",
            headers=headers,
            json={"project_id": project["id"], "command": "rm -rf /"},
        )
        assert refused.status_code == 400
        assert "allowlist" in refused.json()["error"]["message"]

        outside = client.get(
            "/api/workspaces/status", headers=headers, params={"path": "/etc"}
        )
        assert outside.status_code in {400, 403}
        assert "escapes the allowed root" in str(outside.json())

        bad_remote = client.post(
            "/api/workspaces/clone",
            headers=headers,
            json={"path": str(Path(project["workspace_path"])), "remote": "ext::sh -c id"},
        )
        assert bad_remote.status_code == 400
        assert "https:// or git@" in bad_remote.json()["error"]["message"]


def test_git_probe_and_worker_pairing_are_real(tmp_path: Path) -> None:
    with control_app(tmp_path) as (client, headers):
        probe: dict[str, Any] = client.get("/api/workspaces/git", headers=headers).json()
        assert "available" in probe
        assert isinstance(probe["available"], bool)

        pairing = client.post(
            "/api/workers/pairing", headers=headers, json={"label": "lab-linux"}
        )
        assert pairing.status_code == 200, pairing.text
        body = pairing.json()
        assert body.get("code"), body
        assert body.get("expires_at")

        registered = client.post(
            "/api/mesh/register",
            json={"device_key": "dev-1", "name": "lab", "os_family": "linux", "code": "wrong"},
        )
        assert registered.status_code in {400, 401, 403}, registered.text

        workers = client.get("/api/workers", headers=headers).json()
        assert workers["workers"] == []
        assert workers["protocol_version"]


def test_account_routes_are_live_and_answer_honestly_without_the_runtime(
    tmp_path: Path,
) -> None:
    """HTTP-layer coverage for the AI account manager.

    The browser runtime is unreachable here, so the routes must report that:
    no profile is created, no session is claimed, and the dashboard gets the
    provider list it can actually act on.
    """
    with control_app(tmp_path) as (client, headers):
        schema = client.get("/openapi.json").json()
        for path in (
            "/api/accounts",
            "/api/accounts/providers",
            "/api/accounts/{account_id}",
            "/api/accounts/{account_id}/active",
            "/api/accounts/{account_id}/login/begin",
            "/api/accounts/{account_id}/login/complete",
            "/api/accounts/{account_id}/verify",
            "/api/accounts/{account_id}/logout",
            "/api/accounts/{account_id}/delete",
        ):
            assert path in schema["paths"], f"{path} missing from the live app"

        assert client.get("/api/accounts").status_code == 401
        assert client.get("/api/accounts/providers").status_code == 401

        roster = client.get("/api/accounts", headers=headers).json()
        assert roster["connected"] is False
        assert roster["totals"] == {
            "accounts": 0,
            "authenticated": 0,
            "providers_with_accounts": 0,
        }
        assert roster["providers"], roster
        # with no runtime nothing is browser-managed, so the UI must not
        # offer a login window it cannot honour
        assert all(item["managed_by_browser"] is False for item in roster["providers"])

        refused = client.post(
            "/api/accounts", headers=headers, json={"provider_id": "chatgpt", "label": "work"}
        )
        assert refused.status_code == 400, refused.text
        assert "ForgeMesh Browser" in refused.json()["detail"]

        missing = client.get("/api/accounts/acct_does_not_exist", headers=headers)
        assert missing.status_code == 404, missing.text

        for route in ("/api/accounts/acct_does_not_exist/verify",
                      "/api/accounts/acct_does_not_exist/login/begin",
                      "/api/accounts/acct_does_not_exist/logout"):
            answer = client.post(route, headers=headers, json={})
            assert answer.status_code == 404, f"{route} -> {answer.text}"

        # removal is destructive and stays gated even for an id that exists
        unconfirmed = client.post(
            "/api/accounts/acct_does_not_exist/delete",
            headers=headers,
            json={"confirm": False},
        )
        assert unconfirmed.status_code in {404, 409}, unconfirmed.text

        assert "cookie" not in str(roster).lower()
        assert "token" not in str(roster).lower()


def test_agent_auth_routes_refuse_to_invent_a_session(tmp_path: Path) -> None:
    """The hosted sign-in surface, tested without signing anybody in.

    No login command is spawned here: that would open a real provider page.
    These calls only prove the routes reject unknown agents, unknown accounts
    and unknown sessions instead of reporting a terminal that never existed.
    """
    with control_app(tmp_path) as (client, headers):
        schema = client.get("/openapi.json").json()
        for path in (
            "/api/agents/{kind}/auth",
            "/api/agents/{kind}/auth/login",
            "/api/agents/{kind}/auth/session",
            "/api/agents/{kind}/auth/session/input",
            "/api/agents/{kind}/auth/session/key",
            "/api/agents/{kind}/auth/session/cancel",
            "/api/agents/{kind}/accounts",
            "/api/agents/{kind}/accounts/sync",
        ):
            assert path in schema["paths"], f"{path} missing from the live app"

        assert client.get("/api/agents/opencode/auth").status_code == 401

        for route in (
            "/api/agents/not_an_agent/auth",
            "/api/agents/not_an_agent/auth/session",
        ):
            answer = client.get(route, headers=headers)
            assert answer.status_code == 404, f"{route} -> {answer.text}"

        for route in (
            "/api/agents/not_an_agent/auth/login",
            "/api/agents/not_an_agent/auth/session/cancel",
        ):
            answer = client.post(route, headers=headers, json={})
            assert answer.status_code == 404, f"{route} -> {answer.text}"

        orphan = client.get(
            "/api/agents/opencode/auth/session",
            headers=headers,
            params={"session_id": "ses_never_started"},
        )
        assert orphan.status_code == 404, orphan.text

        status = client.get("/api/agents/opencode/auth", headers=headers)
        assert status.status_code == 200, status.text
        body = status.json()
        assert body["kind"] == "opencode"
        # a fresh control database has no agent accounts; the endpoint says so
        assert body["accounts"] == []
        assert body["signed_in"] == 0
        assert body["running_session"] is None
        assert body["capability"]["installed"] in {True, False}

        bad_account = client.post(
            "/api/agents/opencode/auth/login",
            headers=headers,
            json={"account_id": 987654},
        )
        assert bad_account.status_code in {404, 409, 400}, bad_account.text
        assert bad_account.status_code != 200
