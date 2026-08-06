"""shared Slack user-profile cache and refresh logic."""

from datetime import datetime, timezone

from taskboy.store import Store

USER_CACHE_SECONDS = 24 * 60 * 60


async def cached_user_profile(store: Store, client, user_id: str) -> dict | None:
    cached = store.get_slack_user(user_id)
    if cached:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached["updated_at"])
            if age.total_seconds() < USER_CACHE_SECONDS:
                return cached
        except (KeyError, TypeError, ValueError):
            pass
    response = await client.users_info(user=user_id)
    user = response.get("user") or {}
    profile = user.get("profile") or {}
    return store.upsert_slack_user(
        user_id,
        team_id=user.get("team_id"),
        username=user.get("name"),
        real_name=user.get("real_name") or profile.get("real_name"),
        display_name=profile.get("display_name"),
        email=profile.get("email"),
        title=profile.get("title"),
        tz=user.get("tz"),
        is_bot=int(bool(user.get("is_bot"))),
    )
