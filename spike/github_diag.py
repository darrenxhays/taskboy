"""diagnose the github app installation: print what it actually grants and dry-run each profile's token mint.

run in the terminal with GITHUB_APP_ID / GITHUB_INSTALLATION_ID / GITHUB_APP_PRIVATE_KEY exported:
    .venv/bin/python spike/github_diag.py
"""

import asyncio
import json
import os
import time

import aiohttp
import jwt

from taskboy.broker import PROFILE_PERMISSIONS

APP_ID = os.environ["GITHUB_APP_ID"]
INSTALLATION_ID = os.environ["GITHUB_INSTALLATION_ID"]
KEY = os.environ["GITHUB_APP_PRIVATE_KEY"]


def app_jwt() -> str:
    now = int(time.time())
    return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(APP_ID)}, KEY, algorithm="RS256")


async def main() -> None:
    headers = {"Authorization": f"Bearer {app_jwt()}", "Accept": "application/vnd.github+json"}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(f"https://api.github.com/app/installations/{INSTALLATION_ID}") as response:
            installation = await response.json()
            if response.status >= 300:
                print(f"could not read installation ({response.status}): {installation.get('message')}")
                return
            print("installed on account :", (installation.get("account") or {}).get("login"))
            print("repository selection :", installation.get("repository_selection"))
            print("granted permissions  :", json.dumps(installation.get("permissions") or {}, sort_keys=True))
        print()
        for profile, permissions in PROFILE_PERMISSIONS.items():
            async with session.post(f"https://api.github.com/app/installations/{INSTALLATION_ID}/access_tokens", json={"permissions": permissions}) as response:
                if response.status < 300:
                    print(f"mint [{profile:9}] requesting {permissions} -> OK")
                else:
                    body = await response.json()
                    print(f"mint [{profile:9}] requesting {permissions} -> FAIL {response.status}: {body.get('message')}")


if __name__ == "__main__":
    asyncio.run(main())
