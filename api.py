import asyncio
import base64
import json
import logging
from typing import Optional
import aiohttp

TRADES_API = "https://trades.roblox.com"
USERS_API = "https://users.roblox.com"
INVENTORY_API = "https://inventory.roblox.com"
ECONOMY_API = "https://economy.roblox.com"
GROUPS_API = "https://groups.roblox.com"
TWOSTEP_API = "https://twostepverification.roblox.com"


class TwoCaptchaSolver:
    ROBLOX_PUBLIC_KEY = "476068BF-9607-4799-B53D-966BE98E2B81"
    ROBLOX_SUBDOMAIN = "roblox-api.arkoselabs.com"
    API_BASE = "https://api.2captcha.com"
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    )

    def __init__(self, api_key: str):
        self._key = api_key

    async def solve(self, website_url: str, blob: str = "") -> Optional[str]:
        task: dict = {
            "type": "FunCaptchaTaskProxyless",
            "websiteURL": website_url,
            "websitePublicKey": self.ROBLOX_PUBLIC_KEY,
            "funcaptchaApiJSSubdomain": self.ROBLOX_SUBDOMAIN,
            "userAgent": self.UA,
        }
        if blob:
            task["data"] = json.dumps({"blob": blob})

        async with aiohttp.ClientSession() as s:
            async with s.post(
                f"{self.API_BASE}/createTask",
                json={"clientKey": self._key, "task": task},
            ) as resp:
                data = await resp.json()

            if data.get("errorId") != 0:
                logging.warning(f"[captcha] Create-task failed: {data.get('errorDescription', data)}")
                return None

            task_id = data["taskId"]
            logging.info(f"[captcha] Task {task_id} submitted — polling…")

            for attempt in range(36):
                await asyncio.sleep(5)
                async with s.post(
                    f"{self.API_BASE}/getTaskResult",
                    json={"clientKey": self._key, "taskId": task_id},
                ) as resp:
                    result = await resp.json()

                if result.get("status") == "ready":
                    token = result["solution"]["token"]
                    logging.info(f"[captcha] Solved in {(attempt + 1) * 5}s  {token[:60]}…")
                    return token
                if result.get("errorId") != 0:
                    logging.warning(f"[captcha] Solve error: {result.get('errorDescription', result)}")
                    return None

        logging.warning("[captcha] Timed out waiting for 2Captcha solution")
        return None


class RobloxAPI:
    def __init__(
        self,
        roblosecurity: str,
        solver: Optional[TwoCaptchaSolver] = None,
        totp_secret: str = "",
    ):
        self._cookie = f".ROBLOSECURITY={roblosecurity}"
        self._csrf = ""
        self._solver = solver
        self._totp_secret = totp_secret
        self._session: Optional[aiohttp.ClientSession] = None

    async def open(self):
        self._session = aiohttp.ClientSession(headers={
            "Cookie": self._cookie,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json",
            "Referer": "https://www.roblox.com/",
        })
        await self._fetch_csrf()

    async def close(self):
        if self._session:
            await self._session.close()

    async def _fetch_csrf(self):
        try:
            async with self._session.post(f"{TRADES_API}/v1/trades/0/decline") as r:
                token = r.headers.get("x-csrf-token")
                if token:
                    self._csrf = token
        except Exception:
            pass

    async def get(self, url: str, **kwargs) -> tuple[int, dict]:
        async with self._session.get(url, **kwargs) as r:
            try:
                body = await r.json(content_type=None)
            except Exception:
                body = {}
            return r.status, body

    async def post(self, url: str, captcha_page: str = "https://www.roblox.com/", **kwargs) -> tuple[int, dict]:
        base_headers = kwargs.pop("headers", {})
        challenge_headers: dict = {}

        for attempt in range(3):
            headers = {**base_headers, "x-csrf-token": self._csrf, **challenge_headers}
            async with self._session.post(url, headers=headers, **kwargs) as r:
                status = r.status
                resp_hdrs = r.headers
                try:
                    body = await r.json(content_type=None)
                except Exception:
                    body = {}

                if status == 403:
                    logging.debug(
                        f"[post] 403 headers: { {k: v for k, v in resp_hdrs.items()} }\n"
                        f"[post] 403 body: {body}"
                    )
                    new_csrf = resp_hdrs.get("x-csrf-token")
                    if new_csrf and new_csrf != self._csrf and not challenge_headers:
                        self._csrf = new_csrf
                        continue

                    cid = resp_hdrs.get("rblx-challenge-id") or body.get("challengeId")
                    ctype = resp_hdrs.get("rblx-challenge-type") or body.get("challengeType", "")

                    if cid and not challenge_headers:
                        ctype_lower = ctype.lower()

                        if "twostepverification" in ctype_lower:
                            if not self._totp_secret:
                                logging.warning(
                                    "[2sv] Roblox requires 2-Step Verification.\n"
                                    "  1. Roblox → Settings → Security → 2-Step Verification → Authenticator App\n"
                                    "  2. Click 'Can't scan? Use a code instead' to get the text secret\n"
                                    "  3. Set TOTP_SECRET = \"<that code>\" in config.py"
                                )
                                return 403, body

                            cmeta_b64 = resp_hdrs.get("rblx-challenge-metadata", "")
                            try:
                                cmeta = json.loads(base64.b64decode(cmeta_b64 + "=="))
                            except Exception:
                                cmeta = {}

                            tsv_user_id = cmeta.get("userId", "")
                            challenge_id = cmeta.get("challengeId", "")
                            action_type = cmeta.get("actionType", "Generic")

                            if not tsv_user_id or not challenge_id:
                                logging.warning("[2sv] Missing userId/challengeId in metadata — aborting")
                                return 403, body

                            try:
                                import pyotp
                                code = pyotp.TOTP(self._totp_secret).now()
                            except Exception as exc:
                                logging.warning(f"[2sv] TOTP generation failed: {exc}")
                                return 403, body

                            async with self._session.get(
                                f"{TWOSTEP_API}/v1/users/{tsv_user_id}/challenges/authenticator",
                                params={"challengeId": challenge_id},
                            ) as r_init:
                                init_status = r_init.status
                                init_body = await r_init.json(content_type=None)
                            logging.info(f"[2sv] init({init_status}): {init_body}")

                            logging.info(f"[2sv] Submitting TOTP code {code} for user {tsv_user_id}…")
                            async with self._session.post(
                                f"{TWOSTEP_API}/v1/users/{tsv_user_id}/challenges/authenticator/verify",
                                headers={"x-csrf-token": self._csrf},
                                json={
                                    "challengeId": challenge_id,
                                    "actionType": action_type,
                                    "code": code,
                                },
                            ) as r2:
                                tsv_status = r2.status
                                tsv_body = await r2.json(content_type=None)

                            if tsv_status != 200:
                                logging.warning(f"[2sv] Verification failed ({tsv_status}): {tsv_body}")
                                return 403, body

                            verification_token = tsv_body.get("verificationToken", "")
                            logging.info(f"[2sv] Got verificationToken (len={len(verification_token)}): {verification_token[:60]}…")

                            cmeta["verificationToken"] = verification_token
                            updated_meta_b64 = base64.b64encode(
                                json.dumps(cmeta, separators=(',', ':')).encode()
                            ).decode()

                            async with self._session.post(
                                "https://apis.roblox.com/challenge/v1/continue",
                                headers={
                                    "x-csrf-token": self._csrf,
                                    "Origin": "https://www.roblox.com",
                                    "Referer": "https://www.roblox.com/",
                                },
                                json={
                                    "challengeId": cid,
                                    "challengeType": "twostepverification",
                                    "challengeMetadata": updated_meta_b64,
                                },
                            ) as r3:
                                cont_status = r3.status
                                cont_body = await r3.json(content_type=None)
                            logging.info(f"[2sv] continue({cont_status}): {cont_body}")

                            challenge_headers = {
                                "rblx-challenge-id": cid,
                                "rblx-challenge-type": "twostepverification",
                                "rblx-challenge-metadata": updated_meta_b64,
                            }
                            logging.info("[2sv] Retrying trade…")
                            continue

                        elif "captcha" in ctype_lower and self._solver:
                            cmeta_b64 = resp_hdrs.get("rblx-challenge-metadata", "")
                            try:
                                cmeta = json.loads(base64.b64decode(cmeta_b64 + "=="))
                            except Exception:
                                cmeta = {}

                            blob = cmeta.get("dataExchangeBlob", "")
                            unified_id = cmeta.get("unifiedCaptchaId", cid)

                            logging.info(f"[captcha] Challenge received (id={cid[:8]}…) — solving…")
                            token = await self._solver.solve(captcha_page, blob)
                            if not token:
                                logging.warning("[captcha] Could not solve — aborting request")
                                return 403, {"errors": [{"message": "captcha solve failed"}]}

                            solution_b64 = base64.b64encode(json.dumps({
                                "unifiedCaptchaId": unified_id,
                                "captchaToken": token,
                                "actionType": "ACTION_TYPE_ARKOSE_LABS",
                            }).encode()).decode()

                            challenge_headers = {
                                "rblx-challenge-id": cid,
                                "rblx-challenge-type": "captcha",
                                "rblx-challenge-solution": solution_b64,
                            }
                            continue

                        elif "forceauthenticator" in ctype_lower:
                            logging.warning(
                                "[challenge] Roblox is forcing 2-Step Verification setup.\n"
                                "  Fix: Roblox → Settings → Security → 2-Step Verification → Authenticator App."
                            )
                            return 403, body

                return status, body

        return 403, {}
