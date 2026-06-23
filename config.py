"""
Roblox Trade Bot — Configuration
Edit the values below before running.
"""

# Paste your .ROBLOSECURITY cookie value here (from browser cookies on roblox.com)
ROBLOSECURITY = ""

# Trade decision thresholds
MIN_GAIN_RATIO = 0.85  # accept items worth at least 85% of ours

# Automation flags
AUTO_ACCEPT = True   # Automatically accept trades above the threshold
AUTO_DECLINE = True  # Automatically decline trades below the threshold

# How often to poll for inbound trades (seconds)
POLL_INTERVAL = 5

# How often to refresh Rolimons item values (seconds)
VALUE_REFRESH_INTERVAL = 60

# How often to scan group members for trade targets (seconds)
SCAN_INTERVAL = 5

# Members to check per scan cycle (each member = 1 inventory API call)
ITEMS_PER_SCAN = 5

# Upper value bound for target items, as a multiple of your total inventory value
# 2.0 = accept items worth up to 2x what you own
SCAN_MAX_RATIO = 1.3  # look for items up to 30% more than our value

# Roblox group IDs to scan for trade targets.
# Find a group's ID from its URL: roblox.com/groups/GROUP_ID/Name
# Leave empty to auto-discover trading groups via Roblox search.
TRADE_GROUPS = []

# Seconds to wait between sending consecutive outbound trade offers
# Keep this >= 10 to stay well under Roblox rate limits
SEND_COOLDOWN = 2

# Maximum outbound trades to send per UTC day (Roblox hard limit is 100)
# Set lower to leave headroom for manual trades
MAX_TRADES_PER_DAY = 90

# Set to False to log what WOULD be sent without actually sending
AUTO_SEND = True

# 2Captcha API key — used to auto-solve Arkose Labs challenges on trade sends
# Get yours at https://2captcha.com
CAPTCHA_API_KEY = ""

# Log file path (set to None to disable file logging)
LOG_FILE = "tradebot.log"

# TOTP secret for 2-Step Verification auto-solving.
# How to get it:
#   1. Roblox → Settings → Security → 2-Step Verification → Authenticator App → Set Up
#   2. When the QR code appears, click "Can't scan? Use a code instead" (or similar link)
#   3. Copy the text code shown (looks like: JBSWY3DPEHPK3PXP)
#   4. Paste it below.  Also scan the QR into your phone app as normal.
# Leave empty to disable automatic 2SV solving.
TOTP_SECRET = ""

# Items to never give away, regardless of value (asset IDs as ints)
BLACKLISTED_OUTGOING_ITEMS = []

# Items to never accept, regardless of value (asset IDs as ints)
BLACKLISTED_INCOMING_ITEMS = []
