"""Liveness and readiness checks.

`/healthz` is pure liveness -- it makes NO dependency calls. `/readyz` probes
the ingestion service's own `GET /healthz` over HTTP and surfaces the REAL
dependency error on failure (mirroring the Phase-1 / Chainlit readyz gate), so
the frontend can render an honest banner rather than a fabricated degraded mode.
"""

import httpx

from app.config import get_settings

READYZ_TIMEOUT_SECONDS = 5.0


def check_ingestion_ready() -> tuple[bool, str]:
    """Probe `GET {WEBUI_INGESTION_URL}/healthz`.

    Returns `(ready, detail)`. `detail` carries the real dependency error text
    when the probe fails -- never a fabricated message.
    """
    url = f"{get_settings().ingestion_url.rstrip('/')}/healthz"
    try:
        response = httpx.get(url, timeout=READYZ_TIMEOUT_SECONDS)
    except httpx.HTTPError as exc:
        return False, f"ingestion unreachable at {url}: {exc}"

    if response.status_code != 200:
        return False, f"ingestion returned HTTP {response.status_code} from {url}"
    return True, "ingestion reachable"
