"""In-process tests for the agent API (app/routes/agent_api.py).

Covers the restored Nexus agent contract: scoped auth (hermes/bot, with
scope isolation from triage/personal), the digest read, inbox capture with
source_ref dedupe into the needs_review queue, task status close, and the
suggestion_box-backed feedback compat surface.
"""
import pytest

import app.tokens as tokens
from app.db import get_session
from app.models import ActivityLog, Suggestion, WorkTask

HERMES = "test-hermes-token-abc123"
BOT = "test-bot-token-def456"
TRIAGE = "test-triage-token-xyz789"


@pytest.fixture
def agent_client(temp_app, monkeypatch):
    """Test client with hermes/bot/triage scopes configured, legacy off."""
    monkeypatch.setitem(tokens.SCOPED_TOKENS, "hermes", HERMES)
    monkeypatch.setitem(tokens.SCOPED_TOKENS, "bot", BOT)
    monkeypatch.setitem(tokens.SCOPED_TOKENS, "triage", TRIAGE)
    monkeypatch.setattr(tokens, "LEGACY_TOKEN", "")
    return temp_app.test_client()


def _h(token):
    return {"X-Token": token}


# ── auth ─────────────────────────────────────────────────────────────────

def test_digest_requires_token(agent_client):
    assert agent_client.get("/api/v1/digest").status_code == 401


def test_digest_rejects_wrong_token(agent_client):
    r = agent_client.get("/api/v1/digest", headers=_h("nope"))
    assert r.status_code == 401


def test_digest_accepts_hermes_and_bot_scopes(agent_client):
    assert agent_client.get("/api/v1/digest", headers=_h(HERMES)).status_code == 200
    assert agent_client.get("/api/v1/digest", headers=_h(BOT)).status_code == 200


def test_agent_api_rejects_other_scopes(agent_client):
    """Scope isolation: a triage token must not grant the agent surface."""
    assert agent_client.get("/api/v1/digest", headers=_h(TRIAGE)).status_code == 401
    r = agent_client.post("/api/v1/inbox", headers=_h(TRIAGE), json={"title": "x"})
    assert r.status_code == 401


def test_bearer_auth_works(agent_client):
    r = agent_client.get(
        "/api/v1/digest", headers={"Authorization": f"Bearer {HERMES}"})
    assert r.status_code == 200


# ── digest ───────────────────────────────────────────────────────────────

def test_digest_shape_and_overdue(agent_client, temp_app):
    with temp_app.app_context():
        sess = get_session()
        sess.add(WorkTask(title="Overdue thing", due_date="2020-01-01",
                          status="In Progress"))
        sess.add(WorkTask(title="Done thing", due_date="2020-01-01",
                          status="Complete"))
        sess.commit()

    r = agent_client.get("/api/v1/digest", headers=_h(HERMES))
    assert r.status_code == 200
    d = r.get_json()
    for key in ("generated_at", "window", "counts", "overdue", "due_today",
                "due_soon", "recent_activity", "triage_awaiting", "parked"):
        assert key in d
    assert d["counts"]["overdue"] == 1
    assert d["counts"]["active"] == 1  # Complete row excluded
    assert d["overdue"][0]["title"] == "Overdue thing"
    assert d["overdue"][0]["table"] == "work_tasks"


def test_digest_monthly_shape(agent_client):
    r = agent_client.get("/api/v1/digest/monthly?days=30", headers=_h(BOT))
    assert r.status_code == 200
    d = r.get_json()
    for key in ("throughput", "open", "by_project", "completed_titles"):
        assert key in d


# ── inbox capture ────────────────────────────────────────────────────────

def test_inbox_capture_lands_in_review_queue(agent_client, temp_app):
    r = agent_client.post("/api/v1/inbox", headers=_h(HERMES), json={
        "title": "Captured from Slack",
        "body": "details here",
        "source": "ordo",
        "source_ref": "slack-msg-1",
        "priority": "High",
    })
    assert r.status_code == 201
    d = r.get_json()
    assert d["ok"] is True and d["deduped"] is False
    assert d["table"] == "work_tasks"
    task = d["task"]
    assert task["needs_review"] == 1
    assert task["priority"] == "High"
    assert task["description"] == "details here"

    with temp_app.app_context():
        sess = get_session()
        log = sess.query(ActivityLog).filter_by(
            table_name="work_tasks", record_id=d["id"]).all()
        assert any(a.action == "created" for a in log)


def test_inbox_capture_dedupes_on_source_ref(agent_client, temp_app):
    body = {"title": "Same item", "source": "ordo", "source_ref": "ref-42"}
    r1 = agent_client.post("/api/v1/inbox", headers=_h(HERMES), json=body)
    assert r1.status_code == 201
    r2 = agent_client.post("/api/v1/inbox", headers=_h(HERMES), json=body)
    assert r2.status_code == 200
    assert r2.get_json()["deduped"] is True
    assert r2.get_json()["id"] == r1.get_json()["id"]

    with temp_app.app_context():
        sess = get_session()
        rows = sess.query(WorkTask).filter_by(title="Same item").all()
        assert len(rows) == 1


def test_inbox_requires_title(agent_client):
    r = agent_client.post("/api/v1/inbox", headers=_h(HERMES), json={"body": "x"})
    assert r.status_code == 400


def test_inbox_rejects_bad_target(agent_client):
    r = agent_client.post("/api/v1/inbox", headers=_h(HERMES), json={
        "title": "x", "target_table": "personnel_issues"})
    assert r.status_code == 400


# ── task status ──────────────────────────────────────────────────────────

def test_status_close_defaults_to_done(agent_client, temp_app):
    with temp_app.app_context():
        sess = get_session()
        t = WorkTask(title="Close me", status="In Progress")
        sess.add(t)
        sess.commit()
        tid = t.id

    r = agent_client.post(f"/api/v1/task/work_tasks/{tid}/status",
                          headers=_h(HERMES), json={})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is True and d["to"] == "Complete"

    with temp_app.app_context():
        sess = get_session()
        log = sess.query(ActivityLog).filter_by(
            table_name="work_tasks", record_id=tid,
            action="status_change").one()
        assert log.user_name == "Ordo"
        assert log.new_value == "Complete"


def test_status_rejects_invalid_status(agent_client, temp_app):
    with temp_app.app_context():
        sess = get_session()
        t = WorkTask(title="x", status="In Progress")
        sess.add(t)
        sess.commit()
        tid = t.id
    r = agent_client.post(f"/api/v1/task/work_tasks/{tid}/status",
                          headers=_h(HERMES), json={"status": "Bogus"})
    assert r.status_code == 400


def test_status_rejects_non_task_tables(agent_client):
    r = agent_client.post("/api/v1/task/suggestion_box/1/status",
                          headers=_h(HERMES), json={})
    assert r.status_code == 400


def test_status_404_on_missing_task(agent_client):
    r = agent_client.post("/api/v1/task/work_tasks/99999/status",
                          headers=_h(HERMES), json={})
    assert r.status_code == 404


# ── feedback compat (suggestion_box) ─────────────────────────────────────

def test_feedback_list_and_status_update(agent_client, temp_app):
    with temp_app.app_context():
        sess = get_session()
        s = Suggestion(title="Add dark mode", suggestion_type="Improvement",
                          summary="please", status="New")
        sess.add(s)
        sess.commit()
        sid = s.id

    r = agent_client.get("/api/v1/feedback", headers=_h(HERMES))
    assert r.status_code == 200
    d = r.get_json()
    assert d["source_table"] == "suggestion_box"
    assert d["counts"]["total"] == 1
    item = d["items"][0]
    assert item["id"] == sid
    assert item["title"] == "Add dark mode"
    assert item["feedback_type"] == "Improvement"
    assert item["body"] == "please"

    r = agent_client.post(f"/api/v1/feedback/{sid}/status",
                          headers=_h(HERMES),
                          json={"status": "Under Review",
                                "resolution_notes": "looking at it"})
    assert r.status_code == 200
    d = r.get_json()
    assert d["changed"] is True and d["to"] == "Under Review"
    assert d["item"]["resolution_notes"] == "looking at it"


def test_feedback_status_rejects_invalid(agent_client, temp_app):
    with temp_app.app_context():
        sess = get_session()
        s = Suggestion(title="x", status="New")
        sess.add(s)
        sess.commit()
        sid = s.id
    r = agent_client.post(f"/api/v1/feedback/{sid}/status",
                          headers=_h(HERMES), json={"status": "Fixed"})
    assert r.status_code == 400
