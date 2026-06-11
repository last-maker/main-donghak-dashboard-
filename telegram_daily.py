#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
동학개미 지표 — 매일 아침 텔레그램 발송 스크립트
- 무료 공개 API에서 최신 값을 받아 요약 메시지를 작성하고
- 텔레그램 봇으로 전송한다.

사용:
  1) 같은 폴더에 .env 파일을 만들고:
       TELEGRAM_BOT_TOKEN=8824702897:AAHJ...
       TELEGRAM_CHAT_ID=123456789
       DASHBOARD_URL=https://본인티스토리주소/entry/투자-대시보드
  2) 의존성 설치: python3 -m pip install requests
  3) 수동 테스트: python3 telegram_daily.py
"""

import os
import sys
import json
import datetime
import urllib.request
import urllib.parse
import urllib.error

# ──────────────────────────────── 설정 로드 ────────────────────────────────
def load_env():
    """같은 폴더의 .env 파일을 읽어 os.environ에 주입."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

load_env()

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
DASH_URL  = os.environ.get("DASHBOARD_URL", "").strip()

if not BOT_TOKEN or not CHAT_ID:
    print("ERR: TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 비어 있습니다 (.env 확인)")
    sys.exit(1)

# ──────────────────────────────── 유틸 ────────────────────────────────
def http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        print(f"[warn] {fn.__name__ if hasattr(fn,'__name__') else 'fetch'}: {e}", file=sys.stderr)
        return default

# ──────────────────────────────── 데이터 수집 ────────────────────────────────
def fetch_crypto_fg():
    """암호화폐 공포·탐욕 지수 (Alternative.me, CORS·키 불필요)."""
    d = http_json("https://api.alternative.me/fng/?limit=1")
    item = d["data"][0]
    return int(item["value"]), item["value_classification"]

def fetch_btc_usd():
    """비트코인 USD 시세 (CoinGecko, 키 불필요)."""
    d = http_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd&include_24hr_change=true")
    return d["bitcoin"]["usd"], d["bitcoin"]["usd_24h_change"]

def fetch_usd_krw():
    """USD/KRW 환율 (exchangerate.host, 키 불필요)."""
    d = http_json("https://api.frankfurter.app/latest?from=USD&to=KRW")
    return d["rates"]["KRW"], d["date"]

# (참고) S&P/VIX/국채 금리는 FRED API 키를 발급받으면 동일 패턴으로 추가 가능
# https://fred.stlouisfed.org/docs/api/api_key.html  (무료, 즉시 발급)

# ──────────────────────────────── 메시지 작성 ────────────────────────────────
def classify_fg(v):
    if v < 25: return "🔴 극공포"
    if v < 45: return "🟠 공포"
    if v < 55: return "🟡 중립"
    if v < 75: return "🟢 탐욕"
    return "🟢🟢 극탐욕"

def build_message():
    today = datetime.datetime.now().strftime("%Y-%m-%d (%a)")
    lines = [f"<b>📊 동학개미 지표 — {today}</b>", ""]

    # 1) 암호화폐 F&G
    fg = safe(fetch_crypto_fg)
    if fg:
        v, label = fg
        lines.append(f"🪙 <b>암호화폐 공포·탐욕</b>: {v} ({classify_fg(v)})")

    # 2) BTC
    btc = safe(fetch_btc_usd)
    if btc:
        price, chg = btc
        arrow = "🔺" if chg >= 0 else "🔻"
        lines.append(f"₿ <b>BTC</b>: ${price:,.0f}  {arrow}{chg:+.2f}%")

    # 3) 환율
    fx = safe(fetch_usd_krw)
    if fx:
        rate, date = fx
        lines.append(f"💱 <b>USD/KRW</b>: {rate:,.2f}원  <i>({date})</i>")

    lines.append("")
    if DASH_URL:
        lines.append(f"📈 <a href=\"{DASH_URL}\">대시보드에서 전체 보기 →</a>")
    lines.append("<i>매일 아침 08:00 자동 발송</i>")
    return "\n".join(lines)

# ──────────────────────────────── 텔레그램 전송 ────────────────────────────────
def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode("utf-8"))
            if resp.get("ok"):
                print(f"✓ 전송 성공 (message_id={resp['result']['message_id']})")
                return True
            print(f"✗ 텔레그램 API 오류: {resp}")
            return False
    except urllib.error.HTTPError as e:
        print(f"✗ HTTPError {e.code}: {e.read().decode('utf-8', errors='ignore')}")
        return False

# ──────────────────────────────── main ────────────────────────────────
if __name__ == "__main__":
    msg = build_message()
    print("--- 보낼 메시지 ---")
    print(msg)
    print("--- 전송 시작 ---")
    ok = send_telegram(msg)
    sys.exit(0 if ok else 2)
