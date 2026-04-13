from __future__ import annotations

import os


SCENARIO_MODE = os.environ.get("SCENARIO_MODE", "general").lower()

HACKATHON_API_BASE_URL = os.environ.get("JASTER_PLATFORM_HOST", "").rstrip("/")
if HACKATHON_API_BASE_URL and not HACKATHON_API_BASE_URL.endswith("/api"):
    HACKATHON_API_BASE_URL = f"{HACKATHON_API_BASE_URL}/api"
HACKATHON_AGENT_TOKEN = os.environ.get("JASTER_AGENT_TOKEN", "")
HACKATHON_MAX_ACTIVE_CHALLENGES = int(os.environ.get("JASTER_PLATFORM_MAX_ACTIVE", "3"))
HACKATHON_RATE_LIMIT_QPS = int(os.environ.get("JASTER_PLATFORM_QPS", "3"))
HACKATHON_REQUEST_INTERVAL = float(os.environ.get("JASTER_PLATFORM_REQUEST_INTERVAL", "0.35"))
HACKATHON_API_TIMEOUT = float(os.environ.get("JASTER_PLATFORM_TIMEOUT", "15.0"))
