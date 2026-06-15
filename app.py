"""
개인용 주식 매매 분석 대시보드
실행: streamlit run app.py
"""
import json
import os
from datetime import datetime, timedelta

import re
import urllib.parse
import xml.etree.ElementTree as ET

import FinanceDataReader as fdr
import trafilatura
import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from bs4 import BeautifulSoup
from plotly.subplots import make_subplots
from stock_screener import render_screener
from candle_analyzer import analyze_candle

# ===== 페이지 설정 =====
st.set_page_config(
    page_title="적게 일하고 많이 벌기 💵",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)
from design_system import inject_css
inject_css()


# ===== 접속 비밀번호 잠금 =====
def check_password() -> bool:
    """앱 전체 비밀번호 잠금. secrets.toml의 app_password와 비교."""
    try:
        correct = st.secrets["app_password"]
    except Exception:
        correct = None

    # 비밀번호 미설정 시 잠금 없이 통과(설정 안내만 표시)
    if not correct:
        st.warning(
            "🔓 비밀번호가 설정되지 않았습니다. "
            "`.streamlit/secrets.toml`의 `app_password` 값을 설정하면 잠금이 활성화됩니다."
        )
        return True

    if st.session_state.get("auth_ok"):
        return True

    st.title("🔒 로그인")
    with st.form("login_form"):
        pw = st.text_input(
            "비밀번호",
            type="password",
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("입력", type="primary", use_container_width=True)
        if submitted:
            if pw == correct:
                st.session_state["auth_ok"] = True
                st.rerun()
            else:
                st.error("비밀번호가 틀렸습니다.")
    return False


if not check_password():
    st.stop()

PORTFOLIO_FILE = "portfolio.json"
HISTORY_FILE = "history.json"
HISTORY_LIMIT = 500


# ===== 데이터 로딩 =====
@st.cache_data(ttl=600)
def load_stock_data(code: str, days: int = 200) -> pd.DataFrame:
    """종목 코드로 주가 데이터 가져오기 (10분 캐시)."""
    end = datetime.now()
    start = end - timedelta(days=days)
    try:
        df = fdr.DataReader(code, start, end)
    except Exception:
        # 잘못된 코드/네트워크 오류 등으로 조회 실패 시 페이지 전체가 죽지 않도록 빈 결과 반환
        return pd.DataFrame()
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    return df


@st.cache_data(ttl=86400)
def get_stock_listing() -> pd.DataFrame:
    """KRX 전체 종목 + ETF 리스트 (1일 캐시)."""
    frames = []
    for market in ("KRX", "ETF/KR"):
        try:
            df = fdr.StockListing(market)
            if "Code" not in df.columns and "Symbol" in df.columns:
                df = df.rename(columns={"Symbol": "Code"})
            df = df[["Code", "Name"]].dropna()
            df["Code"] = df["Code"].astype(str).str.zfill(6)
            frames.append(df)
        except Exception:
            continue
    if not frames:
        return pd.DataFrame(columns=["Code", "Name"])
    merged = pd.concat(frames, ignore_index=True)
    merged = merged.drop_duplicates(subset="Code")
    return merged.reset_index(drop=True)


def resolve_stock(query: str):
    """코드나 이름으로 종목 조회. (code, name, found) 반환."""
    q = (query or "").strip()
    if not q:
        return "", "", False
    listing = get_stock_listing()
    if listing.empty:
        return q, q, False
    # 1. 코드 정확 일치 (앞자리 0 보정)
    code_q = q.zfill(6) if q.isdigit() else q
    match = listing[listing["Code"] == code_q]
    if not match.empty:
        return match.iloc[0]["Code"], match.iloc[0]["Name"], True
    # 2. 이름 정확 일치
    match = listing[listing["Name"] == q]
    if not match.empty:
        return match.iloc[0]["Code"], match.iloc[0]["Name"], True
    # 3. 이름 부분 일치
    match = listing[listing["Name"].str.contains(q, na=False, case=False)]
    if not match.empty:
        return match.iloc[0]["Code"], match.iloc[0]["Name"], True
    return q, q, False


def format_stock(name: str, code: str) -> str:
    """모든 메뉴 공통 표기: '이름(코드)' 형식."""
    if not name or name == code:
        return code or "-"
    return f"{name}({code})"


@st.cache_data(ttl=1800)
def get_market_news(n: int = 3) -> list:
    """주가 흐름에 영향을 주는 거시·시장 주요 뉴스 top N (Google News RSS, 30분 캐시)."""
    query = "코스피 OR 증시 OR 금리 OR 환율 OR 전쟁 OR 유가 when:2d"
    url = (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(query)
        + "&hl=ko&gl=KR&ceid=KR:ko"
    )
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception:
        return []

    news, seen = [], set()
    for item in root.findall(".//item"):
        raw = (item.findtext("title") or "").strip()
        if not raw:
            continue
        # 제목 끝의 " - 언론사" 분리
        if " - " in raw:
            title, source = raw.rsplit(" - ", 1)
        else:
            title, source = raw, ""
        title = title.strip()
        # 제목 끝에 언론사명이 중복으로 붙는 경우 정리
        while source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        # 유사 중복 이슈 제거 (제목 앞부분 기준)
        key = title[:12]
        if key in seen:
            continue
        seen.add(key)
        news.append({
            "title": title,
            "source": source.strip(),
            "link": (item.findtext("link") or "").strip(),
        })
        if len(news) >= n:
            break
    return news


def is_mobile() -> bool:
    """접속 기기가 모바일인지 User-Agent로 판별 (판별 불가 시 False)."""
    try:
        ua = (st.context.headers.get("User-Agent", "") or "").lower()
    except Exception:
        return False
    return any(k in ua for k in ("iphone", "android", "ipad", "ipod", "mobile"))


# ===== 펀더멘털 (PER/PBR/배당률) =====
def _to_num(text):
    if text is None:
        return None
    s = re.sub(r"[^\d.\-]", "", text)
    if not s or s in {".", "-"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


@st.cache_data(ttl=86400)
def get_fundamentals(code: str) -> dict:
    """네이버 금융에서 PER/PBR/EPS/배당수익률/동일업종 PER 스크레이핑 (1일 캐시)."""
    if not code:
        return {}
    try:
        url = f"https://finance.naver.com/item/main.nhn?code={code}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if r.status_code != 200:
            return {}
        soup = BeautifulSoup(r.text, "html.parser")
        result = {}
        for key, el_id in [("PER", "_per"), ("PBR", "_pbr"), ("EPS", "_eps"), ("DIV", "_dvr")]:
            el = soup.select_one(f"#{el_id}")
            result[key] = _to_num(el.text if el else None)

        # 동일업종 PER (caption 기반)
        ind_table = soup.find("table", summary="동일업종 PER 정보")
        if ind_table:
            em = ind_table.select_one("tr.strong td em")
            result["INDUSTRY_PER"] = _to_num(em.text if em else None)
        return result
    except Exception:
        return {}


def format_fundamentals_line(data: dict) -> str:
    """펀더멘털을 한 줄 요약으로 포맷. 업종 PER 비교 포함."""
    if not data:
        return "펀더멘털 정보 없음"
    parts = []
    per = data.get("PER")
    ind_per = data.get("INDUSTRY_PER")
    if per is not None and per > 0:
        per_text = f"**PER** {per:.2f}"
        if ind_per is not None and ind_per > 0:
            ratio = per / ind_per
            if ratio < 0.8:
                tag = "🟢 저평가"
            elif ratio > 1.3:
                tag = "🔴 고평가"
            else:
                tag = "🟡 평균권"
            per_text += f" (업종 {ind_per:.2f} · {tag})"
        parts.append(per_text)
    if data.get("PBR") is not None and data["PBR"] > 0:
        parts.append(f"**PBR** {data['PBR']:.2f}")
    if data.get("DIV") is not None:
        parts.append(f"**배당률** {data['DIV']:.2f}%")
    if data.get("EPS") is not None:
        parts.append(f"**EPS** {int(data['EPS']):,}")
    return "  ·  ".join(parts) if parts else "펀더멘털 정보 없음"


# ===== 기술적 지표 계산 =====
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """이평선, RSI, 볼린저밴드, 거래량 이평 추가."""
    if df.empty:
        return df
    df = df.copy()

    # 이동평균선
    for n in (5, 20, 60, 120):
        df[f"MA{n}"] = df["Close"].rolling(n).mean()

    # 볼린저 밴드 (20일 기준)
    ma20 = df["Close"].rolling(20).mean()
    std20 = df["Close"].rolling(20).std()
    df["BB_Upper"] = ma20 + 2 * std20
    df["BB_Lower"] = ma20 - 2 * std20

    # RSI (14일)
    delta = df["Close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["RSI"] = 100 - (100 / (1 + rs))

    # 거래량 이동평균
    df["VOL_MA20"] = df["Volume"].rolling(20).mean()

    return df


# ===== 신호 점수 계산 =====
def score_signal(df: pd.DataFrame) -> dict:
    """5가지 항목을 5점씩, 총 25점 만점으로 점수화."""
    if df.empty or len(df) < 60:
        return {"total": 0, "details": {}, "verdict": "데이터 부족"}

    last = df.iloc[-1]
    details = {}

    # 1. 이평선 정배열 (MA5 > MA20 > MA60 > MA120)
    mas = [last.get(f"MA{n}") for n in (5, 20, 60, 120)]
    if all(pd.notna(mas)):
        if mas[0] > mas[1] > mas[2] > mas[3]:
            details["이평선"] = (5, "완벽한 정배열")
        elif mas[0] > mas[1] > mas[2]:
            details["이평선"] = (4, "단·중기 정배열")
        elif mas[0] > mas[1]:
            details["이평선"] = (3, "단기 상승")
        elif mas[0] < mas[1] < mas[2] < mas[3]:
            details["이평선"] = (0, "완벽한 역배열")
        else:
            details["이평선"] = (2, "혼조")
    else:
        details["이평선"] = (2, "데이터 부족")

    # 2. 골든/데드 크로스 (MA5 vs MA20)
    if len(df) >= 2 and pd.notna(df["MA5"].iloc[-1]) and pd.notna(df["MA20"].iloc[-1]):
        ma5_now, ma20_now = df["MA5"].iloc[-1], df["MA20"].iloc[-1]
        ma5_prev, ma20_prev = df["MA5"].iloc[-2], df["MA20"].iloc[-2]
        if ma5_prev <= ma20_prev and ma5_now > ma20_now:
            details["크로스"] = (5, "골든크로스 발생")
        elif ma5_prev >= ma20_prev and ma5_now < ma20_now:
            details["크로스"] = (0, "데드크로스 발생")
        elif ma5_now > ma20_now:
            details["크로스"] = (4, "5일선이 20일선 위")
        else:
            details["크로스"] = (1, "5일선이 20일선 아래")
    else:
        details["크로스"] = (2, "데이터 부족")

    # 3. RSI
    rsi = last.get("RSI")
    if pd.notna(rsi):
        if rsi < 30:
            details["RSI"] = (5, f"과매도 {rsi:.1f}")
        elif rsi < 50:
            details["RSI"] = (3, f"조정 영역 {rsi:.1f}")
        elif rsi < 70:
            details["RSI"] = (4, f"건강한 상승 {rsi:.1f}")
        else:
            details["RSI"] = (1, f"과매수 {rsi:.1f}")
    else:
        details["RSI"] = (2, "데이터 부족")

    # 4. 볼린저 밴드 위치
    close = last["Close"]
    bb_u, bb_l = last.get("BB_Upper"), last.get("BB_Lower")
    if pd.notna(bb_u) and pd.notna(bb_l):
        bb_pos = (close - bb_l) / (bb_u - bb_l) if bb_u != bb_l else 0.5
        if bb_pos < 0.2:
            details["볼린저"] = (5, "하단 근처(반등 기대)")
        elif bb_pos < 0.5:
            details["볼린저"] = (3, "중심선 아래")
        elif bb_pos < 0.8:
            details["볼린저"] = (4, "중심선 위")
        else:
            details["볼린저"] = (1, "상단 근처(과열)")
    else:
        details["볼린저"] = (2, "데이터 부족")

    # 5. 거래량
    vol = last["Volume"]
    vol_ma = last.get("VOL_MA20")
    if pd.notna(vol_ma) and vol_ma > 0:
        ratio = vol / vol_ma
        if ratio >= 2.0:
            details["거래량"] = (5, f"폭증 ({ratio:.1f}배)")
        elif ratio >= 1.3:
            details["거래량"] = (4, f"증가 ({ratio:.1f}배)")
        elif ratio >= 0.8:
            details["거래량"] = (3, f"평균 ({ratio:.1f}배)")
        else:
            details["거래량"] = (1, f"감소 ({ratio:.1f}배)")
    else:
        details["거래량"] = (2, "데이터 부족")

    # 5개 항목 × 5점 = 25점 → 100점 만점으로 환산
    total = sum(v[0] for v in details.values()) * 4

    # 판정 (100점 만점 기준)
    if total >= 80:
        verdict = "🚀 강력 매수"
    elif total >= 60:
        verdict = "✅ 매수 관심"
    elif total >= 40:
        verdict = "🟡 관망"
    elif total >= 24:
        verdict = "⚠️ 매도 검토"
    else:
        verdict = "🔴 매도"

    return {"total": total, "details": details, "verdict": verdict}


# ===== 돌파 매수 신호 (포트폴리오용) =====
def breakout_signal(df: pd.DataFrame) -> dict:
    """돌파 매수 신호: ① 거래량 > 20일 평균 거래량 × 2 ② 종가 > 직전 20거래일 최고가.
    두 조건을 모두 충족하면 매수 신호 발생."""
    if df.empty or len(df) < 21:
        return {"fired": False, "conds": [], "text": "⚠️ 데이터 부족"}
    last = df.iloc[-1]
    close = float(last["Close"])
    vol = float(last["Volume"])
    vol_ma20 = float(df["Volume"].iloc[-20:].mean())
    # 오늘을 제외한 직전 20거래일 최고가 (신고가 돌파 판정)
    prior_high20 = float(df["High"].iloc[-21:-1].max())

    vol_ratio = vol / vol_ma20 if vol_ma20 > 0 else 0
    c_vol = vol_ratio >= 2.0
    c_high = close > prior_high20

    conds = [
        (c_vol, f"거래량 {vol_ratio:.1f}배 (기준 2배 이상)"),
        (c_high, f"종가 {int(close):,} {'>' if c_high else '≤'} 20일 최고 {int(prior_high20):,}"),
    ]
    fired = c_vol and c_high
    met = sum(1 for ok, _ in conds if ok)
    text = "🚀 매수 신호" if fired else f"⏸️ 관망 ({met}/2 충족)"
    return {"fired": fired, "conds": conds, "text": text, "met": met}


# ===== 매매 신호 기준 표 =====
VERDICT_TABLE = """
| 점수 (100점 만점) | 매매 신호 | 의미 |
|---|---|---|
| **80 이상** | 🚀 강력 매수 | 5개 지표 대부분이 매우 긍정적 |
| **60 ~ 79** | ✅ 매수 관심 | 매수 우호적, 분할매수 고려 |
| **40 ~ 59** | 🟡 관망 | 방향성 불명확, 추세 확인 후 결정 |
| **24 ~ 39** | ⚠️ 매도 검토 | 약세 신호, 손절 라인 점검 |
| **24 미만** | 🔴 매도 | 대부분의 지표가 부정적 |

*점수는 **이평선 · 크로스 · RSI · 볼린저밴드 · 거래량** 5개 항목을 각 5점씩 평가한 뒤 100점 만점으로 환산한 값입니다.*
"""


# ===== 백테스트 =====
def compute_daily_scores(df_full: pd.DataFrame) -> list:
    """일별 (date, price, score) 리스트. 한 번만 계산하고 여러 시뮬에 재활용."""
    if df_full is None or df_full.empty or len(df_full) < 80:
        return []
    df_ind = add_indicators(df_full)
    out = []
    for i in range(60, len(df_ind)):
        window = df_ind.iloc[: i + 1]
        result = score_signal(window)
        out.append({
            "date": window.index[-1],
            "price": float(window.iloc[-1]["Close"]),
            "score": result["total"],
        })
    return out


def simulate_trades(
    daily_scores: list,
    buy_threshold: int,
    sell_threshold: int,
    initial_capital: float = 1_000_000,
) -> dict:
    """미리 계산된 일별 점수로 매수/매도 시뮬레이션."""
    if not daily_scores:
        return {}
    cash = initial_capital
    shares = 0.0
    holding = False
    entry_price = 0.0
    entry_date = None
    trades = []
    equity = []
    prev_score = None

    for d in daily_scores:
        score = d["score"]
        price = d["price"]
        date = d["date"]
        if (
            not holding
            and prev_score is not None
            and prev_score < buy_threshold
            and score >= buy_threshold
            and cash > 0
        ):
            shares = cash / price
            entry_price = price
            entry_date = date
            cash = 0.0
            holding = True
        elif (
            holding
            and prev_score is not None
            and prev_score > sell_threshold
            and score <= sell_threshold
        ):
            cash = shares * price
            trades.append({
                "entry_date": entry_date,
                "entry_price": entry_price,
                "exit_date": date,
                "exit_price": price,
                "profit_pct": (price - entry_price) / entry_price * 100,
            })
            shares = 0.0
            holding = False
        equity.append({"date": date, "value": cash + shares * price, "price": price, "score": score})
        prev_score = score

    final_value = cash + shares * equity[-1]["price"]
    first_price = equity[0]["price"]
    last_price = equity[-1]["price"]
    bh_value = initial_capital * (last_price / first_price) if first_price > 0 else initial_capital
    peak = equity[0]["value"]
    max_dd = 0.0
    for e in equity[1:]:
        if e["value"] > peak:
            peak = e["value"]
        dd = (e["value"] - peak) / peak * 100 if peak else 0
        if dd < max_dd:
            max_dd = dd
    win_count = sum(1 for t in trades if t["profit_pct"] > 0)
    win_rate = (win_count / len(trades) * 100) if trades else 0.0

    return {
        "trades": trades,
        "equity": equity,
        "final_value": final_value,
        "bh_value": bh_value,
        "system_return_pct": (final_value - initial_capital) / initial_capital * 100,
        "bh_return_pct": (bh_value - initial_capital) / initial_capital * 100,
        "trade_count": len(trades),
        "win_rate": win_rate,
        "max_drawdown": max_dd,
        "initial_capital": initial_capital,
        "holding_at_end": holding,
    }


def sweep_thresholds(
    daily_scores: list,
    buy_range=(50, 55, 60, 65, 70, 75, 80),
    sell_range=(20, 25, 30, 35, 40, 45),
    initial_capital: float = 1_000_000,
) -> list:
    """모든 (매수, 매도) 조합 시뮬 → 결과 리스트. buy > sell 조건만."""
    results = []
    for b in buy_range:
        for s in sell_range:
            if b <= s:
                continue
            sim = simulate_trades(daily_scores, b, s, initial_capital)
            if sim:
                results.append({
                    "buy": b,
                    "sell": s,
                    "system_return": sim["system_return_pct"],
                    "bh_return": sim["bh_return_pct"],
                    "excess": sim["system_return_pct"] - sim["bh_return_pct"],
                    "trades": sim["trade_count"],
                    "win_rate": sim["win_rate"],
                    "mdd": sim["max_drawdown"],
                    "_sim": sim,
                })
    return results


# ===== 손절/익절 계산 =====
def calc_stop_levels(buy_price: float, current_price: float) -> dict:
    """손절가는 매수가 기준(원금 보호). 익절가는 매수가/현재가 중 높은 쪽 기준
    (수익 중이면 현재가에서 추가 상승 목표, 손실 중이면 매수가에서 회복 목표)."""
    profit_base = max(buy_price, current_price)
    return {
        "보수적 손절(-3%)": round(buy_price * 0.97),
        "일반 손절(-7%)": round(buy_price * 0.93),
        "최후 손절(-10%)": round(buy_price * 0.90),
        "1차 익절(+3%)": round(profit_base * 1.03),
        "2차 익절(+7%)": round(profit_base * 1.07),
        "3차 익절(+15%)": round(profit_base * 1.15),
    }


# ===== 매매 팁 (verdict + 지표 기반 + 뉴스 요약) =====
def _split_sentences(text: str) -> list:
    """한국어 문장 분리. 마침표/물음표/느낌표/'다.' 종결 기준."""
    if not text:
        return []
    parts = re.split(r"(?<=[.!?다요죠])\s+", text)
    return [p.strip() for p in parts if len(p.strip()) >= 15]


def _summarize_articles(articles: list) -> str:
    """가장 상위 기사 1건의 첫 문장만 80자 컷으로 한 줄 요약."""
    for art in articles:
        sents = _split_sentences(art.get("body", ""))
        if sents:
            s = sents[0]
            if len(s) > 80:
                s = s[:80].rsplit(" ", 1)[0] + "…"
            return s
    return ""


@st.cache_data(ttl=1800)
def get_news_summaries(query: str, limit: int = 4) -> dict:
    """네이버 뉴스 검색 → 상위 기사 본문 추출 → 알고리즘 요약 (30분 캐시).
    반환: {"summary": "...", "articles": [{"title", "url", "source"}, ...]}"""
    if not query:
        return {"summary": "", "articles": []}
    try:
        q = urllib.parse.quote(query)
        url = f"https://search.naver.com/search.naver?where=news&query={q}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
        if r.status_code != 200:
            return {"summary": "", "articles": []}
        soup = BeautifulSoup(r.text, "html.parser")
        all_a = soup.find_all("a")
        title_links = [a for a in all_a if a.find("span", class_="sds-comps-text-type-headline1")]

        articles = []
        seen_titles = set()
        for a in title_links:
            href = a.get("href", "")
            if not href.startswith("http"):
                continue
            title_el = a.find("span", class_="sds-comps-text-type-headline1")
            if not title_el:
                continue
            title = title_el.text.strip()
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            try:
                downloaded = trafilatura.fetch_url(href)
                body = trafilatura.extract(downloaded, include_comments=False, include_tables=False) if downloaded else None
            except Exception:
                body = None
            if body:
                # 출처 도메인
                m = re.search(r"https?://(?:www\.)?([^/]+)/", href)
                source = m.group(1) if m else ""
                articles.append({"title": title, "url": href, "source": source, "body": body})
            if len(articles) >= limit:
                break

        if not articles:
            return {"summary": "", "articles": []}

        summary = _summarize_articles(articles)
        # 본문 제거하고 메타만 반환
        return {
            "summary": summary,
            "articles": [{"title": a["title"], "url": a["url"], "source": a["source"]} for a in articles],
        }
    except Exception:
        return {"summary": "", "articles": []}


def _action_advice(verdict: str, ma_msg: str, cross_msg: str, rsi_msg: str, bb_msg: str) -> str:
    """5개 지표 조합으로 구체 액션 한 줄."""
    if "골든크로스" in cross_msg and "과매도" in rsi_msg:
        return "**저점 반등 시그널** — 분할 매수 1차 진입 후보"
    if "정배열" in ma_msg and "건강한 상승" in rsi_msg and "상단" not in bb_msg:
        return "**추세 동행 매수** — MA20 눌림목에서 추가 매수, 손절 라인 설정 필수"
    if "정배열" in ma_msg and ("과매수" in rsi_msg or "상단 근처" in bb_msg):
        return "**과열 구간** — 추격 자제, 부분 익절·트레일링 손절 권장"
    if "역배열" in ma_msg and "과매도" in rsi_msg:
        return "**낙폭과대 반등 후보** — 거래량 동반 양봉 확인 후 소량 진입"
    if "역배열" in ma_msg or "데드크로스" in cross_msg:
        return "**하락 추세** — 신규 매수 보류, 보유분 비중 축소"
    if "혼조" in ma_msg:
        return "**박스권 매매** — 방향 확정 전까진 관망, 5일선 안착 확인"
    if "강력 매수" in verdict:
        return "**적극 매수** — 분할 진입 + 손절가 설정"
    if "매수 관심" in verdict:
        return "**분할 매수** — 거래량 동반 양봉에서 1차 진입"
    if "관망" in verdict:
        return "**관망** — 5일선 안착·거래량 회복 시 진입"
    if "매도 검토" in verdict:
        return "**일부 정리** — 지지선 이탈 시 추가 매도"
    return "**매수 자제** — 추세 반전 신호 확인 후"


# 용어 풀이 사전 — 진단 라인에 등장하면 자동으로 설명이 따라붙음
TERM_GLOSSARY = [
    ("완벽 정배열", "5·20·60·120일선이 위→아래로 차곡차곡 (강한 상승 추세)"),
    ("단·중기 정배열", "단기·중기 이평선이 상승 정렬 (긍정적 추세)"),
    ("단기 상승", "5일선이 20일선 위 (단기 상승 흐름)"),
    ("완벽 역배열", "이평선이 거꾸로 정렬 (강한 하락)"),
    ("약세 흐름", "5일선이 20일선 아래 (단기 약세)"),
    ("혼조", "이평선이 엇갈려 방향 불명확"),
    ("골든크로스", "단기선이 장기선을 위로 뚫음 (매수 전환 신호)"),
    ("데드크로스", "단기선이 장기선을 아래로 뚫음 (매도 전환 신호)"),
    ("과매도", "RSI 30 미만, 너무 많이 빠져 단기 반등 가능"),
    ("과매수", "RSI 70 초과, 너무 올라 차익실현 압력"),
    ("BB 하단", "볼린저밴드 하단 — 단기 반등 자리"),
    ("BB 상단", "볼린저밴드 상단 — 단기 과열 신호"),
    ("BB 중심선 위", "20일선 위 + 변동성 안정 (상승 안정)"),
    ("BB 중심선 아래", "20일선 아래 (약세 흐름)"),
    ("거래량 폭증", "평균 대비 2배 이상 (변곡점 가능)"),
    ("거래량 증가", "평균 대비 1.3배↑ (관심 ↑)"),
    ("거래량 감소", "평균 대비 0.8배↓ (관심 ↓)"),
    ("MA20", "20일 이동평균선 — 중기 추세선"),
    ("MA60", "60일 이동평균선 — 중장기 추세선"),
    ("눌림목", "주가가 잠깐 조정받은 자리 (저가 매수 기회)"),
    ("손절", "손실 확대 방지를 위한 정리 매도"),
    ("익절", "이익 실현 매도"),
    ("트레일링 손절", "주가 상승에 따라 손절가도 같이 올림"),
    ("분할 매수", "한 번에 사지 않고 나눠서 매수"),
    ("박스권", "특정 가격대를 오르락내리락"),
    ("지지선", "더 빠지지 않게 받쳐주는 가격대"),
]


def _glossary_for(text: str) -> str:
    """문구에 등장한 용어만 골라 풀이 라인 생성."""
    found = []
    for term, expl in TERM_GLOSSARY:
        if term in text and not any(term in f[0] for f in found):
            found.append((term, expl))
        if len(found) >= 4:
            break
    if not found:
        return ""
    return " · ".join(f"**{t}** = {e}" for t, e in found)


def make_technical_tip(result: dict) -> tuple:
    """진단+행동 1줄과 용어 풀이 1줄을 함께 반환."""
    verdict = result.get("verdict", "")
    details = result.get("details", {})
    _, ma_msg = details.get("이평선", (0, ""))
    _, cross_msg = details.get("크로스", (0, ""))
    _, rsi_msg = details.get("RSI", (0, ""))
    _, bb_msg = details.get("볼린저", (0, ""))
    _, vol_msg = details.get("거래량", (0, ""))

    # 1) 추세 진단
    trend_bits = []
    if "완벽한 정배열" in ma_msg:
        trend_bits.append("완벽 정배열")
    elif "단·중기 정배열" in ma_msg:
        trend_bits.append("단·중기 정배열")
    elif "단기 상승" in ma_msg:
        trend_bits.append("단기 상승")
    elif "완벽한 역배열" in ma_msg:
        trend_bits.append("완벽 역배열")
    elif "역배열" in ma_msg or "5일선이 20일선 아래" in cross_msg:
        trend_bits.append("약세 흐름")
    elif "혼조" in ma_msg:
        trend_bits.append("혼조")

    if "골든크로스" in cross_msg:
        trend_bits.append("🟢 골든크로스")
    elif "데드크로스" in cross_msg:
        trend_bits.append("🔴 데드크로스")

    # 2) 모멘텀 진단
    mom_bits = []
    if rsi_msg:
        # 메시지 끝 숫자 추출 (예: "건강한 상승 56.2")
        m = re.search(r"([\d.]+)$", rsi_msg)
        rsi_val = m.group(1) if m else ""
        if "과매도" in rsi_msg:
            mom_bits.append(f"RSI {rsi_val} 과매도")
        elif "과매수" in rsi_msg:
            mom_bits.append(f"RSI {rsi_val} 과매수")
        elif "조정" in rsi_msg:
            mom_bits.append(f"RSI {rsi_val} 조정")
        elif "건강한 상승" in rsi_msg:
            mom_bits.append(f"RSI {rsi_val}")
    if "하단 근처" in bb_msg:
        mom_bits.append("BB 하단")
    elif "상단 근처" in bb_msg:
        mom_bits.append("BB 상단")
    elif "중심선 위" in bb_msg:
        mom_bits.append("BB 중심선 위")
    elif "중심선 아래" in bb_msg:
        mom_bits.append("BB 중심선 아래")

    # 3) 거래량
    if "폭증" in vol_msg:
        m = re.search(r"([\d.]+)배", vol_msg)
        v = m.group(1) if m else "?"
        mom_bits.append(f"거래량 폭증({v}배)")
    elif "증가" in vol_msg:
        mom_bits.append("거래량 증가")
    elif "감소" in vol_msg:
        mom_bits.append("거래량 감소")

    diagnosis = " · ".join(trend_bits + mom_bits) if (trend_bits or mom_bits) else "지표 미검출"
    action = _action_advice(verdict, ma_msg, cross_msg, rsi_msg, bb_msg)
    full_line = f"{diagnosis} → {action}"
    glossary_line = _glossary_for(full_line)
    return full_line, glossary_line


# ===== 매수 가격대 제안 =====
def suggest_buy_zones(df_ind: pd.DataFrame) -> list:
    """매수 관심 이상(60점↑)일 때 다단계 매수가 제안."""
    last = df_ind.iloc[-1]
    cur = float(last["Close"])
    ma20 = last.get("MA20")
    ma60 = last.get("MA60")
    bb_lower = last.get("BB_Lower")

    zones = [
        ("적극 매수 (시장가)", int(round(cur)), "추세를 따라 즉시 진입"),
        ("분할 매수 1차 (-2%)", int(round(cur * 0.98)), "가벼운 조정 활용"),
    ]
    if pd.notna(ma20) and ma20 < cur:
        zones.append(("지지선 매수 (MA20 부근)", int(round(ma20)), "20일 이평선까지 조정 시"))
    candidates = []
    if pd.notna(bb_lower) and bb_lower < cur:
        candidates.append(("BB 하단", int(round(bb_lower))))
    if pd.notna(ma60) and ma60 < cur:
        candidates.append(("MA60", int(round(ma60))))
    if candidates:
        label, price = max(candidates, key=lambda x: x[1])
        zones.append((f"저가 매수 ({label})", price, "강한 조정 시 저점 매집"))
    return zones


# ===== 차트 그리기 =====
def make_chart(df: pd.DataFrame, name: str) -> go.Figure:
    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.6, 0.2, 0.2],
        subplot_titles=(f"{name} 가격 + 이평선 + 볼린저밴드", "거래량", "RSI"),
    )

    # 캔들스틱
    fig.add_trace(go.Candlestick(
        x=df.index, open=df["Open"], high=df["High"],
        low=df["Low"], close=df["Close"], name="가격",
        increasing_line_color="#e74c3c", decreasing_line_color="#3498db",
    ), row=1, col=1)

    # 이동평균선
    colors = {"MA5": "#ff7f0e", "MA20": "#2ca02c", "MA60": "#9467bd", "MA120": "#7f7f7f"}
    for ma, color in colors.items():
        if ma in df.columns:
            fig.add_trace(go.Scatter(
                x=df.index, y=df[ma], name=ma, line=dict(color=color, width=1),
            ), row=1, col=1)

    # 볼린저
    if "BB_Upper" in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df["BB_Upper"], name="BB상", line=dict(color="rgba(150,150,150,0.5)", dash="dot")), row=1, col=1)
        fig.add_trace(go.Scatter(x=df.index, y=df["BB_Lower"], name="BB하", line=dict(color="rgba(150,150,150,0.5)", dash="dot"), fill="tonexty", fillcolor="rgba(150,150,150,0.1)"), row=1, col=1)

    # 거래량
    fig.add_trace(go.Bar(x=df.index, y=df["Volume"], name="거래량", marker_color="#95a5a6"), row=2, col=1)
    if "VOL_MA20" in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df["VOL_MA20"], name="거래량MA20", line=dict(color="orange")), row=2, col=1)

    # RSI
    if "RSI" in df.columns:
        fig.add_trace(go.Scatter(x=df.index, y=df["RSI"], name="RSI", line=dict(color="#8e44ad")), row=3, col=1)
        fig.add_hline(y=70, line_dash="dash", line_color="red", row=3, col=1)
        fig.add_hline(y=30, line_dash="dash", line_color="blue", row=3, col=1)
        fig.add_hline(y=50, line_dash="dot", line_color="gray", row=3, col=1)

    fig.update_layout(height=800, showlegend=True, xaxis_rangeslider_visible=False)
    fig.update_yaxes(tickformat=",.0f", ticksuffix="원", row=1, col=1)
    return fig


# ===== 상세 분석 렌더링 (단일 분석/포트폴리오 클릭 공용) =====
def render_analysis_detail(df_ind: pd.DataFrame, result: dict, name: str, code: str, buy_price: float = 0) -> None:
    last = df_ind.iloc[-1]
    prev = df_ind.iloc[-2] if len(df_ind) >= 2 else last
    chg = (last["Close"] - prev["Close"]) / prev["Close"] * 100

    st.markdown(f"## 📌 {format_stock(name, code)}")

    # 펀더멘털 한 줄 요약 (PER/PBR/배당률/EPS)
    fund = get_fundamentals(code)
    st.markdown(f"📊 {format_fundamentals_line(fund)}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("현재가", f"{int(last['Close']):,}원", f"{chg:+.2f}%")
    c2.metric("종합 점수", f"{result['total']}/100")
    c3.metric("매매 신호", result["verdict"])
    if buy_price > 0:
        pl = (last["Close"] - buy_price) / buy_price * 100
        c4.metric("평가 손익", f"{pl:+.2f}%")

    with st.expander("📖 매매 신호 기준 보기"):
        st.markdown(VERDICT_TABLE)

    st.markdown("### 🔬 분석 상세")
    for k, (s, msg) in result["details"].items():
        st.write(f"- **{k}**: {s}/5 — {msg}")

    st.markdown("### 💡 매매 팁")
    tip_line, glossary_line = make_technical_tip(result)
    st.markdown(f"- {tip_line}")
    if glossary_line:
        st.markdown(
            f"<div style='color:#A1A1AA;font-size:13px;margin-left:14px;margin-top:-6px'>🧭 풀이 — {glossary_line}</div>",
            unsafe_allow_html=True,
        )

    # 종목 관련 최근 뉴스 (상위 4건 본문 추출 + 알고리즘 요약)
    query = name if name and name != code else code
    with st.spinner("최근 뉴스 가져오고 요약하는 중..."):
        news_data = get_news_summaries(query, limit=4)
    if news_data.get("summary"):
        st.markdown(f"- 📰 {news_data['summary']}")
        with st.expander(f"📋 참고 기사 {len(news_data['articles'])}건 펼쳐보기"):
            for art in news_data["articles"]:
                src = art.get("source", "")
                st.markdown(
                    f"- [{art['title']}]({art['url']})"
                    + (f" <span style='color:#A1A1AA;font-size:12px'>· {src}</span>" if src else ""),
                    unsafe_allow_html=True,
                )
    else:
        st.caption("📰 최근 뉴스를 가져오지 못했습니다.")

    if result["total"] >= 60:
        st.markdown("### 💵 매수 가격대 제안")
        st.caption("매매 신호가 '매수 관심' 이상일 때만 노출됩니다. 분할 매수 시 단계별 진입 가격으로 활용하세요.")
        for zlabel, zprice, znote in suggest_buy_zones(df_ind):
            st.write(f"- **{zlabel}**: `{zprice:,}원` — {znote}")

    if buy_price > 0:
        cur_close = float(last["Close"])
        profit_base = max(buy_price, cur_close)
        in_profit = cur_close >= buy_price
        st.markdown("### 💰 손절/익절 가격")
        st.caption(
            "손절가: 매수가 기준 (원금 보호) · "
            f"익절가: {'현재가' if in_profit else '매수가'} 기준 "
            f"({'수익 중 → 추가 상승 목표' if in_profit else '손실 중 → 매수가 회복 후 목표'})"
        )
        levels = calc_stop_levels(buy_price, cur_close)
        cols = st.columns(3)
        cols[0].markdown(f"**🛑 손절가** _(매수가 {int(buy_price):,}원 기준)_")
        cols[0].write(f"보수적: {levels['보수적 손절(-3%)']:,}원")
        cols[0].write(f"일반: {levels['일반 손절(-7%)']:,}원")
        cols[0].write(f"최후: {levels['최후 손절(-10%)']:,}원")
        cols[2].markdown(f"**🎯 익절가** _({int(profit_base):,}원 기준)_")
        cols[2].write(f"1차: {levels['1차 익절(+3%)']:,}원")
        cols[2].write(f"2차: {levels['2차 익절(+7%)']:,}원")
        cols[2].write(f"3차: {levels['3차 익절(+15%)']:,}원")

    # 캔들 특이사항 (있을 때만 표시)
    candle_obs = analyze_candle(df_ind)
    if candle_obs:
        st.markdown("### 🕯️ 캔들 특이사항")
        for obs in candle_obs:
            st.markdown(f"- {obs}")

    st.markdown("### 📈 차트")
    st.plotly_chart(make_chart(df_ind, format_stock(name, code)), use_container_width=True)


# ===== 포트폴리오 저장/로드 =====
def load_portfolio() -> list:
    # 클라우드 배포 시: 보유종목을 비공개 Secrets에 보관 (공개 저장소에 노출 안 함)
    from_secrets = False
    items = None
    try:
        raw = st.secrets["portfolio_json"]
    except Exception:
        raw = None
    if raw:
        try:
            items = json.loads(raw)
            from_secrets = True
        except Exception:
            items = None

    if items is None:
        if not os.path.exists(PORTFOLIO_FILE):
            return []
        try:
            with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            return []
    # 이름이 비어있거나 코드와 같은 항목을 KRX 리스트에서 보강
    changed = False
    for item in items:
        code = item.get("code", "")
        name = item.get("name", "")
        if code and (not name or name == code):
            _, found_name, found = resolve_stock(code)
            if found and found_name and found_name != code:
                item["name"] = found_name
                changed = True
    if changed and not from_secrets:
        save_portfolio(items)
    return items


def save_portfolio(items: list) -> None:
    with open(PORTFOLIO_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


# ===== 분석 기록 저장/로드 =====
def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def append_history(entry: dict) -> None:
    history = load_history()
    history.insert(0, entry)
    if len(history) > HISTORY_LIMIT:
        history = history[:HISTORY_LIMIT]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def clear_history() -> None:
    if os.path.exists(HISTORY_FILE):
        os.remove(HISTORY_FILE)


# ===== 사이드바 =====
MODES = ["🔍 단일 종목 분석", "📋 포트폴리오 관리", "📜 분석 기록", "🧪 백테스트", "🔭 종목 발굴"]
MODE_KEYS = {"single": MODES[0], "portfolio": MODES[1], "history": MODES[2], "backtest": MODES[3], "screener": MODES[4]}
KEY_BY_MODE = {v: k for k, v in MODE_KEYS.items()}

# 새로고침 시 마지막 화면 복원: query_params → session_state
saved_key = st.query_params.get("m")
if saved_key in MODE_KEYS and "mode" not in st.session_state:
    st.session_state["mode"] = MODE_KEYS[saved_key]

st.sidebar.title("적게 일하고 많이 벌기 💵")
mode = st.sidebar.radio("어떤 작업을 하시겠어요?", MODES, key="mode", label_visibility="collapsed")

# 현재 메뉴를 URL에 기록 (새로고침해도 유지)
st.query_params["m"] = KEY_BY_MODE[mode]

st.sidebar.markdown("---")
st.sidebar.caption("⚠️ 본 도구는 참고용입니다. 모든 매매 결정의 책임은 본인에게 있습니다.")


# ===== 메인 화면 =====
if mode == "🔍 단일 종목 분석":
    st.title("🔍 단일 종목 분석")

    market_news = get_market_news(3)
    if market_news:
        with st.container(border=True):
            st.markdown("#### 📰 오늘의 증시 주요 뉴스 Top 3")
            st.caption("주가 흐름에 영향을 줄 만한 거시·시장 이슈 (Google News · 30분 캐시)")
            for idx, n in enumerate(market_news, 1):
                src = f"  ·  {n['source']}" if n["source"] else ""
                if n["link"]:
                    st.markdown(f"**{idx}.** [{n['title']}]({n['link']}){src}")
                else:
                    st.markdown(f"**{idx}.** {n['title']}{src}")
    st.write("")

    with st.form("single_form", clear_on_submit=True):
        col1, col2, col3 = st.columns([2, 2, 1])
        with col1:
            query = st.text_input("종목 코드 또는 이름 (예: 005930 또는 삼성전자)")
        with col2:
            buy_price = st.number_input("매수가 (선택)", min_value=0, value=0, step=100)
        with col3:
            st.write("")
            st.write("")
            run = st.form_submit_button("분석 시작", type="primary", use_container_width=True)

    if run and query:
        code, name, found = resolve_stock(query)
        if not found:
            st.warning(f"'{query}'와 일치하는 종목을 찾지 못했습니다. 입력값으로 직접 시도합니다.")
        with st.spinner("데이터 불러오는 중..."):
            df = load_stock_data(code)
        if df.empty:
            st.error(f"'{query}' 데이터를 가져올 수 없습니다. 코드/이름을 확인하세요.")
        else:
            df_ind = add_indicators(df)
            result = score_signal(df_ind)
            last = df_ind.iloc[-1]
            prev = df_ind.iloc[-2] if len(df_ind) >= 2 else last
            chg = (last["Close"] - prev["Close"]) / prev["Close"] * 100

            append_history({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "code": code,
                "name": name,
                "price": int(last["Close"]),
                "change_pct": round(chg, 2),
                "score": result["total"],
                "verdict": result["verdict"],
                "mode": "단일",
            })

            render_analysis_detail(df_ind, result, name, code, buy_price)

elif mode == "📜 분석 기록":
    st.title("📜 분석 기록")
    history = load_history()
    if not history:
        st.info("아직 분석 기록이 없습니다. 종목을 분석하면 자동으로 저장됩니다.")
    else:
        c1, c2 = st.columns([5, 1])
        c1.caption(f"총 {len(history)}건 · 최근 {HISTORY_LIMIT}건까지 보관")
        if c2.button("🗑️ 전체 삭제"):
            clear_history()
            st.rerun()

        # 필터
        stock_options = sorted({format_stock(h.get("name", ""), h.get("code", "")) for h in history})
        f1, f2 = st.columns([2, 2])
        sel_stock = f1.selectbox("종목 필터", ["전체"] + stock_options)
        sel_mode = f2.selectbox("분석 유형", ["전체", "단일", "일괄"])

        filtered = history
        if sel_stock != "전체":
            filtered = [h for h in filtered if format_stock(h.get("name", ""), h.get("code", "")) == sel_stock]
        if sel_mode != "전체":
            filtered = [h for h in filtered if h.get("mode") == sel_mode]

        rows = []
        for h in filtered:
            rows.append({
                "일시": h.get("timestamp", "-"),
                "종목": format_stock(h.get("name", ""), h.get("code", "")),
                "현재가": f"{h.get('price', 0):,}원",
                "등락": f"{h.get('change_pct', 0):+.2f}%",
                "점수": f"{h.get('score', 0)}/100",
                "신호": h.get("verdict", "-"),
                "유형": h.get("mode", "-"),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

elif mode == "🧪 백테스트":
    st.title("🧪 백테스트")
    st.caption("매수/매도 임계점 30여 개 조합을 모두 시뮬해 가장 좋은 조합을 자동으로 찾아줍니다.")

    c1, c2 = st.columns([3, 1])
    bt_query = c1.text_input("종목 코드 또는 이름", value="005930", key="bt_query")
    bt_period = c2.selectbox("기간", ["1년", "2년", "3년", "5년"], index=2)
    days_map = {"1년": 400, "2년": 760, "3년": 1120, "5년": 1850}

    if st.button("최적 임계점 분석", type="primary"):
        code, name, _ = resolve_stock(bt_query)
        with st.spinner(f"{format_stock(name, code)} 데이터 가져오는 중..."):
            df = load_stock_data(code, days=days_map[bt_period])
        if df.empty or len(df) < 80:
            st.error("백테스트에 충분한 데이터가 없습니다 (최소 80일 필요).")
        else:
            with st.spinner("일별 점수 계산..."):
                scores = compute_daily_scores(df)
            if not scores:
                st.error("점수 계산 실패")
            else:
                with st.spinner("임계점 조합 시뮬 중..."):
                    results = sweep_thresholds(scores)
                if not results:
                    st.error("유효한 조합을 찾지 못했습니다.")
                else:
                    # 시스템 수익률 기준 내림차순 정렬, 동률이면 거래수 적은 쪽 우선
                    results.sort(key=lambda x: (-x["system_return"], x["trades"]))
                    best = results[0]
                    bt = best["_sim"]

                    st.markdown(
                        f"### 🏆 최적 임계점: 매수 ≥ **{best['buy']}**점 · 매도 ≤ **{best['sell']}**점"
                    )
                    st.caption(f"📌 {format_stock(name, code)} · {bt_period} · "
                               f"총 {len(results)}개 조합 비교")

                    m1, m2, m3, m4, m5 = st.columns(5)
                    m1.metric("시스템 수익률", f"{best['system_return']:+.2f}%")
                    m2.metric("매수후보유", f"{best['bh_return']:+.2f}%")
                    m3.metric("초과 수익", f"{best['excess']:+.2f}%p")
                    m4.metric("거래 횟수", f"{best['trades']}회")
                    m5.metric("승률", f"{best['win_rate']:.1f}%")
                    st.caption(f"📉 MDD: {best['mdd']:.2f}% · "
                               f"최종 평가 {int(bt['final_value']):,}원"
                               + (" · ⚠️ 종료 시점 보유 중" if bt["holding_at_end"] else ""))

                    # 자산 곡선
                    eq_df = pd.DataFrame(bt["equity"])
                    first_price = eq_df.iloc[0]["price"]
                    eq_df["bh_value"] = bt["initial_capital"] * (eq_df["price"] / first_price)
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(
                        x=eq_df["date"], y=eq_df["value"],
                        name="시스템", line=dict(color="#22C55E", width=2),
                    ))
                    fig.add_trace(go.Scatter(
                        x=eq_df["date"], y=eq_df["bh_value"],
                        name="매수후보유", line=dict(color="#A1A1AA", dash="dot"),
                    ))
                    fig.update_layout(
                        height=400, margin=dict(t=40),
                        title="최적 조합 자산 곡선",
                        yaxis_tickformat=",.0f", yaxis_ticksuffix="원",
                    )
                    st.plotly_chart(fig, use_container_width=True)

                    # 전체 조합 비교 표
                    with st.expander(f"📊 전체 {len(results)}개 조합 결과 (수익률 순)"):
                        table = pd.DataFrame([{
                            "매수≥": r["buy"],
                            "매도≤": r["sell"],
                            "수익률": f"{r['system_return']:+.2f}%",
                            "B&H 대비": f"{r['excess']:+.2f}%p",
                            "거래": r["trades"],
                            "승률": f"{r['win_rate']:.1f}%",
                            "MDD": f"{r['mdd']:.2f}%",
                        } for r in results])
                        st.dataframe(table, use_container_width=True, hide_index=True)

                    # 거래 기록
                    if bt["trades"]:
                        with st.expander(f"📜 최적 조합 거래 기록 ({len(bt['trades'])}건)"):
                            rows = []
                            for t in bt["trades"]:
                                rows.append({
                                    "매수일": t["entry_date"].strftime("%Y-%m-%d"),
                                    "매수가": f"{int(t['entry_price']):,}원",
                                    "매도일": t["exit_date"].strftime("%Y-%m-%d"),
                                    "매도가": f"{int(t['exit_price']):,}원",
                                    "수익률": f"{t['profit_pct']:+.2f}%",
                                })
                            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

                    st.warning(
                        "⚠️ 과거 데이터에 최적화된 결과로 미래 수익을 보장하지 않습니다. "
                        "여러 종목·여러 기간으로 교차 검증하셔야 의미가 있습니다 (과최적화 위험)."
                    )

elif mode == "🔭 종목 발굴":
    render_screener(
        load_stock_data=load_stock_data,
        add_indicators=add_indicators,
        score_signal=score_signal,
    )

else:  # 포트폴리오 관리
    st.title("📋 포트폴리오 관리")
    portfolio = load_portfolio()

    with st.form("add_form", clear_on_submit=True):
        c1, c2, c3 = st.columns([2, 1, 1])
        new_query = c1.text_input("종목 코드 또는 이름 (예: 005930 또는 삼성전자)")
        new_buy = c2.number_input("매수가", min_value=0, step=100)
        new_qty = c3.number_input("수량", min_value=0, step=1)
        if st.form_submit_button("➕ 추가"):
            if new_query:
                code, name, found = resolve_stock(new_query)
                if not found:
                    st.error(
                        f"'{new_query}' 종목을 찾을 수 없어 추가하지 못했습니다. "
                        "정확한 종목 코드(예: 005930) 또는 종목명으로 다시 시도해 주세요."
                    )
                else:
                    portfolio.append({
                        "code": code,
                        "name": name,
                        "buy_price": new_buy,
                        "quantity": new_qty,
                    })
                    save_portfolio(portfolio)
                    st.success(f"{format_stock(name, code)} 추가됨")
                    st.rerun()

    if portfolio:
        mobile = is_mobile()
        st.markdown("### 현재 보유 종목")
        st.caption("👇 종목 이름을 누르면 그 자리 바로 아래에 상세 분석이 펼쳐집니다.")
        with st.expander("📖 매매 신호 기준 보기"):
            st.markdown(VERDICT_TABLE)

        # 데스크톱: 표 헤더 (모바일은 카드형이라 헤더 생략)
        col_widths = [2.4, 0.5, 3, 2, 1, 2, 0.6, 0.6]
        if not mobile:
            h = st.columns(col_widths, vertical_alignment="center")
            h[0].caption("종목")
            h[2].caption("매매 신호")
            h[3].caption("매수가")
            h[4].caption("수량")
            h[5].caption("평가금액")

        detail_idx = st.session_state.get("portfolio_detail")
        edit_idx = st.session_state.get("portfolio_edit")
        for i, item in enumerate(portfolio):
            label = format_stock(item.get("name", ""), item.get("code", ""))

            # 종목별 분석 (캐시되어 있으니 재호출은 빠름)
            code = item.get("code", "")
            df_data = load_stock_data(code) if code else pd.DataFrame()
            if not df_data.empty:
                df_ind = add_indicators(df_data)
                result = score_signal(df_ind)
                signal_text = f"{result['verdict']} ({result['total']}/100)"
                cur_price = int(df_ind.iloc[-1]["Close"])
            else:
                df_ind = None
                result = None
                signal_text = "⚠️ 데이터 없음"
                cur_price = 0

            buy = item.get("buy_price", 0) or 0
            qty = item.get("quantity", 0) or 0
            eval_text = f"{cur_price * qty:,}" if cur_price else "-"

            if mobile:
                # ----- 모바일: 종목별 카드 -----
                with st.container(border=True):
                    if st.button(label, key=f"row_{i}", use_container_width=True):
                        if detail_idx == i:
                            st.session_state.pop("portfolio_detail", None)
                        else:
                            st.session_state["portfolio_detail"] = i
                            st.session_state.pop("portfolio_edit", None)
                        st.rerun()
                    st.markdown(
                        "<div style='font-size:13px;line-height:1.9;color:var(--ds-text-muted)'>"
                        f"🔔 {signal_text}<br>"
                        f"매수가 <b style='color:var(--ds-text)'>{buy:,}</b> · "
                        f"수량 <b style='color:var(--ds-text)'>{qty}</b> · "
                        f"평가 <b style='color:var(--ds-text)'>{eval_text}</b>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                    bc = st.columns(2)
                    if bc[0].button("✏️ 수정", key=f"edit_{i}", use_container_width=True):
                        if edit_idx == i:
                            st.session_state.pop("portfolio_edit", None)
                        else:
                            st.session_state["portfolio_edit"] = i
                            st.session_state.pop("portfolio_detail", None)
                        st.rerun()
                    if bc[1].button("🗑️ 삭제", key=f"del_{i}", use_container_width=True):
                        portfolio.pop(i)
                        save_portfolio(portfolio)
                        st.session_state.pop("portfolio_detail", None)
                        st.session_state.pop("portfolio_edit", None)
                        st.rerun()
            else:
                # ----- 데스크톱: 표 한 줄 -----
                cols = st.columns(col_widths, vertical_alignment="center")
                if cols[0].button(label, key=f"row_{i}", use_container_width=True):
                    if detail_idx == i:
                        st.session_state.pop("portfolio_detail", None)
                    else:
                        st.session_state["portfolio_detail"] = i
                        st.session_state.pop("portfolio_edit", None)
                    st.rerun()
                # cols[1]은 종목 ↔ 매매신호 사이 여백 (spacer)
                cols[2].write(signal_text)
                cols[3].write(f"{buy:,}")
                cols[4].write(f"{qty}")
                cols[5].write(eval_text)
                if cols[6].button("✏️", key=f"edit_{i}", help="매수가·수량 수정"):
                    if edit_idx == i:
                        st.session_state.pop("portfolio_edit", None)
                    else:
                        st.session_state["portfolio_edit"] = i
                        st.session_state.pop("portfolio_detail", None)
                    st.rerun()
                if cols[7].button("🗑️", key=f"del_{i}", help="삭제"):
                    portfolio.pop(i)
                    save_portfolio(portfolio)
                    st.session_state.pop("portfolio_detail", None)
                    st.session_state.pop("portfolio_edit", None)
                    st.rerun()

            # 수정 폼 (편집 모드일 때 행 바로 아래)
            if edit_idx == i:
                with st.container(border=True):
                    st.markdown(f"✏️ **{label}** 매수가·수량 수정")
                    with st.form(f"edit_form_{i}"):
                        e1, e2, e3, e4 = st.columns([2, 2, 1, 1])
                        new_buy = e1.number_input(
                            "매수가", min_value=0, step=100,
                            value=int(item.get("buy_price", 0) or 0),
                            key=f"edit_buy_{i}",
                        )
                        new_qty = e2.number_input(
                            "수량", min_value=0, step=1,
                            value=int(item.get("quantity", 0) or 0),
                            key=f"edit_qty_{i}",
                        )
                        e3.write("")
                        e4.write("")
                        save_btn = e3.form_submit_button("저장", type="primary", use_container_width=True)
                        cancel_btn = e4.form_submit_button("취소", use_container_width=True)
                        if save_btn:
                            portfolio[i]["buy_price"] = new_buy
                            portfolio[i]["quantity"] = new_qty
                            save_portfolio(portfolio)
                            st.session_state.pop("portfolio_edit", None)
                            st.success(f"{label} 수정됨")
                            st.rerun()
                        if cancel_btn:
                            st.session_state.pop("portfolio_edit", None)
                            st.rerun()

            # 클릭된 행 바로 아래에 상세 분석 펼치기
            if detail_idx == i:
                with st.container(border=True):
                    if df_ind is None:
                        st.error("데이터를 가져올 수 없습니다.")
                    else:
                        render_analysis_detail(
                            df_ind, result,
                            item.get("name", ""), item.get("code", ""),
                            item.get("buy_price", 0),
                        )
    else:
        st.info("아직 등록된 종목이 없습니다.")
