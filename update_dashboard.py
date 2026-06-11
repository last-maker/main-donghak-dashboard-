#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
동학개미 대시보드 — DK_DATA 자동 갱신 스크립트
- 무료 공개 API에서 최신 지표를 받아 investment_dashboard.html 안의
  /*DK_DATA_START*/ ... /*DK_DATA_END*/ 블록만 교체한다.
- 표준 라이브러리만 사용 (별도 설치 불필요).

사용:
  python3 update_dashboard.py            # HTML 갱신
  python3 update_dashboard.py --dry      # 파일 수정 없이 결과만 출력

데이터 출처 (전부 무료·키 불필요) — 항목마다 여러 소스를 순서대로 시도:
  FRED fredgraph.csv      — S&P500·나스닥·VIX·WTI·국채금리·하이일드·CPI·실업률·연준 B/S·한국 기준금리
  Stooq CSV               — 코스피·금·달러인덱스·섹터 ETF (및 지수 폴백)
  CoinGecko               — 비트코인 가격·스파크라인
  Frankfurter             — USD/KRW
  CNN Fear & Greed JSON   — 주식 공포·탐욕
  Alternative.me          — 암호화폐 공포·탐욕
  OECD SDMX               — 한국 CPI (YoY)
  연준 공식 캘린더 페이지   — FOMC 일정 자동 스크랩
  Upbit                   — 김치 프리미엄 계산
  Yahoo Finance           — 최후 폴백 전용 (429 차단이 잦아 1순위에서 제외)
"""

import os
import re
import sys
import json
import csv
import io
import time
import datetime
import urllib.request
import urllib.error
import http.cookiejar

# ════════════════════════════════════════════════════════════════
# ① 설정 — 전부 자동이라 평소엔 건드릴 필요 없음
# ════════════════════════════════════════════════════════════════

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "investment_dashboard.html")

# 버핏 지표: None이면 Wilshire5000(^FTW5000) ÷ 미국 GDP 로 자동 계산.
BUFFETT_OVERRIDE = None

# FOMC 일정: 연준 공식 페이지를 자동 스크랩. 아래는 스크랩 실패 시 비상용.
FOMC_FALLBACK = [
    ("2026-01-27", "2026-01-28"), ("2026-03-17", "2026-03-18"),
    ("2026-04-28", "2026-04-29"), ("2026-06-16", "2026-06-17"),
    ("2026-07-28", "2026-07-29"), ("2026-09-15", "2026-09-16"),
    ("2026-10-27", "2026-10-28"), ("2026-12-08", "2026-12-09"),
]

# ════════════════════════════════════════════════════════════════
# ② 공용 유틸
# ════════════════════════════════════════════════════════════════

UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
      "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9,ko;q=0.8"}

import random

_last_request_time = 0.0

def http_get(url, timeout=15, headers=None, retries=3):
    global _last_request_time
    
    # 도메인 별 스로틀링(Throttling) 시간 정의
    delay_required = 1.0
    if "stooq.com" in url or "fred.stlouisfed.org" in url:
        delay_required = 2.0
    elif "yahoo.com" in url:
        delay_required = 1.5
    
    gap = delay_required - (time.time() - _last_request_time)
    if gap > 0:
        time.sleep(gap + random.uniform(0.1, 0.4))  # 약간의 랜덤성(Jitter) 추가
        
    h = dict(UA)
    
    # FRED, Stooq 및 Yahoo WAF 우회 헤더 정의
    if "fred.stlouisfed.org" in url:
        h = {}
    elif "stooq.com" in url or "stooq.pl" in url:
        h["User-Agent"] = "Mozilla/5.0"
        if "Accept-Language" in h:
            del h["Accept-Language"]
    elif "yahoo.com" in url:
        h["User-Agent"] = "Mozilla/5.0"
        if "Accept-Language" in h:
            del h["Accept-Language"]
        if "Accept" in h:
            del h["Accept"]
        
    if headers:
        h.update(headers)
        
    backoff = 3.0
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                _last_request_time = time.time()
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            last_err = e
            # HTTP 429, 403, 503 등 재시도 가능한 에러 처리
            if e.code in (429, 403, 503) and attempt < retries - 1:
                retry_after = e.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait_time = int(retry_after)
                else:
                    wait_time = backoff + random.uniform(0.5, 1.5)
                print(f"  [warn] HTTP {e.code} 발생 ({url}). {wait_time:.1f}초 대기 후 재시도합니다... (시도 {attempt+1}/{retries})", file=sys.stderr)
                time.sleep(wait_time)
                backoff *= 2
            else:
                print(f"  [error] HTTP {e.code} ({url})", file=sys.stderr)
                raise e
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                wait_time = backoff + random.uniform(0.5, 1.5)
                print(f"  [warn] 통신 실패 ({url}): {e}. {wait_time:.1f}초 대기 후 재시도합니다... (시도 {attempt+1}/{retries})", file=sys.stderr)
                time.sleep(wait_time)
                backoff *= 2
            else:
                print(f"  [error] 통신 실패 ({url}): {e}", file=sys.stderr)
                raise e
    _last_request_time = time.time()
    if last_err:
        raise last_err

def http_json(url, timeout=15, headers=None):
    return json.loads(http_get(url, timeout, headers))

def safe(label, fn, default=None):
    try:
        return fn()
    except Exception as e:
        print(f"  [warn] {label}: {e}", file=sys.stderr)
        return default

def rnd(x, d=2):
    return round(float(x), d) if d else int(round(float(x)))

_YAHOO_SPARK_CACHE = {}

def prefetch_yahoo_spark(symbols):
    """Yahoo Finance spark API를 사용해 여러 심볼의 시세를 단 한 번에 조회하여 캐싱한다."""
    global _YAHOO_SPARK_CACHE
    if not symbols:
        return
    symbols_str = ",".join(symbols)
    last_err = None
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v7/finance/spark?symbols={symbols_str}&range=1mo&interval=1d"
        try:
            data = http_json(url)
            results = data.get("spark", {}).get("result", [])
            for r in results:
                symbol = r.get("symbol")
                response_list = r.get("response", [])
                if not response_list:
                    continue
                res = response_list[0]
                meta = res.get("meta", {})
                closes = [c for c in res.get("indicators", {}).get("quote", [{}])[0].get("close", []) if c is not None]
                if not closes:
                    continue
                last = meta.get("regularMarketPrice") or closes[-1]
                prev = meta.get("chartPreviousClose") or (closes[-2] if len(closes) > 1 else last)
                
                if len(closes) >= 2:
                    if abs(closes[-1] - last) < 1e-9:
                        prev = closes[-2]
                    else:
                        prev = closes[-1]
                chg = (last / prev - 1) * 100 if prev else 0.0
                spark = closes[-8:] if len(closes) >= 8 else closes
                if abs(spark[-1] - last) > 1e-9:
                    spark = (spark + [last])[-8:]
                    
                _YAHOO_SPARK_CACHE[symbol.upper()] = (last, chg, [rnd(v, 2) for v in spark])
            return
        except Exception as e:
            last_err = e
    print(f"  [warn] prefetch_yahoo_spark 실패 ({symbols_str}): {last_err}", file=sys.stderr)

def yahoo_chart(symbol, rng="1mo", interval="1d"):
    """캐시된 야후 차트 데이터를 반환하며, 캐시에 없으면 개별 조회를 수행한다."""
    sym_upper = symbol.upper()
    if sym_upper in _YAHOO_SPARK_CACHE:
        return _YAHOO_SPARK_CACHE[sym_upper]
        
    print(f"  [info] Yahoo Spark 캐시 미스: {sym_upper} 개별 조회 시도", file=sys.stderr)
    last_err = None
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{urllib.request.quote(symbol)}?range={rng}&interval={interval}"
        try:
            d = http_json(url)
            res = d["chart"]["result"][0]
            closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
            meta = res["meta"]
            last = meta.get("regularMarketPrice") or closes[-1]
            prev = meta.get("chartPreviousClose") or (closes[-2] if len(closes) > 1 else last)
            if len(closes) >= 2:
                prev = closes[-2] if abs(closes[-1] - last) < 1e-9 else closes[-1]
            chg = (last / prev - 1) * 100 if prev else 0.0
            spark = closes[-8:] if len(closes) >= 8 else closes
            if abs(spark[-1] - last) > 1e-9:
                spark = (spark + [last])[-8:]
            return last, chg, [rnd(v, 2) for v in spark]
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError(f"Yahoo {symbol} 조회 실패")

def fred_csv(series_id, days=60):
    """FRED 그래프 CSV (키 불필요): [(date, value), ...] 최근 days일."""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    url = (f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
           f"&cosd={start}&coed={end}")
    rows = list(csv.reader(io.StringIO(http_get(url))))
    out = []
    for r in rows[1:]:
        if len(r) >= 2 and r[1] not in (".", ""):
            out.append((r[0], float(r[1])))
    return out

def fred_last(series_id, days=60):
    rows = fred_csv(series_id, days)
    return rows[-1][1] if rows else None

# ── 멀티소스 차트 (Yahoo 429 차단 대응) ──

def closes_to_metrics(closes):
    """종가 리스트 → (마지막, 전일 대비 %, spark 8개)."""
    closes = [c for c in closes if c is not None]
    if len(closes) < 2:
        raise ValueError("종가 데이터 부족")
    last, prev = closes[-1], closes[-2]
    chg = (last / prev - 1) * 100 if prev else 0.0
    spark = closes[-8:]
    return last, chg, [rnd(v, 2) for v in spark]

def kospi_naver():
    """네이버 모바일 API 기반 코스피 지수 수집 (8일 종가 포함)."""
    d = http_json("https://m.stock.naver.com/api/index/KOSPI/price?pageSize=8")
    d.reverse()
    closes = []
    for item in d:
        closes.append(float(item["closePrice"].replace(",", "")))
    last = closes[-1]
    chg = float(d[-1]["fluctuationsRatio"])
    if d[-1].get("compareToPreviousPrice", {}).get("code") == "5":
        chg = -chg
    return last, chg, [rnd(v, 2) for v in closes]

def stooq_chart(symbol, days=30):
    """Stooq 일별 CSV (키 불필요). 지수는 ^kospi, ETF는 xlk.us, 선물은 dx.f 형식."""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    url = (f"https://stooq.com/q/d/l/?s={symbol}"
           f"&d1={start.strftime('%Y%m%d')}&d2={end.strftime('%Y%m%d')}&i=d")
    body = http_get(url)
    closes = []
    for r in csv.reader(io.StringIO(body)):
        if len(r) >= 5 and r[0] != "Date" and r[4] not in ("", "N/D"):
            try:
                closes.append(float(r[4]))
            except ValueError:
                pass
                pass
    return closes_to_metrics(closes)

def fred_chart(series_id, days=40):
    return closes_to_metrics([v for _, v in fred_csv(series_id, days)])

def chart_any(label, chain):
    """[(소스명, 함수), ...] 를 순서대로 시도해 첫 성공을 반환."""
    errors = []
    for src, fn in chain:
        try:
            return fn()
        except Exception as e:
            errors.append(f"{src}: {e}")
            print(f"  [warn] {label} ({src}) 시도 실패: {e}", file=sys.stderr)
    raise RuntimeError(f"{label}: 모든 소스 실패 ({', '.join(errors)})")

def btc_chart_upbit(usdkrw):
    """업비트 일봉 캔들 API 기반 BTC/USD 수집 (8일 스파크라인 포함)."""
    candles = http_json("https://api.upbit.com/v1/candles/days?market=KRW-BTC&count=8")
    # 최신순에서 과거순으로 오므로 reverse하여 과거순 -> 최신순으로 정렬
    candles.reverse()
    
    closes_usd = []
    for c in candles:
        closes_usd.append(c["trade_price"] / usdkrw)
        
    last_candle = candles[-1]
    last_usd = last_candle["trade_price"] / usdkrw
    chg = last_candle["change_rate"] * 100
    if last_candle.get("change") == "FALL":
        chg = -chg
        
    spark = [rnd(p, 0) for p in closes_usd]
    return last_usd, chg, spark

def fx_er_api():
    """ExchangeRate-API USD/KRW 최신 가격 (폴백/단일조회용)."""
    d = http_json("https://open.er-api.com/v6/latest/USD")
    krw = d["rates"]["KRW"]
    return krw, 0.0, [rnd(krw, 2)] * 8

def fx_chart():
    """Frankfurter(ECB) USD/KRW 최근 2주 시계열."""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=16)
    d = http_json(f"https://api.frankfurter.app/{start}..{end}?from=USD&to=KRW")
    closes = [v["KRW"] for _, v in sorted(d["rates"].items())]
    return closes_to_metrics(closes)

def fetch_cnn_fg():
    d = http_json("https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
                  headers={"Referer": "https://edition.cnn.com/markets/fear-and-greed",
                           "Origin": "https://edition.cnn.com",
                           "Accept": "application/json"})
    f = d["fear_and_greed"]
    return {
        "now":  rnd(f["score"], 0),         "prev": rnd(f["previous_close"], 0),
        "w1":   rnd(f["previous_1_week"], 0), "m1": rnd(f["previous_1_month"], 0),
        "y1":   rnd(f["previous_1_year"], 0),
    }

def fetch_crypto_fg():
    d = http_json("https://api.alternative.me/fng/?limit=366")
    vals = [int(x["value"]) for x in d["data"]]
    pick = lambda i: vals[i] if i < len(vals) else vals[-1]
    return {"now": pick(0), "prev": pick(1), "w1": pick(7), "m1": pick(30), "y1": pick(365)}

def fetch_usdkrw():
    return chart_any("USD/KRW", [
        ("ER-API", fx_er_api),
        ("Frankfurter", fx_chart),
        ("FRED", lambda: fred_chart("DEXKOUS")),
        ("Yahoo", lambda: yahoo_chart("USDKRW=X")),
    ])

def fetch_buffett():
    if BUFFETT_OVERRIDE is not None:
        return float(BUFFETT_OVERRIDE)
    w5000, _, _ = yahoo_chart("^FTW5000")     # Wilshire 5000 Full Cap (포인트 ≈ $십억)
    gdp = fred_last("GDP", days=400)          # 명목 GDP ($십억, 분기)
    return w5000 / gdp * 100

def fetch_yields():
    series = ["DGS1MO", "DGS3MO", "DGS6MO", "DGS1", "DGS2", "DGS5", "DGS10", "DGS30"]
    today, m1 = [], []
    for sid in series:
        rows = fred_csv(sid, days=55)
        today.append(rnd(rows[-1][1], 2))
        m1.append(rnd(rows[max(0, len(rows) - 22)][1], 2))  # 약 1개월(21영업일) 전
    return today, m1

def fetch_hy():
    rows = fred_csv("BAMLH0A0HYM2", days=55)
    now = rows[-1][1]
    prev = rows[-2][1] if len(rows) > 1 else now
    m1 = rows[max(0, len(rows) - 22)][1]
    return rnd(now, 2), rnd(prev, 2), rnd(m1, 2)

def fetch_us_econ():
    """미국 경제지표 4종 → econ 행 생성."""
    rows = []
    # 기준금리 (목표 상단)
    ff = safe("FEDFUNDS", lambda: fred_csv("DFEDTARU", days=120))
    if ff:
        cur, old = ff[-1][1], ff[0][1]
        d = cur - old
        delta = "— 동결" if abs(d) < 0.01 else (f"▲ +{d:.2f}%p" if d > 0 else f"▼ {d:.2f}%p")
        tone = "flat" if abs(d) < 0.01 else ("bad" if d > 0 else "good")
        rows.append({"flag": "US", "name": "기준금리", "val": f"{cur - 0.25:.2f}".rstrip("0").rstrip("."),
                     "suf": "%", "delta": delta, "tone": tone, "cat": "통화정책"})
    # CPI YoY
    cpi = safe("CPIAUCSL", lambda: fred_csv("CPIAUCSL", days=430))
    if cpi and len(cpi) >= 14:
        yoy_now = (cpi[-1][1] / cpi[-13][1] - 1) * 100
        yoy_prev = (cpi[-2][1] / cpi[-14][1] - 1) * 100
        d = yoy_now - yoy_prev
        rows.append({"flag": "US", "name": "소비자물가 (CPI)", "val": f"{yoy_now:.1f}", "suf": "%",
                     "delta": (f"▲ +{d:.1f}%p" if d > 0.049 else f"▼ {d:.1f}%p" if d < -0.049 else "— 보합"),
                     "tone": "bad" if d > 0.049 else "good" if d < -0.049 else "flat", "cat": "인플레이션"})
    # 실업률
    un = safe("UNRATE", lambda: fred_csv("UNRATE", days=120))
    if un and len(un) >= 2:
        cur, prev = un[-1][1], un[-2][1]
        d = cur - prev
        rows.append({"flag": "US", "name": "실업률", "val": f"{cur:.1f}", "suf": "%",
                     "delta": (f"▲ +{d:.1f}%p" if d > 0.049 else f"▼ {d:.1f}%p" if d < -0.049 else "— 보합"),
                     "tone": "bad" if d > 0.049 else "good" if d < -0.049 else "flat", "cat": "고용"})
    # 연준 대차대조표 (백만$ → 조$)
    bs = safe("WALCL", lambda: fred_csv("WALCL", days=60))
    if bs and len(bs) >= 2:
        cur, prev = bs[-1][1] / 1e6, bs[-2][1] / 1e6
        d = cur - prev
        rows.append({"flag": "US", "name": "연준 대차대조표", "val": f"${cur:.1f}", "suf": "조",
                     "delta": (f"▲ +{d:.2f}조" if d > 0.004 else f"▼ {d:.2f}조" if d < -0.004 else "— 보합"),
                     "tone": "bad" if d > 0.004 else "good" if d < -0.004 else "flat", "cat": "유동성"})
    return rows

def fetch_kr_econ():
    """한국 기준금리(FRED/IMF) + CPI YoY(OECD SDMX) — 키 불필요, 전부 자동."""
    rows = []
    # 기준금리: IMF 'discount rate' = 한국은행 기준금리와 동일 (월별, 1~2개월 시차)
    kr = safe("KR base rate", lambda: fred_csv("INTDSRKRM193N", days=550))
    if kr:
        vals = [v for _, v in kr]
        cur = vals[-1]
        # 가장 최근의 '변경'을 찾아 delta 표시
        delta, tone = "— 동결", "flat"
        for i in range(len(vals) - 1, 0, -1):
            if abs(vals[i] - vals[i - 1]) > 1e-9:
                d = vals[i] - vals[i - 1]
                delta = f"▲ +{d:.2f}%p" if d > 0 else f"▼ {d:.2f}%p"
                tone = "bad" if d > 0 else "good"
                break
        rows.append({"flag": "KR", "name": "기준금리", "val": f"{cur:.2f}", "suf": "%",
                     "delta": delta, "tone": tone, "cat": "통화정책"})
    # CPI YoY: OECD SDMX (키 불필요)
    def _kr_cpi():
        start = (datetime.date.today() - datetime.timedelta(days=200)).strftime("%Y-%m")
        url = ("https://sdmx.oecd.org/public/rest/data/OECD.SDD.TPS,DSD_PRICES@DF_PRICES_ALL,1.0/"
               f"KOR.M.N.CPI.PA._T.N.GY?startPeriod={start}&dimensionAtObservation=AllDimensions&format=csvfile")
        rdr = csv.DictReader(io.StringIO(http_get(url)))
        obs = sorted((r["TIME_PERIOD"], float(r["OBS_VALUE"])) for r in rdr if r.get("OBS_VALUE"))
        return obs
    cpi = safe("KR CPI (OECD)", _kr_cpi)
    if cpi and len(cpi) >= 2:
        cur, prev = cpi[-1][1], cpi[-2][1]
        d = cur - prev
        rows.append({"flag": "KR", "name": "소비자물가 (CPI)", "val": f"{cur:.1f}", "suf": "%",
                     "delta": (f"▲ +{d:.1f}%p" if d > 0.049 else f"▼ {d:.1f}%p" if d < -0.049 else "— 보합"),
                     "tone": "bad" if d > 0.049 else "good" if d < -0.049 else "flat", "cat": "인플레이션"})
    return rows

MONTHS_EN = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
             "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
             "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
             "sep": 9, "oct": 10, "nov": 11, "dec": 12}

def fetch_fomc():
    """연준 공식 캘린더 페이지에서 FOMC 일정 자동 추출. 실패 시 FOMC_FALLBACK."""
    try:
        html = http_get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm", timeout=20)
        text = re.sub(r"<[^>]+>", " ", html)
        meetings = []
        # 연도 섹션별로 분할
        parts = re.split(r"(\d{4})\s+FOMC\s+Meetings", text)
        for i in range(1, len(parts) - 1, 2):
            year = int(parts[i])
            if year < datetime.date.today().year or year > datetime.date.today().year + 1:
                continue
            # "(Released ...)" 제거 후 월+일 패턴 추출
            chunk = re.sub(r"\(Released[^)]*\)", " ", parts[i + 1])
            pat = re.compile(r"([A-Z][a-z]+(?:\s*/\s*[A-Z][a-z]+)?)[^0-9A-Za-z]{1,8}(\d{1,2})\s*-\s*(\d{1,2})\*?")
            for m in pat.finditer(chunk):
                mon_raw, d1, d2 = m.group(1), int(m.group(2)), int(m.group(3))
                mons = [x.strip().lower() for x in mon_raw.split("/")]
                if mons[0] not in MONTHS_EN:
                    continue
                m1 = MONTHS_EN[mons[0]]
                m2 = MONTHS_EN[mons[-1]] if (len(mons) > 1 and mons[-1] in MONTHS_EN) else m1
                if d2 < d1 and m2 == m1:  # 월 경계인데 두 번째 월이 없으면 다음 달
                    m2 = m1 % 12 + 1
                try:
                    meetings.append((datetime.date(year, m1, d1).isoformat(),
                                     datetime.date(year, m2 if d2 >= d1 or m2 != m1 else m1, d2).isoformat()))
                except ValueError:
                    continue
        if meetings:
            return sorted(set(meetings))
    except Exception as e:
        print(f"  [warn] FOMC scrape: {e}", file=sys.stderr)
    return FOMC_FALLBACK

def build_calendar():
    """FOMC(자동 스크랩) + NFP(매월 첫 금요일 자동 생성) → 미래 일정만."""
    today = datetime.date.today()
    ev = []
    for d1, d2 in fetch_fomc():
        ev.append({"date": d1, "name": "FOMC 회의 1일차", "sub": "미국 연준 통화정책회의", "cat": "US", "imp": 3})
        ev.append({"date": d2, "name": "FOMC 기준금리 발표", "sub": "한국시간 다음날 새벽 3시", "cat": "US", "imp": 3})
    # NFP: 향후 3개월치 첫 금요일
    y, m = today.year, today.month
    for _ in range(3):
        d = datetime.date(y, m, 1)
        d += datetime.timedelta(days=(4 - d.weekday()) % 7)  # 첫 금요일
        if d >= today:
            ev.append({"date": d.isoformat(), "name": "미국 고용보고서 (NFP)",
                       "sub": "비농업 고용·실업률 발표", "cat": "US", "imp": 3})
        m += 1
        if m > 12: m, y = 1, y + 1
    ev = [e for e in ev if e["date"] >= (today - datetime.timedelta(days=1)).isoformat()]
    ev.sort(key=lambda e: e["date"])
    return ev[:8]

# ════════════════════════════════════════════════════════════════
# ④ DK_DATA 조립
# ════════════════════════════════════════════════════════════════

SECTOR_ETFS = [
    ("XLK", "기술 (Tech)", 32), ("XLF", "금융", 13), ("XLV", "헬스케어", 11),
    ("XLY", "임의소비재", 10), ("XLC", "통신 서비스", 9), ("XLI", "산업재", 8),
    ("XLP", "필수소비재", 6), ("XLE", "에너지", 3.5), ("XLU", "유틸리티", 2.5),
    ("XLRE", "부동산", 2), ("XLB", "소재", 2),
]

def read_old_data(html):
    m = re.search(r"/\*DK_DATA_START\*/\s*const DK_DATA = (\{.*?\});\s*/\*DK_DATA_END\*/", html, re.S)
    if not m:
        return None
    return json.loads(m.group(1))

def build_dk_data(old):
    old = old or {}
    now_kst = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=9)
    data = {"updated": now_kst.strftime("%Y.%m.%d %H:%M KST")}

    # ── Yahoo Finance 멀티 쿼리 프리페치 (429 차단 및 반복 지연 해소) ──
    yahoo_symbols = [
        "^GSPC", "^IXIC", "^KS11", "GC=F", "DX-Y.NYB", "CL=F", "^VIX", "^FTW5000",
        "XLK", "XLF", "XLV", "XLY", "XLC", "XLI", "XLP", "XLE", "XLU", "XLRE", "XLB"
    ]
    safe("Yahoo Spark 프리페치", lambda: prefetch_yahoo_spark(yahoo_symbols))

    # ── USD/KRW 환율 선조회 (비트코인 달러 환산 및 기타 공통 계산에 필요) ──
    usdkrw_metrics = safe("USD/KRW 환율 선조회", fetch_usdkrw)
    usdkrw_val = usdkrw_metrics[0] if usdkrw_metrics else 1350.0

    # ── 자산 8종 — 항목별 소스 체인 (앞에서부터 시도) ──
    spec = [
        ("S&P 500",   "SP",  "#4dd0ff", "",  "",   2, [
            ("FRED",  lambda: fred_chart("SP500")),
            ("Yahoo", lambda: yahoo_chart("^GSPC")),
            ("Stooq", lambda: stooq_chart("^spx"))]),
        ("나스닥",     "NQ",  "#b58cff", "",  "",   2, [
            ("FRED",  lambda: fred_chart("NASDAQCOM")),
            ("Yahoo", lambda: yahoo_chart("^IXIC"))]),
        ("코스피",     "KS",  "#ff7d92", "",  "",   2, [
            ("Naver", kospi_naver),
            ("Yahoo", lambda: yahoo_chart("^KS11")),
            ("Stooq", lambda: stooq_chart("^kospi"))]),
        ("비트코인",   "₿",   "#ffb84d", "$", "",   0, None),  # 업비트 API 기반 별도 수집
        ("금 (Gold)",  "Au",  "#ffd966", "$", "",   2, [
            ("Yahoo", lambda: yahoo_chart("GC=F")),
            ("Stooq", lambda: stooq_chart("xauusd"))]),
        ("원/달러",    "₩",   "#00ffa3", "",  " ₩", 2, None),  # 선조회된 환율 사용
        ("달러인덱스", "DX",  "#9aa5c4", "",  "",   2, [
            ("Yahoo", lambda: yahoo_chart("DX-Y.NYB")),
            ("Stooq", lambda: stooq_chart("dx.f"))]),
        ("WTI 유가",   "OIL", "#7c5cff", "$", "",   2, [
            ("FRED",  lambda: fred_chart("DCOILWTICO")),
            ("Yahoo", lambda: yahoo_chart("CL=F")),
            ("Stooq", lambda: stooq_chart("cl.f"))]),
    ]

    assets, btc_usd, usdkrw = [], None, None
    old_assets = {a["name"]: a for a in old.get("assets", [])}
    for name, icon, col, pre, suf, dec, chain in spec:
        if name == "비트코인":
            r = safe("업비트 BTC", lambda: btc_chart_upbit(usdkrw_val))
            price, chg, spark = r if r else (None, None, None)
        elif name == "원/달러":
            price, chg, spark = usdkrw_metrics if usdkrw_metrics else (None, None, None)
        else:
            r = safe(name, lambda c=chain: chart_any(name, c))
            price, chg, spark = r if r else (None, None, None)
        if price is None:  # 실패 시 이전 값 유지
            prev = old_assets.get(name)
            if prev:
                assets.append(prev); continue
            price, chg, spark = 0, 0, [0] * 8
        if name == "비트코인": btc_usd = price
        if name == "원/달러": usdkrw = price
        assets.append({"name": name, "icon": icon, "col": col,
                       "price": rnd(price, dec), "chg": rnd(chg, 2),
                       "pre": pre, "suf": suf, "dec": dec, "spark": spark})
    data["assets"] = assets

    # ── 공포·탐욕 ──
    data["fg"] = {
        "stock":  safe("CNN F&G", fetch_cnn_fg) or old.get("fg", {}).get("stock"),
        "crypto": safe("crypto F&G", fetch_crypto_fg) or old.get("fg", {}).get("crypto"),
    }

    # ── 버핏 지표 (Yahoo 실패 시: 직전 값 × S&P500 등락률로 추정) ──
    bf = safe("buffett", fetch_buffett)
    old_bf = old.get("buffett", {}).get("now")
    if bf is None and old_bf:
        spx = next((a for a in assets if a["name"] == "S&P 500"), None)
        if spx:
            bf = old_bf * (1 + spx["chg"] / 100)
            print("  [info] 버핏 지표: 직전 값 × S&P500 등락률로 추정", file=sys.stderr)
    data["buffett"] = {"now": rnd(bf, 0) if bf else old_bf, "prev": old_bf or (rnd(bf, 0) if bf else 0)}

    # ── VIX ──
    vix = safe("VIX", lambda: chart_any("VIX", [
        ("FRED",  lambda: fred_chart("VIXCLS")),
        ("Yahoo", lambda: yahoo_chart("^VIX"))]))
    if vix:
        last, chg, spark = vix
        prev = spark[-2] if len(spark) > 1 else last
        data["vix"] = {"now": rnd(last, 1), "prev": rnd(prev, 1)}
    else:
        data["vix"] = old.get("vix", {"now": 0, "prev": 0})

    # ── 수익률 곡선 ──
    yl = safe("yields", fetch_yields)
    data["yields"] = ({"labels": ["1M", "3M", "6M", "1Y", "2Y", "5Y", "10Y", "30Y"],
                       "today": yl[0], "m1": yl[1]} if yl else old.get("yields"))

    # ── 하이일드 ──
    hy = safe("HY spread", fetch_hy)
    data["hy"] = ({"now": hy[0], "prev": hy[1], "m1": hy[2]} if hy else old.get("hy"))

    # ── 김치 프리미엄 ──
    upbit = safe("upbit", lambda: http_json("https://api.upbit.com/v1/ticker?markets=KRW-BTC")[0]["trade_price"])
    old_k = old.get("kimchi", {})
    prev_kp = None
    if old_k.get("upbit") and old_k.get("btc_usd") and old_k.get("usdkrw"):
        prev_kp = rnd((old_k["upbit"] / (old_k["btc_usd"] * old_k["usdkrw"]) - 1) * 100, 2)
    data["kimchi"] = {
        "upbit": rnd(upbit, 0) if upbit else old_k.get("upbit", 0),
        "btc_usd": rnd(btc_usd, 0) if btc_usd else old_k.get("btc_usd", 0),
        "usdkrw": rnd(usdkrw, 2) if usdkrw else old_k.get("usdkrw", 0),
        "prev": prev_kp if prev_kp is not None else old_k.get("prev", 0),
    }

    # ── 섹터 (Stooq ETF → Yahoo 폴백) ──
    sectors = []
    for etf, name, w in SECTOR_ETFS:
        r = safe(f"sector {etf}", lambda s=etf: chart_any(s, [
            ("Yahoo", lambda: yahoo_chart(s, rng="5d")),
            ("Stooq", lambda: stooq_chart(f"{s.lower()}.us", days=14))]))
        pct = rnd(r[1], 2) if r else next((x["pct"] for x in old.get("sectors", []) if x["name"] == name), 0)
        sectors.append({"name": name, "pct": pct, "w": w})
    data["sectors"] = sectors

    # ── 경제지표 표 (미국 4종 + 한국 2종, 전부 자동) ──
    econ = fetch_us_econ() + fetch_kr_econ()
    # 실패한 항목은 이전 값으로 보충
    have = {(r["flag"], r["name"]) for r in econ}
    for r in old.get("econ", []):
        if (r["flag"], r["name"]) not in have:
            econ.append(r)
    order = {"통화정책": 0, "인플레이션": 1, "고용": 2, "유동성": 3}
    econ.sort(key=lambda r: (r["flag"] != "US", order.get(r["cat"], 9)))
    data["econ"] = econ

    # ── 캘린더 ──
    data["calendar"] = build_calendar()
    return data

# ════════════════════════════════════════════════════════════════
# ⑤ HTML 갱신
# ════════════════════════════════════════════════════════════════

def main():
    dry = "--dry" in sys.argv
    print("▶ 데이터 수집 중...")
    html = open(HTML_PATH, encoding="utf-8").read()
    old = read_old_data(html)
    data = build_dk_data(old)

    block = ("/*DK_DATA_START*/\n  const DK_DATA = "
             + json.dumps(data, ensure_ascii=False, indent=2).replace("\n", "\n  ")
             + ";\n  /*DK_DATA_END*/")

    if dry:
        print(block)
        return

    new_html, n = re.subn(r"/\*DK_DATA_START\*/.*?/\*DK_DATA_END\*/", lambda m: block, html, flags=re.S)
    if n != 1:
        print(f"✗ DK_DATA 마커를 찾지 못했습니다 (matches={n})"); sys.exit(1)
    open(HTML_PATH, "w", encoding="utf-8").write(new_html)
    print(f"✓ {os.path.basename(HTML_PATH)} 갱신 완료 — {data['updated']}")
    print("  → 티스토리 글의 HTML을 이 파일 내용으로 교체하면 끝입니다.")

if __name__ == "__main__":
    main()
