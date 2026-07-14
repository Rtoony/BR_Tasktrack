"""TaskTrack — collaborative task tracker (app factory).

create_app() builds a fresh Flask instance, registers blueprints
(some of them gated by deployment-profile feature flags), wires the
SQLite teardown, configures structured/text logging, the request-ID
middleware, error handlers, and the intake-form rate limiter, and
adds the `flask init-db` CLI command.

Module-level re-exports preserve the legacy import surface:
  from app import ALLOWED_TABLES, DB_PATH, validate_record_data
which telegram_bot.py and email_intake.py rely on. Phase 1C will
replace those imports with REST calls and let us drop the re-exports.
"""
import logging
import os

from flask import Flask, g, jsonify, request
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from werkzeug.exceptions import HTTPException

from sqlalchemy import create_engine, inspect

from . import profile as _profile
from .config import ADMIN_WORKFLOW_VIEWS, ALLOWED_TABLES, SIMPLE_SUBMISSION_CONFIGS
from .db import DB_PATH, close_session, get_secret_key
from .logging_config import configure_logging
from .middleware import init_request_middleware
from .models import Base
from .services.tickets import validate_record_data

LOG = logging.getLogger("tasktrack.app")

__all__ = [
    "create_app",
    "limiter",
    # legacy import surface for telegram_bot.py / email_intake.py
    "ALLOWED_TABLES",
    "ADMIN_WORKFLOW_VIEWS",
    "SIMPLE_SUBMISSION_CONFIGS",
    "DB_PATH",
    "validate_record_data",
]

# Limiter is initialized at module scope so route decorators can
# reference it; init_app() is called inside create_app().
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],  # nothing global — we limit specific routes only
    storage_uri="memory://",
    headers_enabled=True,
)


def create_app(db_path=None) -> Flask:
    """Build a configured Flask app instance.

    DB path resolution order:
      1. `db_path` argument (used by tests and internal callers).
      2. `DB_PATH` environment variable (used by the production
         systemd unit + manual `flask` CLI invocations).
      3. The module-level DB_PATH constant (project-root tracker.db,
         the dev default).
    """
    # Configure logging first so subsequent import-time logs go through
    # our formatter. Re-running it on each create_app() is idempotent
    # (dictConfig overwrites the root config).
    configure_logging(log_format=_profile.LOG_FORMAT)

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(
        __name__,
        template_folder=os.path.join(project_root, "templates"),
    )
    app.config["DB_PATH"] = db_path or os.environ.get("DB_PATH") or DB_PATH
    app.secret_key = get_secret_key(app.config["DB_PATH"])
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = _profile.SESSION_COOKIE_SECURE
    app.config["SESSION_COOKIE_HTTPONLY"] = True

    # Profile + feature flags into app.config so route handlers and
    # templates can read them via current_app.config[...].
    for key, value in _profile.summary().items():
        app.config[key] = value

    LOG.info("starting profile=%s ai_intake=%s calendar=%s brand=%r log=%s",
             _profile.PROFILE, _profile.ENABLE_AI_INTAKE,
             _profile.ENABLE_CALENDAR_WIDGET, _profile.BRAND_NAME,
             _profile.LOG_FORMAT)
    for key, default, override in _profile.overrides():
        LOG.warning("profile=%s override: %s default=%r env=%r",
                    _profile.PROFILE, key, default, override)

    _check_schema_matches_models(app.config["DB_PATH"])

    app.teardown_appcontext(close_session)
    init_request_middleware(app)
    limiter.init_app(app)
    _register_error_handlers(app)
    _register_legacy_api_redirect(app)

    # Inject feature flags + branding into every Jinja render so templates
    # can gate UI without route-by-route context plumbing.
    @app.context_processor
    def _inject_profile_context():
        return {
            "ai_intake_enabled": _profile.ENABLE_AI_INTAKE,
            "calendar_enabled": _profile.ENABLE_CALENDAR_WIDGET,
            "brand_name": _profile.BRAND_NAME,
            "tasktrack_profile": _profile.PROFILE,
        }

    # Always-on blueprints.
    from .routes.auth import bp as auth_bp
    from .routes.main import bp as main_bp
    from .routes.intake import bp as intake_bp
    from .routes.api import bp as api_bp
    from .routes.admin import bp as admin_bp
    from .routes.telegram_api import bp as telegram_api_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(intake_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(telegram_api_bp)

    # Feature-flagged blueprints.
    # - AI Intake: experimental; off in the initial company release
    #   (decision 2026-04-27).
    # - Calendar widget: Nexus-specific Radicale integration; replaced
    #   by Outlook in Phase 8 for the company product.
    # - Maximus API: built for Josh's personal AI assistant on Nexus;
    #   off in the company release (the company VM has nothing calling
    #   /api/v1/maximus/*). Phase 7 will remove or spin out entirely.
    if _profile.ENABLE_AI_INTAKE:
        from .routes.triage import bp as triage_bp
        app.register_blueprint(triage_bp)

    if _profile.ENABLE_CALENDAR_WIDGET:
        from .routes.calendar import bp as calendar_bp
        app.register_blueprint(calendar_bp)

    if _profile.ENABLE_MAXIMUS_API:
        from .routes.maximus import bp as maximus_bp
        app.register_blueprint(maximus_bp)

    # Agent API: the Nexus agent principal surface (Ordo + AI-PC stewards)
    # — digest/inbox/task-status/feedback parity with the personal app.
    # Token-gated (hermes/bot scopes); inert until a token is configured.
    if _profile.ENABLE_AGENT_API:
        from .routes.agent_api import bp as agent_api_bp
        app.register_blueprint(agent_api_bp)

    from .cli import create_admin_command, db_upgrade_command, init_db_command
    app.cli.add_command(init_db_command)
    app.cli.add_command(db_upgrade_command)
    app.cli.add_command(create_admin_command)

    return app


def _check_schema_matches_models(db_path: str) -> None:
    """Refuse to start if the live SQLite has drifted from the model definitions.

    Phase 1D-1 ships models alongside the existing raw-sqlite3 routes;
    this guard catches the case where the DB has been mutated outside
    of Alembic since the baseline was stamped. Phase 1D-2's blueprint
    conversion relies on the live DB matching what models claim.

    The check is permissive about EXTRA columns the live DB has (some
    historical artifacts) but strict about MISSING columns and tables
    that the models expect.
    """
    engine = create_engine(f"sqlite:///{db_path}")
    insp = inspect(engine)
    live_tables = set(insp.get_table_names())
    issues = []
    for table in Base.metadata.tables.values():
        if table.name not in live_tables:
            issues.append(f"table missing: {table.name}")
            continue
        live_cols = {c["name"] for c in insp.get_columns(table.name)}
        model_cols = {c.name for c in table.columns}
        missing = model_cols - live_cols
        if missing:
            issues.append(f"{table.name}: missing columns {sorted(missing)}")
    engine.dispose()
    if issues:
        raise RuntimeError(
            "schema drift detected — refusing to start. "
            "Run `alembic upgrade head` or repair the DB. Issues: "
            + "; ".join(issues)
        )
    LOG.info("schema check: %d tables match models", len(Base.metadata.tables))


def _register_legacy_api_redirect(app: Flask) -> None:
    """308-redirect /api/<rest> -> /api/v1/<rest> for one release cycle.

    The 308 status preserves the request method (so POST/PUT/DELETE
    aren't downgraded to GET) and tells caches the move is permanent.
    Active SPA + email_intake.py + scripts/smoke.sh have all been
    updated to /api/v1/* directly; this catch-all only matters for
    external clients that still hit the legacy paths (the Telegram
    bot until 1C-b lands, and any hand-typed curls).
    """
    from flask import redirect

    @app.before_request
    def _legacy_api_to_v1():
        path = request.path
        if path.startswith("/api/") and not path.startswith("/api/v1/"):
            new_path = "/api/v1" + path[len("/api"):]
            if request.query_string:
                new_path += "?" + request.query_string.decode("ascii", errors="ignore")
            return redirect(new_path, code=308)

    @app.before_request
    def _legacy_submit_to_intake():
        # /submit/* -> /intake/*. Capability is intentionally not redirected:
        # /submit/capability returns 404 (intake surface removed).
        path = request.path
        if path == "/submit/capability" or path == "/intake/capability":
            return jsonify({
                "error": "capability submissions are not part of the intake surface",
                "request_id": g.get("request_id", "-"),
            }), 404
        if path == "/submit" or path.startswith("/submit/"):
            new_path = "/intake" + path[len("/submit"):]
            if request.query_string:
                new_path += "?" + request.query_string.decode("ascii", errors="ignore")
            return redirect(new_path, code=308)


def _register_error_handlers(app: Flask) -> None:
    """Return JSON for /api/* errors with the request ID; HTML pass-through elsewhere."""

    @app.errorhandler(HTTPException)
    def _http_exc(e):
        rid = g.get("request_id", "-")
        if request.path.startswith("/api/"):
            return jsonify({
                "error": e.description or "error",
                "code": e.code,
                "request_id": rid,
            }), e.code
        return e

    @app.errorhandler(Exception)
    def _unhandled(e):
        rid = g.get("request_id", "-")
        # Always log non-HTTP exceptions with full traceback server-side.
        # Body is never logged. The client sees only the request ID.
        LOG.exception("unhandled exception path=%s rid=%s", request.path, rid)
        if request.path.startswith("/api/"):
            return jsonify({
                "error": "internal server error",
                "request_id": rid,
            }), 500
        return ("Internal Server Error — see logs for request id " + rid, 500)
