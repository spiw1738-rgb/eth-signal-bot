#!/usr/bin/env python3
"""
ETH Session-Overlap Fibonacci Box Signal Bot
---------------------------------------------
استراتژی (خلاصه):
1. باکس روزانه = ناحیه‌ی بین فیبوناچی ۰.۵ و ۰.۶۱۸ روی سقف/کف روز قبل (به وقت تهران).
2. باکس فقط از ساعت ۱۰:۳۰ به وقت تهران (شروع همپوشانی توکیو-لندن) تا آخر همون روز فعاله.
3. وقتی یه کندل ۱۵ دقیقه‌ای کامل تو باکس بسته بشه → وضعیت "آماده‌باش".
4. بعد از آماده‌باش، هر ۵ دقیقه قیمت لحظه‌ای چک می‌شه؛ همین که قیمت از باکس در جهت
   مخالفِ کندلِ ورودی خارج بشه (نیازی به بسته‌شدن کندل نیست) → سیگنال صادر می‌شه.
5. SL = لبه‌ی مقابل باکس. TP = دو برابر فاصله‌ی SL تا Entry (ریسک به ریوارد ۱:۲).

نحوه‌ی اجرا:
    export TELEGRAM_BOT_TOKEN="..."
    export TELEGRAM_CHAT_ID="..."
    python eth_signal_bot.py

⚠️ این ابزار صرفاً اجرای خودکار قوانین فنی‌ایه که خودتون تعریف کردید. توصیه‌ی مالی
رسمی نیست و هیچ تضمینی برای سودآوری استراتژی وجود نداره.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

SYMBOL = "ETHUSDT"
BYBIT_KLINES_URL = "https://api.bybit.com/v5/market/kline"
BYBIT_TICKER_URL = "https://api.bybit.com/v5/market/tickers"
STATE_FILE = "state.json"

TEHRAN = ZoneInfo("Asia/Tehran")
OVERLAP_START_HOUR = 10  # 10:30 به وقت تهران، شروع همپوشانی توکیو-لندن
OVERLAP_START_MINUTE = 30

RISK_REWARD = 2  # TP = دو برابر فاصله‌ی SL تا Entry


# ---------------------------------------------------------------------------
# ابزارهای کمکی برای گرفتن داده از Bybit
# ---------------------------------------------------------------------------
# Bybit بازه‌ی کندل رو به‌صورت عدد دقیقه یا "D" می‌خواد، نه "15m"/"1h"
INTERVAL_MAP = {"15m": "15", "1h": "60"}


def fetch_klines(interval, limit):
    resp = requests.get(
        BYBIT_KLINES_URL,
        params={
            "category": "linear",
            "symbol": SYMBOL,
            "interval": INTERVAL_MAP[interval],
            "limit": limit,
        },
        timeout=15,
    )
    resp.raise_for_status()
    raw = resp.json()["result"]["list"]  # Bybit جدیدترین کندل رو اول لیست می‌ده
    candles = []
    for k in reversed(raw):  # برگردوندن به ترتیب زمانی صعودی
        open_time = datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc)
        candles.append({
            "open_time": open_time,
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "close_time": open_time,  # برای چک "بسته‌شده بودن" کافیه
        })
    return candles


def fetch_current_price():
    resp = requests.get(
        BYBIT_TICKER_URL,
        params={"category": "linear", "symbol": SYMBOL},
        timeout=15,
    )
    resp.raise_for_status()
    return float(resp.json()["result"]["list"][0]["lastPrice"])


# ---------------------------------------------------------------------------
# محاسبه‌ی باکس روزانه
# ---------------------------------------------------------------------------
def get_previous_tehran_day_bounds(now_tehran):
    """بازه‌ی UTC معادل «دیروز» به وقت تهران رو برمی‌گردونه."""
    today_start_tehran = now_tehran.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start_tehran = today_start_tehran - timedelta(days=1)
    yesterday_end_tehran = today_start_tehran
    return yesterday_start_tehran.astimezone(timezone.utc), yesterday_end_tehran.astimezone(timezone.utc)


def calculate_box(now_tehran):
    """سقف/کف روز قبل رو با کندل‌های ۱ ساعته می‌گیره و باکس ۰.۵-۰.۶۱۸ رو حساب می‌کنه."""
    start_utc, end_utc = get_previous_tehran_day_bounds(now_tehran)
    # کندل ۱ ساعته برای پوشش کامل ۲۴ ساعت دیروز (حداکثر ۲۴ کندل کافیه)
    candles = fetch_klines("1h", 30)
    day_candles = [c for c in candles if start_utc <= c["open_time"] < end_utc]
    if not day_candles:
        return None

    day_high = max(c["high"] for c in day_candles)
    day_low = min(c["low"] for c in day_candles)
    diff = day_high - day_low

    fib_050 = day_high - 0.5 * diff
    fib_0618 = day_high - 0.618 * diff

    box_top = max(fib_050, fib_0618)
    box_bottom = min(fib_050, fib_0618)
    return {"box_top": box_top, "box_bottom": box_bottom, "day_high": day_high, "day_low": day_low}


# ---------------------------------------------------------------------------
# حافظه‌ی وضعیت (بین اجراهای مختلف حفظ می‌شه)
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# تلگرام
# ---------------------------------------------------------------------------
def send_telegram(text, token, chat_id):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, data={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    })
    if not resp.ok:
        print(f"⚠️  ارسال تلگرام ناموفق بود: {resp.status_code} {resp.text}", file=sys.stderr)


def format_signal_message(direction, entry, sl, tp, box):
    emoji = "🟢 خرید (Long)" if direction == "LONG" else "🔴 فروش (Short)"
    return (
        f"📊 *سیگنال جدید ETH/USDT*\n\n"
        f"{emoji}\n\n"
        f"ورود: `{entry:.2f}`\n"
        f"حد ضرر (SL): `{sl:.2f}`\n"
        f"هدف سود (TP): `{tp:.2f}` (ریسک به ریوارد ۱:{RISK_REWARD})\n\n"
        f"باکس روز: `{box['box_bottom']:.2f}` تا `{box['box_top']:.2f}`\n\n"
        f"⚠️ این پیام صرفاً اجرای خودکار قوانین فنی تعریف‌شده است و توصیه‌ی مالی رسمی نیست."
    )


# ---------------------------------------------------------------------------
# منطق اصلی
# ---------------------------------------------------------------------------
def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("❌ TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID باید ست بشن.", file=sys.stderr)
        sys.exit(1)

    now_tehran = datetime.now(TEHRAN)
    today_str = now_tehran.strftime("%Y-%m-%d")
    state = load_state()

    # اگه روز عوض شده، وضعیت رو ریست کن و باکس جدید بساز
    if state.get("date") != today_str:
        state = {"date": today_str, "status": "waiting"}

    # قبل از شروع همپوشانی توکیو-لندن، کاری نکن
    overlap_start = now_tehran.replace(
        hour=OVERLAP_START_HOUR, minute=OVERLAP_START_MINUTE, second=0, microsecond=0
    )
    if now_tehran < overlap_start:
        print("⏳ هنوز به ساعت شروع همپوشانی نرسیدیم.")
        save_state(state)
        return

    # اگه امروز قبلاً سیگنال داده شده، دیگه کاری نکن
    if state.get("status") == "signaled":
        print("✅ امروز قبلاً سیگنال صادر شده.")
        return

    # باکس رو (اگه هنوز محاسبه نشده) بساز
    if "box" not in state:
        box = calculate_box(now_tehran)
        if box is None:
            print("⚠️  نتونستم داده‌ی روز قبل رو بگیرم.")
            save_state(state)
            return
        state["box"] = box
        save_state(state)
    box = state["box"]

    if state["status"] == "waiting":
        # آخرین کندل ۱۵ دقیقه‌ای کامل (بسته‌شده) رو بررسی کن.
        # کندل آخر تو لیست بایبیت معمولاً هنوز در حال شکل‌گیریه، پس نادیده‌ش می‌گیریم.
        candles = fetch_klines("15m", 4)
        if len(candles) < 2:
            save_state(state)
            return
        last = candles[-2]

        inside_box = box["box_bottom"] <= last["low"] and last["high"] <= box["box_top"]
        if inside_box:
            entry_direction = "bullish" if last["close"] > last["open"] else "bearish"
            state["status"] = "armed"
            state["entry_candle_direction"] = entry_direction
            print(f"🎯 کندل ورودی ({entry_direction}) تو باکس بسته شد. رفتیم تو حالت آماده‌باش.")
        save_state(state)
        return

    if state["status"] == "armed":
        price = fetch_current_price()
        entry_direction = state["entry_candle_direction"]

        # اگه کندل ورودی صعودی بود، دنبال خروج نزولی (زیر باکس) می‌گردیم → سیگنال فروش
        # اگه کندل ورودی نزولی بود، دنبال خروج صعودی (بالای باکس) می‌گردیم → سیگنال خرید
        signal_direction = None
        if entry_direction == "bullish" and price < box["box_bottom"]:
            signal_direction = "SHORT"
            sl = box["box_top"]
        elif entry_direction == "bearish" and price > box["box_top"]:
            signal_direction = "LONG"
            sl = box["box_bottom"]

        if signal_direction:
            entry = price
            risk = abs(entry - sl)
            tp = entry - RISK_REWARD * risk if signal_direction == "SHORT" else entry + RISK_REWARD * risk

            message = format_signal_message(signal_direction, entry, sl, tp, box)
            send_telegram(message, token, chat_id)

            state["status"] = "signaled"
            print(f"🚀 سیگنال {signal_direction} صادر شد.")

        save_state(state)


if __name__ == "__main__":
    main()
