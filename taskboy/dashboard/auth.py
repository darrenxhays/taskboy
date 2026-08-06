"""viewer identity from the alb's signed oidc header.

the alb runs the auth0 sign-in flow at the edge; every forwarded request carries
x-amzn-oidc-data, an ES256 jwt signed by the alb. the instance security group only
admits traffic from the alb, so a request bearing a valid header is a signed-in
user. the app still verifies the signature (defense in depth) and enforces the
email domain because auth0 cannot restrict it at the alb.

read access: any account under dashboard.allowed_email_domain.
write access: emails listed in dashboard.admin_emails (config.yaml).
"""

import base64
import binascii
import json
import time
from dataclasses import dataclass

import jwt
from fastapi import Request

from taskboy import settings
from taskboy.config import DashboardConfig

OIDC_DATA_HEADER = "x-amzn-oidc-data"
CSRF_HEADER = "x-harness-dashboard"  # custom header on mutating requests: cross-site forms can't set it, cross-site fetch fails preflight


class NotAuthenticated(Exception):
    pass


class NotAuthorized(Exception):
    pass


@dataclass
class Viewer:
    email: str
    admin: bool


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


async def _public_key(request: Request, kid: str) -> str:
    """alb signing keys are fetched per key id and cached on the app; they rotate rarely."""
    cache: dict[str, str] = request.app.state.alb_key_cache
    if kid not in cache:
        import aiohttp

        url = f"https://public-keys.auth.elb.{settings.REGION}.amazonaws.com/{kid}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get(url) as response:
                response.raise_for_status()
                cache[kid] = await response.text()
    return cache[kid]


async def viewer_from_request(request: Request) -> Viewer:
    config: DashboardConfig = request.app.state.config.dashboard
    token = request.headers.get(OIDC_DATA_HEADER, "").strip()
    if not token:
        # local dev runs without an alb; a configured dev identity stands in
        if settings.ENVIRONMENT == "local" and config.dev_user_email:
            return _authorize(config, config.dev_user_email)
        raise NotAuthenticated("no identity header")
    try:
        header = json.loads(_b64url_decode(token.split(".")[0]))
        kid = header["kid"]
        key = await _public_key(request, kid)
        claims = jwt.decode(token, key, algorithms=["ES256"])
    except (jwt.PyJWTError, KeyError, ValueError, IndexError, binascii.Error, json.JSONDecodeError) as e:
        raise NotAuthenticated(f"identity header rejected: {type(e).__name__}")
    if int(claims.get("exp", 0)) < int(time.time()) - 60:
        raise NotAuthenticated("identity expired")
    email = str(claims.get("email") or "").lower()
    if not email:
        raise NotAuthenticated("identity has no email claim")
    email_verified = claims.get("email_verified")
    if "email_verified" in claims and (not email_verified or str(email_verified).lower() == "false"):
        raise NotAuthorized("email is not verified")
    return _authorize(config, email)


def _authorize(config: DashboardConfig, email: str) -> Viewer:
    email = email.lower()
    if not email.endswith("@" + config.allowed_email_domain):
        raise NotAuthorized(f"{email} is outside the allowed domain")
    return Viewer(email=email, admin=email in config.admin_emails)


async def require_viewer(request: Request) -> Viewer:
    return await viewer_from_request(request)


async def require_viewer_write(request: Request) -> Viewer:
    # any workspace member may submit (e.g. task feedback), with the same csrf guard as admin writes
    viewer = await viewer_from_request(request)
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        raise NotAuthorized("missing dashboard request header")
    return viewer


async def require_admin(request: Request) -> Viewer:
    viewer = await viewer_from_request(request)
    if not viewer.admin:
        request.app.state.store.add_admin_event(viewer.email, "authorize", request.url.path, "denied", {"reason": "not in dashboard.admin_emails"})
        raise NotAuthorized("management actions require an email listed in dashboard.admin_emails")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        raise NotAuthorized("missing dashboard request header")
    return viewer
