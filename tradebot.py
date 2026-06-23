#!/usr/bin/env python3
import asyncio
import datetime
import logging
import sys
from typing import Optional
import aiohttp
import config
from api import RobloxAPI, TwoCaptchaSolver, TRADES_API, USERS_API
from values import ItemValueCache
from scanner import fetch_inventory, scan_group_members


def setup_logging():
    handlers = [logging.StreamHandler(sys.stdout)]
    if config.LOG_FILE:
        handlers.append(logging.FileHandler(config.LOG_FILE, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


class TradeBot:
    def __init__(self):
        solver = (
            TwoCaptchaSolver(config.CAPTCHA_API_KEY)
            if getattr(config, "CAPTCHA_API_KEY", "")
            else None
        )
        self.api = RobloxAPI(
            config.ROBLOSECURITY,
            solver=solver,
            totp_secret=getattr(config, "TOTP_SECRET", ""),
        )
        self.values = ItemValueCache()

        self.user_id: Optional[int] = None
        self.username: Optional[str] = None

        self._inventory: dict[int, int] = {}
        self._inv_lock = asyncio.Lock()

        self._ratelimit = asyncio.Semaphore(4)

        self._outbound_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = 0

        self._processed_inbound: set[int] = set()
        self._contacted_users: set[int] = set()
        self._queued_users: set[int] = set()

        self._sends_today = 0
        self._budget_day: Optional[datetime.date] = None

    async def authenticate(self) -> bool:
        status, data = await self.api.get(f"{USERS_API}/v1/users/authenticated")
        if status == 200:
            self.user_id = data["id"]
            self.username = data["name"]
            logging.info(f"Logged in as  {self.username}  (ID {self.user_id})")
            return True
        logging.error(f"Auth failed ({status}). Check .ROBLOSECURITY in config.py.")
        return False

    async def _refresh_inventory(self):
        inv = await fetch_inventory(self.api, self._ratelimit, self.user_id)
        async with self._inv_lock:
            self._inventory = inv
        logging.info(f"[inventory] {len(inv)} collectibles loaded.")

    async def _pick_our_items(self, target_value: int) -> list[dict]:
        snap = await self.values.snapshot()
        async with self._inv_lock:
            inv = dict(self._inventory)

        candidates = sorted(
            (
                (snap.get(aid, {}).get("value", 0), aid, uid)
                for aid, uid in inv.items()
                if aid not in config.BLACKLISTED_OUTGOING_ITEMS
                and snap.get(aid, {}).get("value", 0) > 0
            )
        )

        chosen, total = [], 0
        for val, aid, uid in candidates:
            if total >= target_value:
                break
            chosen.append({"assetId": aid, "userAssetId": uid})
            total += val

        return chosen if total >= target_value * 0.9 else []

    async def _summarise_offer(self, offer: dict) -> tuple[int, list[str]]:
        robux = offer.get("robux", 0)
        total = robux
        labels = []
        for item in offer.get("userAssets", []):
            aid = item["assetId"]
            val = await self.values.get_value(aid)
            nm = await self.values.get_name(aid)
            total += val
            labels.append(f"{nm} ({val:,})")
        if robux:
            labels.append(f"{robux:,} Robux")
        return total, labels

    async def _evaluate_inbound(self, trade_id: int) -> tuple[str, str]:
        async with self._ratelimit:
            status, details = await self.api.get(f"{TRADES_API}/v1/trades/{trade_id}")
        if status != 200:
            return "skip", f"Could not load trade details ({status})"

        their_offer = our_offer = None
        for offer in details.get("offers", []):
            if offer["user"]["id"] == self.user_id:
                our_offer = offer
            else:
                their_offer = offer

        if not their_offer or not our_offer:
            return "skip", "Unexpected trade structure"

        if any(i["assetId"] in config.BLACKLISTED_OUTGOING_ITEMS
               for i in our_offer.get("userAssets", [])):
            return "skip", "Would give a blacklisted item"

        if any(i["assetId"] in config.BLACKLISTED_INCOMING_ITEMS
               for i in their_offer.get("userAssets", [])):
            return "decline", "Incoming trade contains blacklisted item"

        recv_val, recv_labels = await self._summarise_offer(their_offer)
        give_val, give_labels = await self._summarise_offer(our_offer)
        ratio = (recv_val / give_val) if give_val else float("inf")
        their_name = their_offer["user"]["name"]

        logging.info(
            f"\n"
            f"  ┌─ Inbound #{trade_id} from {their_name}\n"
            f"  │  RECEIVE : {', '.join(recv_labels) or 'nothing'}  (≈{recv_val:,})\n"
            f"  │  GIVE    : {', '.join(give_labels) or 'nothing'}  (≈{give_val:,})\n"
            f"  │  RATIO   : {ratio:.3f}x  (threshold {config.MIN_GAIN_RATIO}x)\n"
            f"  └─"
        )

        if ratio >= config.MIN_GAIN_RATIO:
            return "accept", f"ratio {ratio:.3f}x >= {config.MIN_GAIN_RATIO}x"
        return "decline", f"ratio {ratio:.3f}x < {config.MIN_GAIN_RATIO}x"

    async def _send_trade(
        self,
        target_user_id: int,
        their_user_asset_ids: list[int],
        their_asset_ids: list[int],
    ) -> bool:
        target_val = sum([await self.values.get_value(aid) for aid in their_asset_ids])
        if target_val == 0:
            logging.info("  [sender] All offered items have unknown value — skipping.")
            return False

        our_items = await self._pick_our_items(target_val)
        if not our_items:
            logging.info(
                f"  [sender] Not enough inventory value to match trade ad "
                f"(need ≈{target_val:,}) — skipping."
            )
            return False

        payload = {
            "offers": [
                {
                    "userId": self.user_id,
                    "userAssets": [{"id": i["userAssetId"]} for i in our_items],
                    "robux": 0,
                },
                {
                    "userId": target_user_id,
                    "userAssets": [{"id": uid} for uid in their_user_asset_ids],
                    "robux": 0,
                },
            ]
        }
        async with self._ratelimit:
            status, body = await self.api.post(f"{TRADES_API}/v1/trades/send", json=payload)
        if status != 200:
            err = body.get("errors", [{}])[0].get("message", "unknown error")
            logging.warning(f"  [sender] Send failed ({status}): {err}")
        return status == 200

    async def value_daemon(self, public: aiohttp.ClientSession):
        while True:
            await self.values.refresh(public)
            await asyncio.sleep(config.VALUE_REFRESH_INTERVAL)

    async def inbound_daemon(self):
        while True:
            async with self._ratelimit:
                status, data = await self.api.get(
                    f"{TRADES_API}/v1/trades/inbound",
                    params={"limit": 25, "sortOrder": "Desc"},
                )
            trades = data.get("data", []) if status == 200 else []
            new_trades = [t for t in trades if t["id"] not in self._processed_inbound]

            if new_trades:
                logging.info(f"[inbound] {len(new_trades)} new trade(s)")

            for trade in new_trades:
                tid = trade["id"]
                self._processed_inbound.add(tid)
                decision, reason = await self._evaluate_inbound(tid)

                if decision == "accept" and config.AUTO_ACCEPT:
                    async with self._ratelimit:
                        status, _ = await self.api.post(f"{TRADES_API}/v1/trades/{tid}/accept")
                    logging.info(f"  -> {'ACCEPTED' if status == 200 else 'ACCEPT FAILED'}: {reason}")
                elif decision == "decline" and config.AUTO_DECLINE:
                    async with self._ratelimit:
                        status, _ = await self.api.post(f"{TRADES_API}/v1/trades/{tid}/decline")
                    logging.info(f"  -> {'DECLINED' if status == 200 else 'DECLINE FAILED'}: {reason}")
                else:
                    logging.info(f"  -> SKIPPED ({decision}): {reason}")

            await asyncio.sleep(config.POLL_INTERVAL)

    async def scanner_daemon(self):
        while True:
            snap = await self.values.snapshot()
            async with self._inv_lock:
                our_items = dict(self._inventory)

            our_total = sum(snap.get(aid, {}).get("value", 0) for aid in our_items)
            if our_total == 0:
                logging.info("[scanner] All owned items have unknown value — skipping.")
                await asyncio.sleep(config.SCAN_INTERVAL)
                continue

            candidates = await scan_group_members(
                api=self.api,
                ratelimit=self._ratelimit,
                values=self.values,
                our_inventory=our_items,
                our_total=our_total,
                contacted_users=self._contacted_users,
                queued_users=self._queued_users,
                bot_user_id=self.user_id,
                sends_today=self._sends_today,
            )

            if not candidates:
                logging.info("[scanner] No new targets found this cycle.")
                await asyncio.sleep(config.SCAN_INTERVAL)
                continue

            candidates.sort(key=lambda c: c[0], reverse=True)
            budget_left = config.MAX_TRADES_PER_DAY - self._sends_today

            for rank, (ratio, user_id, asset_id, user_asset_id) in enumerate(candidates, 1):
                name = snap.get(asset_id, {}).get("name", str(asset_id))
                val = snap.get(asset_id, {}).get("value", 0)
                logging.info(f"  #{rank:02d}  {name} (≈{val:,})  ratio {ratio:.3f}x  → user {user_id}")
                if rank > budget_left:
                    logging.info(f"  [scanner] Budget cap — stopping at #{rank}.")
                    break
                self._queued_users.add(user_id)
                self._seq += 1
                await self._outbound_queue.put((
                    -ratio, self._seq,
                    {
                        "user_id": user_id,
                        "offer_ids": [asset_id],
                        "offer_val": val,
                        "user_asset_ids": [user_asset_id],
                    },
                ))

            await asyncio.sleep(config.SCAN_INTERVAL)

    def _check_daily_reset(self):
        today = datetime.date.today()
        if self._budget_day != today:
            self._budget_day = today
            self._sends_today = 0
            logging.info("[sender] Daily trade counter reset (new UTC day).")

    async def sender_daemon(self):
        while True:
            self._check_daily_reset()

            if self._sends_today >= config.MAX_TRADES_PER_DAY:
                logging.info(
                    f"[sender] Daily cap reached ({config.MAX_TRADES_PER_DAY}). "
                    "Sleeping 5 min…"
                )
                await asyncio.sleep(300)
                continue

            _priority, _seq, job = await self._outbound_queue.get()
            user_id = job["user_id"]
            offer_ids = job["offer_ids"]

            self._queued_users.discard(user_id)

            if user_id in self._contacted_users:
                self._outbound_queue.task_done()
                continue

            self._contacted_users.add(user_id)

            self._check_daily_reset()
            if self._sends_today >= config.MAX_TRADES_PER_DAY:
                logging.info(f"  [sender] Cap hit while processing queue — dropping user {user_id}.")
                self._outbound_queue.task_done()
                continue

            if "user_asset_ids" in job:
                their_uids = job["user_asset_ids"]
            else:
                their_inv = await fetch_inventory(self.api, self._ratelimit, user_id)
                their_uids = [their_inv[aid] for aid in offer_ids if aid in their_inv]

            if len(their_uids) != len(offer_ids):
                logging.info(f"  [sender] User {user_id} no longer has the target item — skipping.")
                self._outbound_queue.task_done()
                await asyncio.sleep(1)
                continue

            if not config.AUTO_SEND:
                logging.info(
                    f"  [sender] WOULD SEND to {user_id} "
                    f"({self._sends_today + 1}/{config.MAX_TRADES_PER_DAY})  "
                    f"[auto_send=False]"
                )
                self._outbound_queue.task_done()
                await asyncio.sleep(1)
                continue

            ok = await self._send_trade(user_id, their_uids, offer_ids)

            if ok:
                self._sends_today += 1
                await self._refresh_inventory()

            logging.info(
                f"  [sender] Trade to {user_id}: {'SENT' if ok else 'FAILED'}  "
                f"({self._sends_today}/{config.MAX_TRADES_PER_DAY} today)"
            )

            self._outbound_queue.task_done()
            await asyncio.sleep(config.SEND_COOLDOWN)

    async def run(self):
        await self.api.open()
        try:
            if not await self.authenticate():
                return

            async with aiohttp.ClientSession(headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Referer": "https://www.rolimons.com/",
                "Origin": "https://www.rolimons.com",
            }) as public:
                await asyncio.gather(
                    self.values.refresh(public),
                    self._refresh_inventory(),
                )

                logging.info("All daemons starting…")
                await asyncio.gather(
                    self.value_daemon(public),
                    self.inbound_daemon(),
                    self.scanner_daemon(),
                    self.sender_daemon(),
                )
        finally:
            await self.api.close()


def main():
    setup_logging()

    if not config.ROBLOSECURITY:
        print(
            "No cookie set.\n"
            "1. Log in to roblox.com in your browser.\n"
            "2. Open DevTools → Application → Cookies → roblox.com.\n"
            "3. Copy the value of '.ROBLOSECURITY'.\n"
            "4. Paste it into ROBLOSECURITY = \"...\" in config.py.\n"
        )
        sys.exit(1)

    try:
        asyncio.run(TradeBot().run())
    except KeyboardInterrupt:
        logging.info("Bot stopped.")


if __name__ == "__main__":
    main()
