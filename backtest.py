#!/usr/bin/env python3
"""
Backtest — شبیه‌سازی استراتژی روی داده‌ی تاریخی
--------------------------------------------------
همون منطق eth_signal_bot.py رو روی چند ماه گذشته اجرا می‌کنه و نتیجه رو تو
backtest_results.md می‌نویسه.

⚠️ محدودیت مهم بک‌تست (حتماً بخون):
- چون داده‌ی تاریخی فقط کندل ۱۵ دقیقه‌ای داریم (نه تیک لحظه‌ای)، لحظه‌ی دقیق
  خروج از باکس تقریبی محاسبه می‌شه (با لبه‌ی باکس، نه قیمت لحظه‌ای واقعی).
  یعنی نتیجه‌ی واقعی ربات زنده ممکنه کمی بهتر یا بدتر از این گزارش باشه.
- اگه SL و TP هر دو تو یه کندل لمس بشن، بدبینانه فرض می‌کنیم SL اول خورده
  (برای جلوگیری از گزارش خوش‌بینانه‌ی غیرواقعی).
- این صرفاً شبیه‌سازی قوانین فنیه، نه توصیه‌ی مالی و نه تضمین سودآوری آینده.
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

FSYM = "ETH"
TSYM = "USDT"
CC_HISTOMINUTE_URL = "https://min-api.cryptocompare.com/data/v2/histominute"

TEHRAN = ZoneInfo("Asia/Tehran")
OVERLAP_START_HOUR = 10
OVERLAP_START_MINUTE = 30
RISK_REWARD = 2

BACKTEST_DAYS = int(os.environ.get("BACKTEST_DAYS", "90"))


def fetch_all_15m_candles(days):
    """با صفحه‌بندی (pagination)، کندل‌های ۱۵ دقیقه‌ای N روز گذشته رو می‌گیره."""
    needed = days * 24 * 4 + 200  # کمی مارجین اضافه
    all_candles = []
    to_ts = int(datetime.now(timezone.utc).timestamp())

    while len(all_candles) < needed:
        resp = requests.get(
            CC_HISTOMINUTE_URL,
            params={"fsym": FSYM, "tsym": TSYM, "aggregate": 15, "limit": 2000, "toTs": to_ts},
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("Response") == "Error":
            raise RuntimeError(data.get("Message", "CryptoCompare error"))
        chunk = data["Data"]["Data"]
        if not chunk:
            break
        all_candles = chunk + all_candles
        oldest_time = chunk[0]["time"]
        to_ts = oldest_time - 1
        if len(chunk) < 2:
            break

    candles = []
    for k in all_candles:
        open_time = datetime.fromtimestamp(k["time"], tz=timezone.utc)
        candles.append({
            "open_time": open_time,
            "open": float(k["open"]),
            "high": float(k["high"]),
            "low": float(k["low"]),
            "close": float(k["close"]),
        })
    # حذف تکراری‌ها (ممکنه صفحه‌بندی هم‌پوشانی داشته باشه) و مرتب‌سازی
    seen = set()
    unique = []
    for c in candles:
        key = c["open_time"]
        if key not in seen:
            seen.add(key)
            unique.append(c)
    unique.sort(key=lambda c: c["open_time"])
    return unique


def group_by_tehran_date(candles):
    by_date = defaultdict(list)
    for c in candles:
        tehran_time = c["open_time"].astimezone(TEHRAN)
        by_date[tehran_time.date()].append(c)
    return by_date


def calculate_box_from_candles(day_candles):
    day_high = max(c["high"] for c in day_candles)
    day_low = min(c["low"] for c in day_candles)
    diff = day_high - day_low
    fib_050 = day_high - 0.5 * diff
    fib_0618 = day_high - 0.618 * diff
    return {"box_top": max(fib_050, fib_0618), "box_bottom": min(fib_050, fib_0618)}


def simulate_day(box, today_candles):
    """منطق ورود/خروج رو برای یه روز شبیه‌سازی می‌کنه. سیگنال (اگه بود) رو برمی‌گردونه."""
    overlap_start = datetime.combine(
        list(today_candles)[0]["open_time"].astimezone(TEHRAN).date(),
        datetime.min.time().replace(hour=OVERLAP_START_HOUR, minute=OVERLAP_START_MINUTE),
        tzinfo=TEHRAN,
    )
    status = "waiting"
    entry_direction = None

    for c in today_candles:
        if c["open_time"].astimezone(TEHRAN) < overlap_start:
            continue

        if status == "waiting":
            inside_box = box["box_bottom"] <= c["low"] and c["high"] <= box["box_top"]
            if inside_box:
                entry_direction = "bullish" if c["close"] > c["open"] else "bearish"
                status = "armed"
            continue

        if status == "armed":
            if entry_direction == "bullish" and c["low"] < box["box_bottom"]:
                return {"direction": "SHORT", "entry": box["box_bottom"], "sl": box["box_top"],
                        "signal_time": c["open_time"]}
            if entry_direction == "bearish" and c["high"] > box["box_top"]:
                return {"direction": "LONG", "entry": box["box_top"], "sl": box["box_bottom"],
                        "signal_time": c["open_time"]}
    return None


def resolve_trade(signal, all_candles_after):
    """بعد از سیگنال، می‌بینه اول به TP می‌خوره یا SL."""
    entry = signal["entry"]
    sl = signal["sl"]
    risk = abs(entry - sl)
    tp = entry - RISK_REWARD * risk if signal["direction"] == "SHORT" else entry + RISK_REWARD * risk

    for c in all_candles_after:
        if signal["direction"] == "LONG":
            hit_sl = c["low"] <= sl
            hit_tp = c["high"] >= tp
        else:
            hit_sl = c["high"] >= sl
            hit_tp = c["low"] <= tp

        if hit_sl and hit_tp:
            return "LOSS", tp  # بدبینانه: فرض می‌کنیم SL اول خورده
        if hit_sl:
            return "LOSS", tp
        if hit_tp:
            return "WIN", tp
    return "OPEN", tp


def main():
    print(f"📥 در حال دریافت {BACKTEST_DAYS} روز داده‌ی تاریخی...")
    candles = fetch_all_15m_candles(BACKTEST_DAYS)
    if not candles:
        print("❌ داده‌ای دریافت نشد.", file=sys.stderr)
        sys.exit(1)

    by_date = group_by_tehran_date(candles)
    sorted_dates = sorted(by_date.keys())

    trades = []
    for i in range(1, len(sorted_dates)):
        prev_date = sorted_dates[i - 1]
        curr_date = sorted_dates[i]
        prev_candles = by_date[prev_date]
        curr_candles = by_date[curr_date]

        if len(prev_candles) < 50 or len(curr_candles) < 10:
            continue  # روز ناقصه (مثلاً اولین/آخرین روز داده)

        box = calculate_box_from_candles(prev_candles)
        signal = simulate_day(box, curr_candles)
        if signal:
            after_index = candles.index(
                next(c for c in candles if c["open_time"] == signal["signal_time"])
            )
            outcome, tp = resolve_trade(signal, candles[after_index + 1:])
            trades.append({
                "date": str(curr_date),
                "direction": signal["direction"],
                "entry": signal["entry"],
                "sl": signal["sl"],
                "tp": tp,
                "outcome": outcome,
            })

    wins = sum(1 for t in trades if t["outcome"] == "WIN")
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    opens = sum(1 for t in trades if t["outcome"] == "OPEN")
    resolved = wins + losses
    win_rate = (wins / resolved * 100) if resolved else 0
    total_r = wins * RISK_REWARD - losses * 1

    lines = [
        "# نتیجه‌ی بک‌تست استراتژی ETH",
        "",
        f"بازه: {BACKTEST_DAYS} روز گذشته — تعداد کل سیگنال‌ها: {len(trades)}",
        "",
        f"- ✅ برد: {wins}",
        f"- ❌ باخت: {losses}",
        f"- ⏳ هنوز باز/نامشخص: {opens}",
        f"- 🎯 نرخ برد (فقط معاملات بسته‌شده): {win_rate:.1f}%",
        f"- 📈 مجموع R (با فرض ریسک ثابت ۱ واحد در هر معامله): {total_r:+.1f}R",
        "",
        "| تاریخ | جهت | ورود | SL | TP | نتیجه |",
        "|---|---|---|---|---|---|",
    ]
    for t in trades:
        lines.append(
            f"| {t['date']} | {t['direction']} | {t['entry']:.2f} | {t['sl']:.2f} | "
            f"{t['tp']:.2f} | {t['outcome']} |"
        )

    lines.append("")
    lines.append(
        "⚠️ این گزارش صرفاً شبیه‌سازی قوانین فنی روی داده‌ی گذشته است. عملکرد گذشته "
        "تضمینی برای آینده نیست و این توصیه‌ی مالی رسمی نیست."
    )

    with open("backtest_results.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"✅ تموم شد. {len(trades)} سیگنال پیدا شد ({wins} برد / {losses} باخت / {opens} باز).")
    print("نتیجه‌ی کامل تو backtest_results.md نوشته شد.")


if __name__ == "__main__":
    main()
