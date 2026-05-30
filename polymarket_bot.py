"""
Polymarket Telegram Alert Bot v3
---------------------------------
Setup: pip install requests python-telegram-bot==20.7
Run:   python polymarket_bot.py
"""

import os
import requests
import json
import logging
import time
import asyncio
from datetime import datetime, timezone
from telegram import Bot

# ─────────────────────────────────────────
# SETTINGS
# ─────────────────────────────────────────

BOT_TOKEN               = os.getenv("BOT_TOKEN")
CHAT_ID                 = os.getenv("CHAT_ID")
POLYMARKET_URL          = "https://polymarket.com"
POLYTRACK_URL           = "https://polytrack-beta.vercel.app"
GAMMA_API               = "https://gamma-api.polymarket.com"
DATA_API                = "https://data-api.polymarket.com"
HEADERS                 = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

ODDS_CHANGE_THRESHOLD   = 0.10
VOLUME_SPIKE_MULTIPLIER = 2.0
MIN_VOLUME_USD          = 50_000
MIN_POSITION_USD        = 50_000      # Alert for positions >= $50K
MIN_NEW_MARKET_VOLUME   = 5_000_000   # Only alert new markets with >= $5M volume
LEADERBOARD_PNL_DELTA   = 10_000      # Alert when top-10 PnL shifts by >= $10K
HOURS_UNTIL_CLOSE       = 24
CHECK_INTERVAL_MINUTES  = 15
LEADERBOARD_SIZE        = 10

# ─────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────
# STATE
# ─────────────────────────────────────────

previous_state    = {}
known_markets     = set()
seen_trade_ids    = set()
leaderboard_state = {}

# ─────────────────────────────────────────
# TELEGRAM
# ─────────────────────────────────────────

RATE_LIMIT_BURST = 3    # messages per window before pause
RATE_LIMIT_PAUSE = 2.0  # seconds to wait after each burst
_burst_count     = 0

async def send_message(text):
    global _burst_count

    # Pause after every RATE_LIMIT_BURST messages
    if _burst_count > 0 and _burst_count % RATE_LIMIT_BURST == 0:
        log.info(f"Rate limit: {RATE_LIMIT_BURST} messages sent, waiting {RATE_LIMIT_PAUSE}s...")
        time.sleep(RATE_LIMIT_PAUSE)

    try:
        bot = Bot(token=BOT_TOKEN)
        await bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        _burst_count += 1
        log.info(f"Message sent ({_burst_count}): {text[:60]}...")
    except Exception as e:
        log.error(f"Telegram error: {e}")

def send(text):
    asyncio.run(send_message(text))

# ─────────────────────────────────────────
# API
# ─────────────────────────────────────────

def get_markets(limit=50):
    try:
        r = requests.get(
            f"{GAMMA_API}/markets",
            params={"limit": limit, "active": "true", "closed": "false",
                    "_sort": "volume", "_order": "DESC"},
            headers=HEADERS, timeout=15
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.error(f"Markets API error: {e}")
        return []


def get_recent_trades(limit=100):
    try:
        r = requests.get(
            f"{DATA_API}/trades",
            params={
                "limit": limit,
                "filterType": "CASH",
                "filterAmount": MIN_POSITION_USD,
                "takerOnly": "false",
            },
            headers=HEADERS, timeout=15
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        log.error(f"Trades API error: {e}")
        return []


def get_leaderboard():
    try:
        r = requests.get(
            f"{DATA_API}/v1/leaderboard",
            params={"limit": LEADERBOARD_SIZE, "offset": 0,
                    "orderBy": "PNL", "timePeriod": "ALL"},
            headers=HEADERS, timeout=15
        )
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception as e:
        log.error(f"Leaderboard API error: {e}")
        return []


def parse_market(m):
    try:
        prices    = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
        yes_price = float(prices[0]) if prices else 0.5
    except:
        yes_price = 0.5
    return {
        "id":        m.get("id", ""),
        "title":     m.get("question", m.get("title", "Unknown")),
        "yes_price": yes_price,
        "volume":    float(m.get("volume", 0)),
        "end_date":  m.get("endDate", ""),
        "slug":      m.get("groupSlug") or m.get("slug", ""),
    }

# ─────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────

def fmt_vol(v):
    if v >= 1_000_000_000: return f"${v/1_000_000_000:.1f}B"
    if v >= 1_000_000:     return f"${v/1_000_000:.1f}M"
    if v >= 1_000:         return f"${v/1_000:.0f}K"
    return f"${v:.0f}"

def murl(slug):
    return f"{POLYMARKET_URL}/event/{slug}" if slug else POLYMARKET_URL

def polytrack_footer():
    return f"\n🔍 <a href='{POLYTRACK_URL}'>PolyTrack Analytics</a>"

# ─────────────────────────────────────────
# ALERTS
# ─────────────────────────────────────────

def alert_odds_change(m, old, new):
    d = "📈" if new > old else "📉"
    send(
        f"{d} <b>ODDS CHANGE</b>\n\n"
        f"📌 {m['title']}\n\n"
        f"Yes: <b>{old*100:.0f}% → {new*100:.0f}%</b> "
        f"({'+' if new>old else ''}{(new-old)*100:.0f} pts)\n"
        f"💰 Volume: {fmt_vol(m['volume'])}\n"
        f"🔗 <a href='{murl(m['slug'])}'>View Market</a>"
        f"{polytrack_footer()}\n\n"
        f"#Polymarket"
    )

def alert_volume_spike(m, old_v, new_v):
    send(
        f"🔥 <b>VOLUME SPIKE</b>\n\n"
        f"📌 {m['title']}\n\n"
        f"💰 {fmt_vol(old_v)} → <b>{fmt_vol(new_v)}</b> ({new_v/old_v:.1f}x)\n"
        f"Yes: {m['yes_price']*100:.0f}% | No: {(1-m['yes_price'])*100:.0f}%\n"
        f"🔗 <a href='{murl(m['slug'])}'>View Market</a>"
        f"{polytrack_footer()}\n\n"
        f"#Polymarket"
    )

def alert_new_market(m):
    send(
        f"🆕 <b>NEW MARKET — {fmt_vol(m['volume'])} Volume</b>\n\n"
        f"📌 {m['title']}\n\n"
        f"Yes: {m['yes_price']*100:.0f}% | No: {(1-m['yes_price'])*100:.0f}%\n"
        f"💰 Volume: {fmt_vol(m['volume'])}\n"
        f"🔗 <a href='{murl(m['slug'])}'>View Market</a>"
        f"{polytrack_footer()}\n\n"
        f"#Polymarket"
    )

def alert_closing_soon(m, hours_left):
    send(
        f"⏰ <b>MARKET CLOSING SOON</b>\n\n"
        f"📌 {m['title']}\n\n"
        f"⌛ <b>{hours_left:.0f} hours</b> remaining\n"
        f"Yes: {m['yes_price']*100:.0f}% | No: {(1-m['yes_price'])*100:.0f}%\n"
        f"💰 Volume: {fmt_vol(m['volume'])}\n"
        f"🔗 <a href='{murl(m['slug'])}'>View Market</a>"
        f"{polytrack_footer()}\n\n"
        f"#Polymarket"
    )

def alert_large_position(trade):
    side       = (trade.get("side") or "BUY").upper()
    outcome    = trade.get("outcome") or trade.get("asset", "Yes")
    price      = float(trade.get("price") or 0.5)
    size       = float(trade.get("size") or 0)
    usd_value  = float(trade.get("usdcSize") or trade.get("cashAmount") or size * price)
    title      = (trade.get("title") or trade.get("market")
                  or trade.get("conditionId", "Unknown Market"))
    slug       = trade.get("groupSlug") or trade.get("slug", "")
    side_emoji = "🟢" if side == "BUY" else "🔴"

    send(
        f"🐋 <b>LARGE POSITION OPENED</b>\n\n"
        f"📌 {title}\n\n"
        f"{side_emoji} <b>{side} {outcome}</b>\n"
        f"💵 Size: <b>{fmt_vol(usd_value)}</b>\n"
        f"🎯 Price: {price*100:.1f}¢\n"
        f"🔗 <a href='{murl(slug)}'>View Market</a>"
        f"{polytrack_footer()}\n\n"
        f"#Polymarket #Whale"
    )

def alert_leaderboard_move(rank, address, old_pnl, new_pnl):
    pnl_change  = new_pnl - old_pnl
    emoji       = "📈" if pnl_change > 0 else "📉"
    short_addr  = f"{address[:6]}...{address[-4:]}" if len(address) > 10 else address

    send(
        f"{emoji} <b>LEADERBOARD MOVE — Rank #{rank}</b>\n\n"
        f"👤 Trader: <code>{short_addr}</code>\n"
        f"💰 PnL: {fmt_vol(old_pnl)} → <b>{fmt_vol(new_pnl)}</b>\n"
        f"{'📈' if pnl_change > 0 else '📉'} Change: {'+' if pnl_change > 0 else ''}{fmt_vol(abs(pnl_change))}\n"
        f"🔗 <a href='{POLYMARKET_URL}/profile/{address}'>View Profile</a>"
        f"{polytrack_footer()}\n\n"
        f"#Polymarket #Leaderboard"
    )

# ─────────────────────────────────────────
# CHECK: LARGE POSITIONS ($50K+)
# ─────────────────────────────────────────

def trade_id(t):
    return (t.get("id") or t.get("tradeId") or t.get("transactionHash") or
            t.get("proxyWallet", "") + str(t.get("timestamp", "")))


def load_seen_trades():
    """Silently populate seen_trade_ids on startup to avoid alerting on existing trades."""
    trades = get_recent_trades()
    for t in trades:
        tid = trade_id(t)
        if tid:
            seen_trade_ids.add(tid)
    log.info(f"Initial trade scan done. {len(seen_trade_ids)} trades loaded.")


def check_large_positions():
    # data-api filters server-side via filterType=CASH&filterAmount, all returned trades qualify
    trades = get_recent_trades()
    for t in trades:
        tid = trade_id(t)
        if not tid or tid in seen_trade_ids:
            continue
        seen_trade_ids.add(tid)
        alert_large_position(t)

    if len(seen_trade_ids) > 10_000:
        seen_trade_ids.clear()

# ─────────────────────────────────────────
# CHECK: TOP 10 LEADERBOARD
# ─────────────────────────────────────────

def check_leaderboard():
    global leaderboard_state
    traders = get_leaderboard()
    if not traders:
        return

    for rank, trader in enumerate(traders[:LEADERBOARD_SIZE], start=1):
        address = (trader.get("address") or trader.get("proxyWallet")
                   or trader.get("proxy_wallet", ""))
        if not address:
            continue

        pnl = float(trader.get("pnl") or trader.get("profitLoss") or 0)

        if address in leaderboard_state:
            old_pnl = leaderboard_state[address]["pnl"]
            if abs(pnl - old_pnl) >= LEADERBOARD_PNL_DELTA:
                alert_leaderboard_move(rank, address, old_pnl, pnl)

        leaderboard_state[address] = {"rank": rank, "pnl": pnl}

# ─────────────────────────────────────────
# CHECK: MARKETS
# ─────────────────────────────────────────

def initial_scan():
    """Silently populate known_markets on first run to avoid startup spam."""
    global previous_state, known_markets
    log.info("Initial scan — loading existing markets (no alerts)...")
    raw = get_markets(50)
    now = datetime.now(timezone.utc)
    for m in [parse_market(x) for x in raw]:
        mid, vol, price = m["id"], m["volume"], m["yes_price"]
        if vol < MIN_VOLUME_USD:
            continue
        known_markets.add(mid)
        hours_left = None
        if m["end_date"]:
            try:
                end = datetime.fromisoformat(m["end_date"].replace("Z", "+00:00"))
                hours_left = (end - now).total_seconds() / 3600
            except:
                pass
        previous_state[mid] = {"price": price, "volume": vol, "hours_left": hours_left}
    log.info(f"Initial scan done. {len(known_markets)} markets loaded.")


def check_markets():
    global previous_state, known_markets
    log.info("Checking markets...")
    raw = get_markets(50)
    if not raw:
        log.warning("No data received.")
        return

    now = datetime.now(timezone.utc)
    for m in [parse_market(x) for x in raw]:
        mid, vol, price = m["id"], m["volume"], m["yes_price"]
        if vol < MIN_VOLUME_USD:
            continue

        if mid not in known_markets:
            if vol >= MIN_NEW_MARKET_VOLUME:
                alert_new_market(m)
            known_markets.add(mid)

        if mid in previous_state:
            prev = previous_state[mid]
            if abs(price - prev["price"]) >= ODDS_CHANGE_THRESHOLD:
                alert_odds_change(m, prev["price"], price)
            if prev["volume"] > 0 and vol >= prev["volume"] * VOLUME_SPIKE_MULTIPLIER:
                alert_volume_spike(m, prev["volume"], vol)

        if m["end_date"]:
            try:
                end = datetime.fromisoformat(m["end_date"].replace("Z", "+00:00"))
                hl  = (end - now).total_seconds() / 3600
                ph  = previous_state.get(mid, {}).get("hours_left")
                if 0 < hl <= HOURS_UNTIL_CLOSE and (ph is None or ph > HOURS_UNTIL_CLOSE):
                    alert_closing_soon(m, hl)
                previous_state.setdefault(mid, {})["hours_left"] = hl
            except:
                pass

        previous_state[mid] = {
            "price":      price,
            "volume":     vol,
            "hours_left": previous_state.get(mid, {}).get("hours_left")
        }

    log.info(f"Done. {len(raw)} markets processed.")

# ─────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────

if __name__ == "__main__":
    if not BOT_TOKEN or not CHAT_ID:
        raise EnvironmentError("BOT_TOKEN and CHAT_ID environment variables must be set.")
    log.info("Bot starting...")
    send(
        "✅ <b>Polymarket Alert Bot active!</b>\n\n"
        "Alerts every 15 minutes:\n"
        "📈 Odds change 10%+\n"
        "🔥 Volume 2x spike\n"
        f"🆕 New market with {fmt_vol(MIN_NEW_MARKET_VOLUME)}+ volume\n"
        "⏰ Market closing in 24h\n"
        f"🐋 Position opened {fmt_vol(MIN_POSITION_USD)}+\n"
        "🏆 Top 10 leaderboard move\n\n"
        f"🔍 <a href='{POLYTRACK_URL}'>PolyTrack Analytics</a>\n\n"
        f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )

    initial_scan()
    load_seen_trades()
    while True:
        check_markets()
        check_large_positions()
        check_leaderboard()
        log.info(f"Next check in {CHECK_INTERVAL_MINUTES} minutes...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)
