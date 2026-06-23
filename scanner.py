import asyncio
import logging
import random
from typing import Optional
import config
from api import GROUPS_API, INVENTORY_API


async def fetch_inventory(api, ratelimit, user_id: int) -> dict[int, int]:
    inv: dict[int, int] = {}
    cursor = ""
    while True:
        params = {"limit": 100, "sortOrder": "Asc"}
        if cursor:
            params["cursor"] = cursor
        async with ratelimit:
            status, data = await api.get(
                f"{INVENTORY_API}/v1/users/{user_id}/assets/collectibles",
                params=params,
            )
        if status != 200:
            break
        for item in data.get("data", []):
            inv[item["assetId"]] = item["userAssetId"]
        cursor = data.get("nextPageCursor") or ""
        if not cursor:
            break
        await asyncio.sleep(0.3)
    return inv


async def get_trading_groups(api, ratelimit) -> list[int]:
    if config.TRADE_GROUPS:
        return list(config.TRADE_GROUPS)

    async with ratelimit:
        status, data = await api.get(
            f"{GROUPS_API}/v1/groups/search",
            params={"keyword": "limiteds trading", "limit": 10, "prioritizeExactMatch": "false"},
        )
    if status != 200:
        logging.warning(f"[scanner] Group search failed ({status})")
        return []
    groups = [g["id"] for g in data.get("data", [])]
    logging.info(f"[scanner] Auto-discovered groups: {groups}")
    return groups


async def scan_group_members(
    api,
    ratelimit,
    values,
    our_inventory: dict[int, int],
    our_total: int,
    contacted_users: set[int],
    queued_users: set[int],
    bot_user_id: int,
    sends_today: int,
) -> list[tuple[float, int, int, int]]:
    snap = await values.snapshot()
    val_min = our_total * config.MIN_GAIN_RATIO
    val_max = our_total * config.SCAN_MAX_RATIO

    groups = await get_trading_groups(api, ratelimit)
    if not groups:
        logging.info("[scanner] No trading groups available.")
        return []

    group_id = random.choice(groups)
    sort_order = random.choice(["Asc", "Desc"])
    allowed = [10, 25, 50, 100]
    limit = min(allowed, key=lambda v: abs(v - config.ITEMS_PER_SCAN))

    async with ratelimit:
        status, data = await api.get(
            f"{GROUPS_API}/v1/groups/{group_id}/users",
            params={"limit": limit, "sortOrder": sort_order},
        )
    if status != 200:
        logging.warning(f"[scanner] Group {group_id} member list failed ({status}): {data}")
        return []

    members = data.get("data", [])
    logging.info(
        f"[scanner] Group {group_id} — checking {len(members)} members  "
        f"range {val_min:,.0f}–{val_max:,.0f}  budget {sends_today}/{config.MAX_TRADES_PER_DAY}"
    )

    candidates: list[tuple[float, int, int, int]] = []

    for member in members:
        user_info = member.get("user", {})
        user_id = user_info.get("userId") or user_info.get("id")
        if not user_id or user_id == bot_user_id:
            continue
        if user_id in contacted_users or user_id in queued_users:
            continue

        their_inv = await fetch_inventory(api, ratelimit, user_id)

        best: Optional[tuple[float, int, int]] = None
        for asset_id, user_asset_id in their_inv.items():
            if asset_id in our_inventory:
                continue
            if asset_id in config.BLACKLISTED_INCOMING_ITEMS:
                continue
            item_val = snap.get(asset_id, {}).get("value", 0)
            if not (val_min <= item_val <= val_max):
                continue
            ratio = item_val / our_total
            if best is None or ratio > best[0]:
                best = (ratio, asset_id, user_asset_id)

        if best:
            ratio, asset_id, user_asset_id = best
            name = snap.get(asset_id, {}).get("name", str(asset_id))
            val = snap.get(asset_id, {}).get("value", 0)
            logging.info(f"  user {user_id}: {name} (≈{val:,})  ratio {ratio:.2f}x")
            candidates.append((ratio, user_id, asset_id, user_asset_id))
        else:
            logging.debug(f"  user {user_id}: nothing in range")

        await asyncio.sleep(0.3)

    return candidates
