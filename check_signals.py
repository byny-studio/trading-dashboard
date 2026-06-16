"""
장 마감 후 포트폴리오 신호 점검 → 강한 신호가 있으면 Discord로 알림.
GitHub Actions(cron)에서 실행. 앱 로직(추세/반등/과열/시장필터)을 자체 재현.

환경변수:
  DISCORD_WEBHOOK  : 디스코드 웹훅 URL
  PORTFOLIO_JSON   : 보유종목 JSON 배열 문자열 (portfolio.json 내용)
"""
import os
import json
import warnings
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import requests
import FinanceDataReader as fdr


def add_indicators(df):
    df = df.copy()
    for n in (5, 20, 60, 120):
        df[f"MA{n}"] = df["Close"].rolling(n).mean()
    ma20 = df["Close"].rolling(20).mean()
    std20 = df["Close"].rolling(20).std()
    df["BB_Upper"] = ma20 + 2 * std20
    df["BB_Lower"] = ma20 - 2 * std20
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    df["RSI"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    df["VOL_MA20"] = df["Volume"].rolling(20).mean()
    return df


def trend_score(df):
    if df.empty or len(df) < 60:
        return 0
    last = df.iloc[-1]
    v = []
    mas = [last.get(f"MA{n}") for n in (5, 20, 60, 120)]
    if all(pd.notna(mas)):
        v.append(5 if mas[0] > mas[1] > mas[2] > mas[3] else 4 if mas[0] > mas[1] > mas[2]
                 else 3 if mas[0] > mas[1] else 0 if mas[0] < mas[1] < mas[2] < mas[3] else 2)
    else:
        v.append(2)
    m5, m20 = df["MA5"].iloc[-1], df["MA20"].iloc[-1]
    m5p, m20p = df["MA5"].iloc[-2], df["MA20"].iloc[-2]
    v.append(5 if (m5p <= m20p and m5 > m20) else 0 if (m5p >= m20p and m5 < m20) else 4 if m5 > m20 else 1)
    rsi = last.get("RSI")
    v.append((5 if 50 <= rsi < 70 else 3 if rsi >= 70 else 2 if 40 <= rsi < 50 else 0) if pd.notna(rsi) else 2)
    close = last["Close"]
    bu, bl = last.get("BB_Upper"), last.get("BB_Lower")
    pos = (close - bl) / (bu - bl) if (pd.notna(bu) and bu != bl) else 0.5
    v.append(5 if 0.5 <= pos < 0.8 else 4 if pos >= 0.8 else 2 if 0.3 <= pos < 0.5 else 0)
    vma = last.get("VOL_MA20")
    r = last["Volume"] / vma if (pd.notna(vma) and vma > 0) else 1
    v.append(5 if r >= 2 else 4 if r >= 1.3 else 3 if r >= 0.8 else 1)
    return sum(v) * 4


def reversion_score(df):
    if df.empty or len(df) < 60:
        return 0
    last = df.iloc[-1]
    prev = df.iloc[-2]
    v = []
    rsi = last.get("RSI")
    v.append((5 if rsi < 30 else 4 if rsi < 40 else 2 if rsi < 50 else 0) if pd.notna(rsi) else 2)
    close = last["Close"]
    bu, bl = last.get("BB_Upper"), last.get("BB_Lower")
    pos = (close - bl) / (bu - bl) if (pd.notna(bu) and bu != bl) else 0.5
    v.append(5 if pos < 0.1 else 4 if pos < 0.25 else 2 if pos < 0.45 else 0)
    ma20 = last.get("MA20")
    gap = (close - ma20) / ma20 if (pd.notna(ma20) and ma20 > 0) else 0
    v.append(5 if gap <= -0.12 else 4 if gap <= -0.07 else 2 if gap <= -0.03 else 1)
    vma = last.get("VOL_MA20")
    r = last["Volume"] / vma if (pd.notna(vma) and vma > 0) else 1
    v.append(5 if r >= 1.5 else 3 if r >= 1.0 else 1)
    up = close > prev["Close"]
    cu = close > last.get("Open", close)
    v.append(5 if (up and cu) else 3 if up else 1)
    return sum(v) * 4


def overheat(df):
    if df.empty or len(df) < 6:
        return 0, []
    last, prev = df.iloc[-1], df.iloc[-2]
    close = float(last["Close"])
    rsi = last.get("RSI")
    bu = last.get("BB_Upper")
    chg1 = (close - prev["Close"]) / prev["Close"] * 100 if prev["Close"] else 0
    c5 = float(df["Close"].iloc[-6])
    chg5 = (close - c5) / c5 * 100 if c5 else 0
    vma = last.get("VOL_MA20")
    vr = last["Volume"] / vma if (pd.notna(vma) and vma > 0) else 0
    c_rsi = pd.notna(rsi) and rsi >= 70
    c_band = pd.notna(bu) and close >= bu
    c_s1, c_s5, c_vol = chg1 >= 8, chg5 >= 18, vr >= 2
    tags = [t for t, ok in [
        (f"RSI{rsi:.0f}" if pd.notna(rsi) else "RSI-", c_rsi),
        ("볼린저상단", c_band), (f"1일{chg1:+.0f}%", c_s1),
        (f"5일{chg5:+.0f}%", c_s5), (f"거래{vr:.1f}x", c_vol)] if ok]
    met = sum([c_rsi, c_band, c_s1, c_s5, c_vol])
    strong = (c_rsi and (c_band or c_s1)) or (c_s1 and (c_band or c_vol)) or met >= 3
    return (2 if strong else 1 if (c_band or c_s1 or met >= 2) else 0), tags


def market_bullish():
    try:
        df = fdr.DataReader("KS11", datetime.now() - timedelta(days=450), datetime.now())
        return float(df["Close"].iloc[-1]) >= float(df["Close"].rolling(200).mean().iloc[-1])
    except Exception:
        return True


def main():
    webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not webhook:
        print("DISCORD_WEBHOOK 미설정 — 종료")
        return
    try:
        portfolio = json.loads(os.environ.get("PORTFOLIO_JSON", "[]"))
    except Exception:
        portfolio = []
    if not portfolio:
        print("PORTFOLIO_JSON 비어있음 — 종료")
        return

    bull = market_bullish()
    end = datetime.now()
    start = end - timedelta(days=320)
    buy_trend, buy_rev, take_profit = [], [], []
    ref_date = ""

    for it in portfolio:
        code = str(it.get("code", "")).zfill(6)
        name = it.get("name", code)
        try:
            df = add_indicators(fdr.DataReader(code, start, end))
        except Exception:
            continue
        if df.empty or len(df) < 60:
            continue
        last_date = str(df.index[-1].date())
        if last_date > ref_date:
            ref_date = last_date
        price = int(df.iloc[-1]["Close"])
        t, r = trend_score(df), reversion_score(df)
        lvl, tags = overheat(df)
        if lvl == 2:
            take_profit.append(f"• **{name}** {price:,}원 — {' · '.join(tags)}")
        if bull and t >= 70:
            buy_trend.append(f"• **{name}** {price:,}원 (추세 {t})")
        if r >= 70:
            buy_rev.append(f"• **{name}** {price:,}원 (반등 {r})")

    if not (buy_trend or buy_rev or take_profit):
        print("강한 신호 없음 — 알림 생략")
        return

    lines = [f"📊 **트레이딩 신호 알림** (장 시작 전 · {ref_date} 종가 기준)",
             f"시장: {'📈 상승추세' if bull else '📉 하락추세(추세매수 보류)'}"]
    if take_profit:
        lines += ["", "🔥 **과열 — 시초가 일부 익절 고려**"] + take_profit
    if buy_trend:
        lines += ["", "📈 **추세 매수**"] + buy_trend
    if buy_rev:
        lines += ["", "🔄 **반등 매수**"] + buy_rev
    lines += ["", "_참고용 신호입니다. 매매 결정은 본인 판단._"]

    msg = "\n".join(lines)[:1900]
    resp = requests.post(webhook, json={"content": msg}, timeout=15)
    print("Discord 발송:", resp.status_code)


if __name__ == "__main__":
    main()
