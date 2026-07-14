"""Agent API — the headless-assistant surface (Ordo / Nexus stewards).

Restores the pre-migration Nexus agent contract on the company app so the
Ordo assistant keeps full read/manage parity after the 2026-07-09 move to
BRPLVM. Ported from the personal TaskTrack's digest.py / agent_tasks.py /
inbox.py / agent_feedback.py, adapted to this app's models:

    GET  /api/v1/digest?due_days=7&activity_hours=24    morning-briefing snapshot
    GET  /api/v1/digest/monthly?days=30                 month roll-up
    POST /api/v1/inbox                                  capture (needs_review queue)
    POST /api/v1/task/<table>/<id>/status               close / re-status a task
    GET  /api/v1/feedback                               suggestion_box triage view
    POST /api/v1/feedback/<id>/status                   suggestion_box status update

Auth: `hermes` scope (TASKTRACK_TOKEN_HERMES — the agent principal) or the
`bot` scope, via X-Token / Bearer. This app has no inbox_items table, so
capture lands in the target tracker with needs_review=1 (the same review
queue the intake confirm flow uses); source_ref dedupe rides the
request_reference column where the tracker has one. "Feedback" maps onto
suggestion_box — the company analog of the personal app's feedback_items.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from sqlalchemy import func, select

from ..config import ALLOWED_TABLES
from ..db import get_session
from ..models import ActivityLog, Suggestion, to_dict
from ..services.tickets import (
    TABLE_MODELS,
    create_direct_record,
    done_statuses_for_table,
    is_overdue_value,
    overdue_field_for_table,
)
from ..tokens import check_any_scope

bp = Blueprint("agent_api", __name__)

AGENT_NAME = "Ordo"

# Actionable-work trackers for the digest + status surface. Deliberately
# excludes personnel_issues (sensitive), suggestion_box, personal_tasks.
TASK_TABLES = ("work_tasks", "project_work_tasks", "training_tasks")

_DEFAULT_DUE_DAYS = 7
_DEFAULT_ACTIVITY_HOURS = 24
_RECENT_SCAN_LIMIT = 100
_REVIEW_SAMPLE_LIMIT = 5
_FEEDBACK_MAX_LIMIT = 200


def _require_agent_token():
    return check_any_scope("hermes", "bot")


def _clamp(raw, default: int, lo: int, hi: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(value, hi))


def _due_date(raw):
    """Parse a due value (date or datetime ISO string) to a date, or None."""
    if not raw:
        return None
    value = str(raw).strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _record_title(table: str, row_dict: dict) -> str:
    return (
        row_dict.get("title")
        or row_dict.get("project_name")
        or f"#{row_dict.get('id', '?')}"
    )


def _task_item(table: str, due_field: str, row) -> dict:
    d = to_dict(row) or {}
    return {
        "table": table,
        "id": d.get("id"),
        "title": _record_title(table, d),
        "status": d.get("status", ""),
        "priority": d.get("priority", ""),
        "due": d.get(due_field, "") if due_field else "",
        "project_number": d.get("project_number", "") or "",
        "project_name": d.get("project_name", "") or "",
        "engineer": d.get("engineer", "") or "",
    }


# ── GET /api/v1/digest ───────────────────────────────────────────────────

@bp.route("/api/v1/digest", methods=["GET"])
def digest():
    err = _require_agent_token()
    if err:
        return err

    due_days = _clamp(request.args.get("due_days"), _DEFAULT_DUE_DAYS, 1, 90)
    activity_hours = _clamp(
        request.args.get("activity_hours"), _DEFAULT_ACTIVITY_HOURS, 1, 168
    )
    today = date.today()
    horizon = today + timedelta(days=due_days)

    sess = get_session()
    overdue: list[dict] = []
    due_today: list[dict] = []
    due_soon: list[dict] = []
    by_table: dict[str, dict] = {}
    review_total = 0
    review_sample: list[dict] = []

    for table in TASK_TABLES:
        cfg = ALLOWED_TABLES[table]
        Model = TABLE_MODELS[table]
        due_field = overdue_field_for_table(cfg)
        done = done_statuses_for_table(table)
        counts = {"active": 0, "overdue": 0, "due_soon": 0}

        for row in sess.scalars(select(Model)).all():
            row_dict = None
            if getattr(row, "needs_review", 0):
                review_total += 1
                if len(review_sample) < _REVIEW_SAMPLE_LIMIT:
                    row_dict = to_dict(row) or {}
                    review_sample.append({
                        "table": table,
                        "id": row_dict.get("id"),
                        "title": _record_title(table, row_dict),
                    })
            if getattr(row, "status", None) in done:
                continue
            counts["active"] += 1
            if due_field is None:
                continue
            raw_due = getattr(row, due_field, "") or ""
            if is_overdue_value(raw_due):
                overdue.append(_task_item(table, due_field, row))
                counts["overdue"] += 1
                continue
            parsed = _due_date(raw_due)
            if parsed is None:
                continue
            if parsed == today:
                due_today.append(_task_item(table, due_field, row))
            if today <= parsed <= horizon:
                due_soon.append(_task_item(table, due_field, row))
                counts["due_soon"] += 1

        by_table[table] = counts

    # Soonest first; overdue shows the most-overdue (oldest due) first.
    overdue.sort(key=lambda i: _due_date(i["due"]) or date.max)
    due_soon.sort(key=lambda i: _due_date(i["due"]) or date.max)
    due_today.sort(key=lambda i: (i.get("priority", ""), i.get("title", "")))

    # Recent movement: bounded recent slice, window-filtered in Python.
    cutoff = datetime.utcnow() - timedelta(hours=activity_hours)
    recent_rows = sess.scalars(
        select(ActivityLog)
        .where(ActivityLog.table_name.in_(TASK_TABLES))
        .order_by(ActivityLog.created_at.desc())
        .limit(_RECENT_SCAN_LIMIT)
    ).all()
    recent_activity: list[dict] = []
    for r in recent_rows:
        if r.created_at is None or r.created_at < cutoff:
            continue
        Model = TABLE_MODELS.get(r.table_name)
        title = ""
        if Model is not None:
            target = sess.get(Model, r.record_id)
            if target is not None:
                title = _record_title(r.table_name, to_dict(target) or {})
        recent_activity.append({
            "table": r.table_name,
            "record_id": r.record_id,
            "record_title": title,
            "action": r.action,
            "field": r.field_name or "",
            "new_value": r.new_value or "",
            "user_name": r.user_name or "",
            "at": r.created_at.isoformat(),
        })

    # Shape-compatible with the personal app's digest: the funnel keys map
    # onto this app's needs_review queue (no inbox/parked machinery here).
    return jsonify({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "window": {"due_days": due_days, "activity_hours": activity_hours},
        "counts": {
            "overdue": len(overdue),
            "due_today": len(due_today),
            "due_soon": len(due_soon),
            "active": sum(c["active"] for c in by_table.values()),
            "by_table": by_table,
            "triage_awaiting": review_total,
            "triage_redacted": 0,
            "parked": 0,
        },
        "overdue": overdue,
        "due_today": due_today,
        "due_soon": due_soon,
        "triage_awaiting": review_sample,
        "parked": [],
        "recent_activity": recent_activity,
    })


# ── GET /api/v1/digest/monthly ───────────────────────────────────────────

@bp.route("/api/v1/digest/monthly", methods=["GET"])
def monthly():
    """Month-level roll-up: throughput from the activity log plus current
    open state and open-work-by-project. ?days (7-92, default 30)."""
    err = _require_agent_token()
    if err:
        return err
    days = _clamp(request.args.get("days"), 30, 7, 92)
    today = date.today()
    horizon = today + timedelta(days=days)
    cutoff = datetime.utcnow() - timedelta(days=days)
    sess = get_session()

    overdue = due_soon = active = 0
    by_project: dict[str, dict] = {}
    for table in TASK_TABLES:
        cfg = ALLOWED_TABLES[table]
        Model = TABLE_MODELS[table]
        due_field = overdue_field_for_table(cfg)
        done = done_statuses_for_table(table)
        for row in sess.scalars(select(Model)).all():
            if getattr(row, "status", None) in done:
                continue
            active += 1
            if due_field is None:
                continue
            raw = getattr(row, due_field, "") or ""
            proj = getattr(row, "project_number", "") or "(no #)"
            bucket = by_project.setdefault(proj, {"open": 0, "overdue": 0})
            if is_overdue_value(raw):
                overdue += 1
                bucket["open"] += 1
                bucket["overdue"] += 1
            else:
                d = _due_date(raw)
                if d is not None and today <= d <= horizon:
                    due_soon += 1
                    bucket["open"] += 1

    def _count(*conds):
        stmt = select(func.count()).select_from(ActivityLog).where(
            ActivityLog.table_name.in_(TASK_TABLES),
            ActivityLog.created_at >= cutoff, *conds)
        return sess.scalar(stmt) or 0

    created = _count(ActivityLog.action == "created")
    completed = _count(ActivityLog.action == "status_change",
                       ActivityLog.new_value == "Complete")
    comp_rows = sess.scalars(
        select(ActivityLog).where(
            ActivityLog.table_name.in_(TASK_TABLES),
            ActivityLog.created_at >= cutoff,
            ActivityLog.action == "status_change",
            ActivityLog.new_value == "Complete",
        ).order_by(ActivityLog.created_at.desc()).limit(12)
    ).all()
    completed_titles = []
    for a in comp_rows:
        Model = TABLE_MODELS.get(a.table_name)
        target = sess.get(Model, a.record_id) if Model else None
        completed_titles.append(
            _record_title(a.table_name, to_dict(target) or {}) if target
            else f"#{a.record_id}")

    ranked = sorted(by_project.items(), key=lambda kv: (-kv[1]["overdue"], -kv[1]["open"]))
    return jsonify({
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        "period_days": days,
        "throughput": {"created": created, "completed": completed,
                       "net_open_change": created - completed},
        "completed_titles": completed_titles,
        "open": {"overdue": overdue, "due_next": due_soon, "active": active},
        "by_project": [{"project": k, "open": v["open"], "overdue": v["overdue"]}
                       for k, v in ranked],
    })


# ── POST /api/v1/inbox — capture into the review queue ──────────────────

@bp.route("/api/v1/inbox", methods=["POST"])
def capture():
    """Agent capture. Same body contract as the personal app's inbox:

        {"title": "...", "body": "...", "source": "ordo",
         "source_ref": "optional dedupe key", "priority": "Low|Medium|High",
         "due_date": "YYYY-MM-DD", "target_table": "work_tasks"}

    No inbox_items table here — the item lands in the target tracker with
    needs_review=1 (the intake review queue). source_ref dedupes via the
    request_reference column when the tracker has one; retries are no-ops.
    """
    err = _require_agent_token()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()[:256]
    if not title:
        return jsonify({"error": "title is required"}), 400
    target = (data.get("target_table") or "work_tasks").strip() or "work_tasks"
    if target not in TASK_TABLES:
        return jsonify({"error": f"target_table must be one of {list(TASK_TABLES)}"}), 400

    source = str(data.get("source") or "agent").strip()[:64]
    source_ref = str(data.get("source_ref") or "").strip()[:128]
    priority = (data.get("priority") or "Medium").strip()
    if priority not in ("High", "Medium", "Low"):
        priority = "Medium"

    sess = get_session()
    Model = TABLE_MODELS[target]
    valid_cols = {c.name for c in Model.__table__.columns}

    ref_key = f"{source}:{source_ref}" if source_ref else ""
    if ref_key and "request_reference" in valid_cols:
        existing = sess.scalars(
            select(Model).where(Model.request_reference == ref_key).limit(1)
        ).first()
        if existing is not None:
            return jsonify({
                "ok": True, "deduped": True, "table": target,
                "id": existing.id, "task": to_dict(existing),
            }), 200

    payload = {
        "title": title,
        "description": (data.get("body") or "").strip(),
        "priority": priority,
        "due_date": (data.get("due_date") or "").strip(),
        "source": source,
        "needs_review": 1,
        "created_by_name": f"{AGENT_NAME} capture ({source})",
    }
    if ref_key and "request_reference" in valid_cols:
        payload["request_reference"] = ref_key

    new_id, create_err = create_direct_record(
        sess, target, payload, f"{AGENT_NAME} capture",
        action="created", action_detail=f"agent capture ({source})",
    )
    if create_err:
        sess.rollback()
        return jsonify({"error": create_err}), 400
    sess.commit()
    row = sess.get(Model, new_id)
    return jsonify({
        "ok": True, "deduped": False, "table": target,
        "id": new_id, "task": to_dict(row),
    }), 201


# ── POST /api/v1/task/<table>/<id>/status — close / re-status ───────────

def _done_status(table: str, flow: list) -> str:
    done = done_statuses_for_table(table)
    return next((s for s in flow if s in done), "Complete")


@bp.route("/api/v1/task/<table>/<int:record_id>/status", methods=["POST"])
def set_status(table, record_id):
    err = _require_agent_token()
    if err:
        return err
    if table not in TASK_TABLES:
        return jsonify({"error": f"status updates limited to {list(TASK_TABLES)}"}), 400

    flow = ALLOWED_TABLES[table].get("status_flow", [])
    requested = (request.get_json(silent=True) or {}).get("status") or ""
    requested = requested.strip() or _done_status(table, flow)
    if flow and requested not in flow:
        return jsonify({"error": f"status must be one of {flow}"}), 400

    sess = get_session()
    Model = TABLE_MODELS[table]
    row = sess.get(Model, record_id)
    if row is None:
        return jsonify({"error": "task not found"}), 404

    old = row.status
    if old == requested:
        return jsonify({"ok": True, "unchanged": True, "task": to_dict(row)})

    row.status = requested
    row.updated_at = datetime.utcnow()
    sess.add(ActivityLog(
        table_name=table, record_id=record_id, action="status_change",
        field_name="status", old_value=str(old), new_value=str(requested),
        user_name=AGENT_NAME,
    ))
    sess.commit()
    sess.refresh(row)
    return jsonify({"ok": True, "from": old, "to": requested, "task": to_dict(row)})


# ── Feedback compat — mapped onto suggestion_box ─────────────────────────

def _suggestion_brief(row: Suggestion) -> dict:
    """Old feedback_items brief shape, sourced from suggestion_box."""
    return {
        "id": row.id,
        "title": row.title,
        "feedback_type": row.suggestion_type,
        "priority": row.priority,
        "status": row.status,
        "dev_status": "",
        "page_url": "",
        "tab": "",
        "component_label": "",
        "body": row.summary,
        "tags": "",
        "resolution_notes": row.review_notes,
        "created_at": str(row.created_at) if row.created_at else None,
    }


@bp.route("/api/v1/feedback", methods=["GET"])
def list_feedback():
    """Triage view of the suggestion box (this app's feedback analog).

    Query params: ``status`` = ``open`` (default; excludes terminal
    statuses) / ``all`` / a specific status. ``type`` = suggestion_type
    filter. ``limit``.
    """
    err = _require_agent_token()
    if err:
        return err

    sess = get_session()
    done = set(done_statuses_for_table("suggestion_box"))
    status = (request.args.get("status") or "open").strip()
    ftype = (request.args.get("type") or "").strip()
    limit = _clamp(request.args.get("limit"), 50, 1, _FEEDBACK_MAX_LIMIT)

    stmt = select(Suggestion)
    if status == "open" and done:
        stmt = stmt.where(Suggestion.status.notin_(done))
    elif status not in ("open", "all", ""):
        stmt = stmt.where(Suggestion.status == status)
    if ftype:
        stmt = stmt.where(Suggestion.suggestion_type == ftype)
    stmt = stmt.order_by(Suggestion.created_at.desc()).limit(limit)
    items = [_suggestion_brief(r) for r in sess.scalars(stmt).all()]

    by_type: dict = {}
    by_status: dict = {}
    open_count = 0
    for s, t in sess.execute(
        select(Suggestion.status, Suggestion.suggestion_type)
    ).all():
        by_status[s] = by_status.get(s, 0) + 1
        by_type[t] = by_type.get(t, 0) + 1
        if s not in done:
            open_count += 1

    return jsonify({
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "filter": {"status": status, "type": ftype or None, "limit": limit},
        "counts": {"open": open_count, "total": sum(by_status.values()),
                   "by_type": by_type, "by_status": by_status},
        "items": items,
        "source_table": "suggestion_box",
    })


@bp.route("/api/v1/feedback/<int:record_id>/status", methods=["POST"])
def set_feedback_status(record_id):
    err = _require_agent_token()
    if err:
        return err

    flow = ALLOWED_TABLES["suggestion_box"].get("status_flow", [])
    data = request.get_json(silent=True) or {}
    requested = (data.get("status") or "").strip()
    if not requested or requested not in flow:
        return jsonify({"error": f"status must be one of {flow}"}), 400

    sess = get_session()
    row = sess.get(Suggestion, record_id)
    if row is None:
        return jsonify({"error": "feedback item not found"}), 404

    old = row.status
    changed = False
    if old != requested:
        row.status = requested
        changed = True
        sess.add(ActivityLog(
            table_name="suggestion_box", record_id=record_id, action="status_change",
            field_name="status", old_value=str(old), new_value=str(requested),
            user_name=AGENT_NAME,
        ))

    notes = data.get("resolution_notes")
    if notes is not None and str(notes) != (row.review_notes or ""):
        row.review_notes = str(notes)
        changed = True

    if changed:
        row.updated_at = datetime.utcnow()
        sess.commit()
        sess.refresh(row)
    return jsonify({"ok": True, "changed": changed, "from": old,
                    "to": row.status, "item": _suggestion_brief(row)})
