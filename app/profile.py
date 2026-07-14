"""Deployment profile + feature flags.

Two profiles:
- `personal` (default) — Josh's local Nexus build. Calendar on, AI Intake on,
  intake forms anonymous-OK, binds 0.0.0.0, "TaskTrack" branding.
- `company` — the BR Task Tracker rollout. Calendar off, AI Intake off
  (experimental, not in initial release), intake forms login-required,
  binds 127.0.0.1, "BR Task Tracker" branding.

Each individual flag can be overridden by setting its env var directly;
overrides relative to the active profile log a startup warning so they
don't drift silently.

All values resolve at module import time. To change the profile or any
flag, restart the service with new env. The systemd unit injects them
via vault → `/dev/shm/nexus-env-collab-tracker` → process env.
"""
import logging
import os

LOG = logging.getLogger("tasktrack.profile")

PROFILE_RAW = os.environ.get("TASKTRACK_PROFILE", "personal")
PROFILE = PROFILE_RAW.strip().lower()
if PROFILE not in ("personal", "company"):
    LOG.warning("unknown TASKTRACK_PROFILE=%r — falling back to 'personal'", PROFILE_RAW)
    PROFILE = "personal"

PROFILE_DEFAULTS = {
    "personal": {
        "ENABLE_AI_INTAKE": "true",
        "ENABLE_CALENDAR_WIDGET": "true",
        "ENABLE_MAXIMUS_API": "true",
        "ENABLE_AGENT_API": "true",
        "INTAKE_FORM_AUTH": "none",
        "INTAKE_FORM_RATE_LIMIT_PER_HR_PER_IP": "60",
        "BIND_HOST": "0.0.0.0",
        "BRAND_NAME": "TaskTrack",
        "LOG_FORMAT": "text",
        "ENABLE_DEBUG_ROUTES": "true",
        "ALLOW_HARD_DELETE": "true",
        "SESSION_COOKIE_SECURE": "true",
    },
    "company": {
        "ENABLE_AI_INTAKE": "false",
        "ENABLE_CALENDAR_WIDGET": "false",
        "ENABLE_MAXIMUS_API": "false",
        "ENABLE_AGENT_API": "true",
        "INTAKE_FORM_AUTH": "required",
        "INTAKE_FORM_RATE_LIMIT_PER_HR_PER_IP": "10",
        "BIND_HOST": "127.0.0.1",
        "BRAND_NAME": "BR Task Tracker",
        "LOG_FORMAT": "structured",
        "ENABLE_DEBUG_ROUTES": "false",
        "ALLOW_HARD_DELETE": "false",
        # SECURE cookies require HTTPS. Initial company-VM rollout uses
        # plain HTTP on the LAN behind Windows-network access control;
        # tasktrack.env explicitly sets this to "false" for that case.
        # Default stays "true" so a HTTPS-fronted deploy is safe.
        "SESSION_COOKIE_SECURE": "true",
    },
}


def _bool(s) -> bool:
    return str(s).strip().lower() in ("1", "true", "yes", "on")


def _resolve(key: str) -> str:
    """Env value if set, else profile default, else empty string."""
    env_value = os.environ.get(key)
    if env_value is not None:
        return env_value
    return PROFILE_DEFAULTS[PROFILE].get(key, "")


def get_str(key: str) -> str:
    return _resolve(key)


def get_bool(key: str) -> bool:
    return _bool(_resolve(key))


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(_resolve(key))
    except (TypeError, ValueError):
        return default


# Resolved values exported at module scope for easy import.
ENABLE_AI_INTAKE = get_bool("ENABLE_AI_INTAKE")
ENABLE_CALENDAR_WIDGET = get_bool("ENABLE_CALENDAR_WIDGET")
ENABLE_MAXIMUS_API = get_bool("ENABLE_MAXIMUS_API")
ENABLE_AGENT_API = get_bool("ENABLE_AGENT_API")
INTAKE_FORM_AUTH = get_str("INTAKE_FORM_AUTH")  # "none" | "required"
INTAKE_FORM_RATE_LIMIT_PER_HR_PER_IP = get_int("INTAKE_FORM_RATE_LIMIT_PER_HR_PER_IP", 60)
BIND_HOST = get_str("BIND_HOST")
BRAND_NAME = get_str("BRAND_NAME")
LOG_FORMAT = get_str("LOG_FORMAT")  # "text" | "structured"
ENABLE_DEBUG_ROUTES = get_bool("ENABLE_DEBUG_ROUTES")
ALLOW_HARD_DELETE = get_bool("ALLOW_HARD_DELETE")
SESSION_COOKIE_SECURE = get_bool("SESSION_COOKIE_SECURE")


def overrides() -> list[tuple[str, str, str]]:
    """List of (key, profile_default, env_override) where env explicitly differs from profile default."""
    out = []
    for key, default in PROFILE_DEFAULTS[PROFILE].items():
        env_value = os.environ.get(key)
        if env_value is not None and env_value != default:
            out.append((key, default, env_value))
    return out


def summary() -> dict:
    """Resolved profile state for /healthz, startup logs, debug pages."""
    return {
        "profile": PROFILE,
        "ENABLE_AI_INTAKE": ENABLE_AI_INTAKE,
        "ENABLE_CALENDAR_WIDGET": ENABLE_CALENDAR_WIDGET,
        "ENABLE_MAXIMUS_API": ENABLE_MAXIMUS_API,
        "INTAKE_FORM_AUTH": INTAKE_FORM_AUTH,
        "INTAKE_FORM_RATE_LIMIT_PER_HR_PER_IP": INTAKE_FORM_RATE_LIMIT_PER_HR_PER_IP,
        "BIND_HOST": BIND_HOST,
        "BRAND_NAME": BRAND_NAME,
        "LOG_FORMAT": LOG_FORMAT,
        "ENABLE_DEBUG_ROUTES": ENABLE_DEBUG_ROUTES,
        "ALLOW_HARD_DELETE": ALLOW_HARD_DELETE,
        "SESSION_COOKIE_SECURE": SESSION_COOKIE_SECURE,
    }
