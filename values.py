import asyncio
import json
import logging
import aiohttp

ROLIMONS_ITEMS = "https://www.rolimons.com/itemapi/itemdetails"


class ItemValueCache:
    def __init__(self):
        self._data: dict[int, dict] = {}
        self._lock = asyncio.Lock()

    async def refresh(self, session: aiohttp.ClientSession):
        try:
            async with session.get(ROLIMONS_ITEMS, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                status = resp.status
                text = await resp.text()

            if status != 200:
                logging.warning(f"[values] Rolimons returned HTTP {status}: {text[:300]}")
                return
            if not text.strip():
                logging.warning("[values] Rolimons returned an empty body (HTTP 200) — likely a Cloudflare block")
                return

            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                logging.warning(f"[values] Rolimons response is not JSON. First 300 chars: {text[:300]}")
                return

            parsed: dict[int, dict] = {}
            for str_id, d in raw.get("items", {}).items():
                if len(d) < 4:
                    continue
                aid = int(str_id)
                rap = d[2] if d[2] != -1 else 0
                val = d[3] if d[3] != -1 else rap
                parsed[aid] = {"name": d[0], "rap": rap, "value": val}
            async with self._lock:
                self._data = parsed
            logging.info(f"[values] {len(parsed):,} items loaded from Rolimons.")
        except Exception as exc:
            logging.warning(f"[values] Refresh failed: {exc}")

    async def get_value(self, asset_id: int) -> int:
        async with self._lock:
            return self._data.get(asset_id, {}).get("value", 0)

    async def get_name(self, asset_id: int) -> str:
        async with self._lock:
            return self._data.get(asset_id, {}).get("name", f"[{asset_id}]")

    async def snapshot(self) -> dict[int, dict]:
        async with self._lock:
            return dict(self._data)
