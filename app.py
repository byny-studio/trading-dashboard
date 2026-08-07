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
from theme_tracker import render_theme_tracker, leader_theme_map
import us_market

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


# 종목 업종·주요제품 한 줄 설명(종목명 옆 회색 글씨) — 공용 모듈
from stock_meta import stock_desc, desc_html  # noqa: E402


def _relative_time(pub_str: str) -> str:
    """RFC822 발행시각 → 상대시간 표기('방금'/'25분 전'/'3시간 전'/'2일 전')."""
    if not pub_str:
        return ""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(pub_str)
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        secs = max(0, (now - dt).total_seconds())
        if secs < 3600:
            m = int(secs // 60)
            return f"{m}분 전" if m >= 1 else "방금"
        if secs < 86400:
            return f"{int(secs // 3600)}시간 전"
        return f"{int(secs // 86400)}일 전"
    except Exception:
        return ""


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
            "pub": (item.findtext("pubDate") or "").strip(),   # 발행시각(상대시간 계산용)
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


# ===== 시장 추세 필터 (KOSPI 200일선) =====
@st.cache_data(ttl=3600)
def get_market_uptrend_series() -> pd.Series:
    """KOSPI가 200일선 위인지(상승추세) 날짜별 불리언 Series. 백테스트 시장필터용."""
    try:
        end = datetime.now()
        start = end - timedelta(days=1850 + 300)
        df = fdr.DataReader("KS11", start, end)
        if df is None or df.empty or len(df) < 200:
            return pd.Series(dtype=bool)
        ma200 = df["Close"].rolling(200).mean()
        up = (df["Close"] >= ma200)
        up.index = pd.to_datetime(up.index)
        return up.dropna()
    except Exception:
        return pd.Series(dtype=bool)


MKT_BAND = 0.02       # 완충대: 200일선 ±2% 안이면 '중립'(휩쏘 방지)
MKT_SLOPE_LB = 20     # 200일선 기울기 판단 기간(거래일)
MKT_SLOPE_TH = 0.003  # 기울기 임계: 20일간 200선 변화 ±0.3%
MKT_CRASH_DD = -12    # 급락 경보: 최근 고점(60일) 대비 낙폭 이하면 크래시(200선 위여도)
MKT_CRASH_MOM = -10   # 급락 경보: 20일 수익률 이하면 크래시


@st.cache_data(ttl=3600)
def get_market_regime() -> dict:
    """현재 시장(KOSPI) 국면. 200일선 대비 위치(완충대 ±2%) + 200일선 기울기 결합.
    {'ok','bullish'(종가≥200선, 기존호환),'regime'(상승/중립/하락),
     'slope'(up/flat/down),'band'(above/within/below),'index','ma200','gap_pct','slope_pct'}."""
    up = get_market_uptrend_series()
    base = {"ok": False, "bullish": True, "regime": "중립", "slope": "flat", "band": "within"}
    if up.empty:
        return base
    try:
        end = datetime.now()
        df = fdr.DataReader("KS11", end - timedelta(days=560), end)
        ma = df["Close"].rolling(200).mean()
        cur = float(df["Close"].iloc[-1])
        ma200 = float(ma.iloc[-1])
        ma_prev = float(ma.iloc[-(MKT_SLOPE_LB + 1)]) if ma.notna().sum() > MKT_SLOPE_LB else ma200
        gap = (cur - ma200) / ma200 if ma200 else 0            # 지수 vs 200선 이격
        slope_pct = (ma200 - ma_prev) / ma_prev if ma_prev else 0  # 200선 20일 기울기
        band = "above" if gap > MKT_BAND else "below" if gap < -MKT_BAND else "within"
        slope = "up" if slope_pct > MKT_SLOPE_TH else "down" if slope_pct < -MKT_SLOPE_TH else "flat"
        # 국면 확정: 완충대 밖 + 기울기가 방향과 일치(또는 flat)일 때만. 상충/완충대 안 = 중립(전환기)
        if band == "above" and slope in ("up", "flat"):
            regime = "상승"
        elif band == "below" and slope in ("down", "flat"):
            regime = "하락"
        else:
            regime = "중립"
        # ⚠️ 급락 오버라이드: 200선은 후행이라 단기 폭락을 놓침 → 최근 고점 낙폭·20일 모멘텀으로 감지
        recent_peak = float(df["Close"].iloc[-60:].max()) if len(df) >= 60 else cur
        dd_from_peak = (cur - recent_peak) / recent_peak * 100 if recent_peak else 0
        mom20 = (cur - float(df["Close"].iloc[-21])) / float(df["Close"].iloc[-21]) * 100 if len(df) > 21 else 0
        crash = dd_from_peak <= MKT_CRASH_DD or mom20 <= MKT_CRASH_MOM
        return {"ok": True, "bullish": cur >= ma200, "regime": regime, "slope": slope,
                "band": band, "index": cur, "ma200": ma200,
                "gap_pct": gap * 100, "slope_pct": slope_pct * 100,
                "crash": crash, "dd_from_peak": dd_from_peak, "mom20": mom20}
    except Exception:
        return {"ok": True, "bullish": bool(up.iloc[-1]), "regime": "중립",
                "slope": "flat", "band": "within"}


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


def _parse_signed(txt: str):
    """'+2,313,745' / '-716,994' / '' → float 또는 None."""
    if not txt:
        return None
    t = txt.replace(",", "").replace("+", "").strip()
    if t in ("", "-", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


@st.cache_data(ttl=86400)
def get_financials(code: str) -> dict:
    """네이버 기업실적분석 표에서 연간 재무(매출/영업이익/순이익/이익률/ROE/부채비율) 스크레이핑."""
    if not code:
        return {}
    try:
        url = f"https://finance.naver.com/item/main.naver?code={code}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        if r.status_code != 200:
            return {}
        r.encoding = "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")
        tbl = soup.select_one("div.section.cop_analysis table")
        if not tbl:
            return {}
        heads = tbl.select("thead tr")
        if len(heads) < 2:
            return {}
        periods = [th.get_text(strip=True) for th in heads[1].select("th")]
        # 연간 실적 컬럼 수 = '최근 연간 실적' th의 colspan
        annual_n = 4
        for th in heads[0].select("th"):
            if "연간" in th.get_text():
                annual_n = int(th.get("colspan", 4))
                break
        wanted = {
            "매출액": "매출액", "영업이익": "영업이익", "당기순이익": "당기순이익",
            "영업이익률": "영업이익률", "ROE(지배주주)": "ROE", "부채비율": "부채비율",
        }
        out = {"periods": periods[:annual_n]}
        for tr in tbl.select("tbody tr"):
            th = tr.select_one("th")
            if not th:
                continue
            label = th.get_text(strip=True)
            if label in wanted:
                vals = [_parse_signed(td.get_text(strip=True)) for td in tr.select("td")]
                out[wanted[label]] = vals[:annual_n]
        return out
    except Exception:
        return {}


@st.cache_data(ttl=1800)
def get_supply_demand(code: str, days: int = 20) -> dict:
    """네이버 외국인/기관 순매매(주식수) 최근 days일 스크레이핑 + 5/20일 누적."""
    if not code:
        return {}
    try:
        url = f"https://finance.naver.com/item/frgn.naver?code={code}"
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        if r.status_code != 200:
            return {}
        r.encoding = "euc-kr"
        soup = BeautifulSoup(r.text, "html.parser")
        rows = []
        for tr in soup.select("table.type2 tr"):
            tds = [td.get_text(strip=True) for td in tr.select("td")]
            if len(tds) >= 9 and tds[0] and "." in tds[0]:
                rows.append({
                    "date": tds[0], "close": _parse_signed(tds[1]),
                    "inst": _parse_signed(tds[5]), "frgn": _parse_signed(tds[6]),
                    "frgn_ratio": tds[8],
                })
        rows = rows[:days]
        if not rows:
            return {}

        def _sum(key, n):
            return sum((row[key] or 0) for row in rows[:n])
        return {
            "rows": rows,
            "inst_5": _sum("inst", 5), "frgn_5": _sum("frgn", 5),
            "inst_20": _sum("inst", len(rows)), "frgn_20": _sum("frgn", len(rows)),
            "n": len(rows),
        }
    except Exception:
        return {}


@st.cache_data(ttl=1800)
def get_kiwoom_supply(code: str) -> dict:
    """키움 ka10059 20일 투자자별 수급 → 사모 중심 매집 판단(flow_verdict).
    키움 미설정/실패 시 {} → 호출측이 네이버 수급으로 폴백(클라우드는 네이버). 로컬에서만 사모까지."""
    import os, sys
    try:
        ad = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autotrade")
        if os.path.isdir(ad) and ad not in sys.path:
            sys.path.insert(0, ad)
        from kiwoom_api import from_secrets
        from accumulation import flow_verdict, kiwoom_flow
        api = from_secrets()
        fl = kiwoom_flow(api, code)
        if fl is None or len(fl) < 12:
            return {}
        return flow_verdict(fl)
    except Exception:
        return {}


def analyze_financials(fin: dict) -> str:
    """재무 표 → 기업분석 한 줄 요약(성장성·수익성·안정성·흑자여부)."""
    if not fin or not fin.get("매출액"):
        return ""
    def _actual(key):
        """(E) 추정 제외한 실제 연간값 리스트."""
        periods, vals = fin.get("periods", []), fin.get(key, [])
        return [v for p, v in zip(periods, vals) if v is not None and "(E)" not in p]
    parts = []
    sales = _actual("매출액")
    if len(sales) >= 2:
        g = (sales[-1] - sales[-2]) / abs(sales[-2]) * 100 if sales[-2] else 0
        if len(sales) >= 3 and sales[-1] > sales[-2] > sales[-3]:
            parts.append(f"📈 매출 꾸준히 성장(최근 +{g:.0f}%)")
        elif g > 5:
            parts.append(f"📈 매출 성장(+{g:.0f}%)")
        elif g < -5:
            parts.append(f"📉 매출 감소({g:.0f}%)")
        else:
            parts.append("➡️ 매출 정체")
    profit = _actual("당기순이익")
    if profit:
        if profit[-1] is not None and profit[-1] < 0:
            parts.append("🔴 최근 순이익 적자")
        elif len(profit) >= 2 and profit[-2] is not None and profit[-2] < 0 <= profit[-1]:
            parts.append("🟢 흑자 전환")
    opm = _actual("영업이익률")
    if opm:
        v = opm[-1]
        if v is not None:
            if v >= 15:
                parts.append(f"💰 고수익(영업이익률 {v:.0f}%)")
            elif v < 0:
                parts.append("⚠️ 영업적자")
    roe = _actual("ROE")
    if roe and roe[-1] is not None and roe[-1] >= 10:
        parts.append(f"⭐ ROE {roe[-1]:.0f}%(자본효율 우수)")
    debt = _actual("부채비율")
    if debt and debt[-1] is not None:
        v = debt[-1]
        if v >= 200:
            parts.append(f"⚠️ 부채비율 {v:.0f}%(높음)")
        elif v <= 100:
            parts.append(f"🛡️ 부채비율 {v:.0f}%(안정)")
    return "  ·  ".join(parts)


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


# ===== 추세 점수 / 반등 점수 (철학 분리) =====
def trend_score(df: pd.DataFrame, horizon: str = "short") -> dict:
    """추세추종 점수 — '오르는 추세에 올라타기'. 5개 항목 × 5점 → 100점.
    horizon: "short"(단기, MA5/20 크로스) / "mid"(중장기 몇 주, MA20/60 크로스)."""
    if df.empty or len(df) < 60:
        return {"total": 0, "details": {}}
    last = df.iloc[-1]
    d = {}
    mid = horizon == "mid"
    fast_n, slow_n = (60, 120) if mid else (5, 20)  # 크로스오버 쌍(중장기 1~3개월: MA60/120)

    # 1. 이평선 정배열 (중장기는 MA5 무시, MA20>60>120 위주)
    mas = [last.get(f"MA{n}") for n in (5, 20, 60, 120)]
    if all(pd.notna(mas)):
        if mid:
            if mas[1] > mas[2] > mas[3]:
                d["이평선 정배열"] = (5, "완벽한 정배열(20>60>120)")
            elif mas[1] > mas[2]:
                d["이평선 정배열"] = (4, "중기 정배열(20>60)")
            elif mas[1] < mas[2] < mas[3]:
                d["이평선 정배열"] = (0, "완벽한 역배열")
            else:
                d["이평선 정배열"] = (2, "혼조")
        else:
            if mas[0] > mas[1] > mas[2] > mas[3]:
                d["이평선 정배열"] = (5, "완벽한 정배열")
            elif mas[0] > mas[1] > mas[2]:
                d["이평선 정배열"] = (4, "단·중기 정배열")
            elif mas[0] > mas[1]:
                d["이평선 정배열"] = (3, "단기 상승")
            elif mas[0] < mas[1] < mas[2] < mas[3]:
                d["이평선 정배열"] = (0, "완벽한 역배열")
            else:
                d["이평선 정배열"] = (2, "혼조")
    else:
        d["이평선 정배열"] = (2, "데이터 부족")

    # 2. 골든크로스 / 단기선 위치 (short: MA5/20, mid: MA20/60)
    cf, cs = f"MA{fast_n}", f"MA{slow_n}"
    if len(df) >= 2 and pd.notna(df[cf].iloc[-1]) and pd.notna(df[cs].iloc[-1]):
        mf, ms = df[cf].iloc[-1], df[cs].iloc[-1]
        mfp, msp = df[cf].iloc[-2], df[cs].iloc[-2]
        if mfp <= msp and mf > ms:
            d["크로스"] = (5, f"골든크로스 발생({fast_n}/{slow_n})")
        elif mfp >= msp and mf < ms:
            d["크로스"] = (0, f"데드크로스 발생({fast_n}/{slow_n})")
        elif mf > ms:
            d["크로스"] = (4, f"{fast_n}일선이 {slow_n}일선 위")
        else:
            d["크로스"] = (1, f"{fast_n}일선이 {slow_n}일선 아래")
    else:
        d["크로스"] = (2, "데이터 부족")

    # 3. RSI 건강구간 (중장기는 밴드 넓게: 45~75)
    lo, hi = (45, 75) if mid else (50, 70)
    rsi = last.get("RSI")
    if pd.notna(rsi):
        if lo <= rsi < hi:
            d["RSI"] = (5, f"건강한 상승 {rsi:.1f}")
        elif rsi >= hi:
            d["RSI"] = (3, f"과열이나 강세 {rsi:.1f}")
        elif (lo - 10) <= rsi < lo:
            d["RSI"] = (2, f"동력 약함 {rsi:.1f}")
        else:
            d["RSI"] = (0, f"추세 없음 {rsi:.1f}")
    else:
        d["RSI"] = (2, "데이터 부족")

    # 4. 볼린저 위치 (추세는 중심선 위가 좋음)
    close = last["Close"]
    bu, bl = last.get("BB_Upper"), last.get("BB_Lower")
    if pd.notna(bu) and pd.notna(bl):
        pos = (close - bl) / (bu - bl) if bu != bl else 0.5
        if 0.5 <= pos < 0.8:
            d["볼린저"] = (5, "중심선 위(상승 흐름)")
        elif pos >= 0.8:
            d["볼린저"] = (4, "상단 근처(강세·과열)")
        elif 0.3 <= pos < 0.5:
            d["볼린저"] = (2, "중심선 아래")
        else:
            d["볼린저"] = (0, "하단권(추세 약함)")
    else:
        d["볼린저"] = (2, "데이터 부족")

    # 5. 거래량 (추세 확인)
    vol = last["Volume"]
    vma = last.get("VOL_MA20")
    if pd.notna(vma) and vma > 0:
        r = vol / vma
        if r >= 2.0:
            d["거래량"] = (5, f"폭증 ({r:.1f}배)")
        elif r >= 1.3:
            d["거래량"] = (4, f"증가 ({r:.1f}배)")
        elif r >= 0.8:
            d["거래량"] = (3, f"평균 ({r:.1f}배)")
        else:
            d["거래량"] = (1, f"감소 ({r:.1f}배)")
    else:
        d["거래량"] = (2, "데이터 부족")

    return {"total": sum(v[0] for v in d.values()) * 4, "details": d}


def reversion_score(df: pd.DataFrame, horizon: str = "short") -> dict:
    """반등(역추세) 점수 — '많이 빠진 종목의 반등 노리기'. 5개 항목 × 5점 → 100점.
    horizon: "short"(MA20 이격) / "mid"(MA60 이격, 더 깊은 눌림 요구)."""
    if df.empty or len(df) < 60:
        return {"total": 0, "details": {}}
    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    d = {}
    mid = horizon == "mid"

    # 1. RSI 과매도 (낮을수록 반등 기대)
    rsi = last.get("RSI")
    if pd.notna(rsi):
        if rsi < 30:
            d["RSI 과매도"] = (5, f"과매도 {rsi:.1f}")
        elif rsi < 40:
            d["RSI 과매도"] = (4, f"약한 과매도 {rsi:.1f}")
        elif rsi < 50:
            d["RSI 과매도"] = (2, f"조정권 {rsi:.1f}")
        else:
            d["RSI 과매도"] = (0, f"반등 영역 아님 {rsi:.1f}")
    else:
        d["RSI 과매도"] = (2, "데이터 부족")

    # 2. 볼린저 하단 근접
    close = last["Close"]
    bu, bl = last.get("BB_Upper"), last.get("BB_Lower")
    if pd.notna(bu) and pd.notna(bl):
        pos = (close - bl) / (bu - bl) if bu != bl else 0.5
        if pos < 0.1:
            d["볼린저 하단"] = (5, "밴드 하단(반등 기대)")
        elif pos < 0.25:
            d["볼린저 하단"] = (4, "하단권")
        elif pos < 0.45:
            d["볼린저 하단"] = (2, "중심선 아래")
        else:
            d["볼린저 하단"] = (0, "하단권 아님")
    else:
        d["볼린저 하단"] = (2, "데이터 부족")

    # 3. 이격도 (얼마나 아래로 빠졌나 / short: MA20, mid: MA120 기준 더 깊게)
    base_n = 120 if mid else 20
    base_ma = last.get(f"MA{base_n}")
    # 중장기(1~3개월)는 더 깊은 눌림을 노리므로 임계를 더 깊게 (-22/-13/-6%)
    t1, t2, t3 = (-0.22, -0.13, -0.06) if mid else (-0.12, -0.07, -0.03)
    if pd.notna(base_ma) and base_ma > 0:
        gap = (close - base_ma) / base_ma
        if gap <= t1:
            d["낙폭(이격)"] = (5, f"{base_n}일선 -{abs(gap)*100:.0f}% 급락")
        elif gap <= t2:
            d["낙폭(이격)"] = (4, f"{base_n}일선 -{abs(gap)*100:.0f}%")
        elif gap <= t3:
            d["낙폭(이격)"] = (2, f"{base_n}일선 -{abs(gap)*100:.0f}%")
        else:
            d["낙폭(이격)"] = (1, "낙폭 작음")
    else:
        d["낙폭(이격)"] = (2, "데이터 부족")

    # 4. 거래량 동반 (반등엔 거래 필요)
    vol = last["Volume"]
    vma = last.get("VOL_MA20")
    if pd.notna(vma) and vma > 0:
        r = vol / vma
        if r >= 1.5:
            d["거래량 동반"] = (5, f"거래 급증 ({r:.1f}배)")
        elif r >= 1.0:
            d["거래량 동반"] = (3, f"평균 이상 ({r:.1f}배)")
        else:
            d["거래량 동반"] = (1, f"거래 부족 ({r:.1f}배)")
    else:
        d["거래량 동반"] = (2, "데이터 부족")

    # 5. 반등 시작 (오늘 양봉 + 직전보다 상승 전환)
    if pd.notna(prev.get("Close")):
        up_today = close > prev["Close"]
        candle_up = close > last.get("Open", close)
        if up_today and candle_up:
            d["반등 시작"] = (5, "양봉·상승 전환")
        elif up_today:
            d["반등 시작"] = (3, "전일 대비 상승")
        else:
            d["반등 시작"] = (1, "아직 하락 중")
    else:
        d["반등 시작"] = (2, "데이터 부족")

    return {"total": sum(v[0] for v in d.values()) * 4, "details": d}


def momentum_score(df: pd.DataFrame) -> dict:
    """모멘텀 점수 — '최근 강하게 오르는 종목 잡기'. 5개 항목 × 5점 → 100점.
    최근 상승률·가속·신고가 근접·거래량·강한 RSI. (종목 발굴 '모멘텀' 기준용)"""
    if df.empty or len(df) < 120:
        return {"total": 0, "details": {}}
    last = df.iloc[-1]
    close = float(last["Close"])
    d = {}

    # 1. 최근 20일(약 1개월) 수익률
    c20 = float(df["Close"].iloc[-21]) if len(df) >= 21 else close
    r20 = (close - c20) / c20 * 100 if c20 else 0
    d["20일 수익률"] = (5 if r20 >= 20 else 4 if r20 >= 10 else 3 if r20 >= 3
                       else 1 if r20 >= 0 else 0, f"{r20:+.1f}%")

    # 2. 최근 5일 가속(단기 탄력)
    c5 = float(df["Close"].iloc[-6]) if len(df) >= 6 else close
    r5 = (close - c5) / c5 * 100 if c5 else 0
    d["5일 가속"] = (5 if r5 >= 8 else 4 if r5 >= 3 else 3 if r5 >= 0 else 1, f"{r5:+.1f}%")

    # 3. 신고가 근접도 (최근 1년 고점 대비 현재 위치)
    win = min(252, len(df))
    hi = float(df["Close"].iloc[-win:].max())
    prox = close / hi if hi else 0
    d["신고가 근접"] = (5 if prox >= 0.98 else 4 if prox >= 0.90 else 2 if prox >= 0.80
                      else 0, f"고점의 {prox*100:.0f}%")

    # 4. 거래량 증가 (모멘텀엔 거래 동반 필수)
    vma = last.get("VOL_MA20")
    vr = float(last["Volume"]) / vma if (pd.notna(vma) and vma > 0) else 1
    d["거래량"] = (5 if vr >= 2 else 4 if vr >= 1.3 else 3 if vr >= 0.8 else 1, f"{vr:.1f}배")

    # 5. RSI 강세구간 (모멘텀은 강한 RSI 선호, 과매도는 모멘텀 아님)
    rsi = last.get("RSI")
    if pd.notna(rsi):
        d["RSI"] = (5 if 55 <= rsi < 80 else 3 if rsi >= 80 else 2 if 45 <= rsi < 55
                    else 0, f"{rsi:.1f}")
    else:
        d["RSI"] = (2, "데이터 부족")

    return {"total": sum(v[0] for v in d.values()) * 4, "details": d}


def dual_verdict(trend_total: int, rev_total: int, market_bullish: bool = True) -> str:
    """추세·반등 점수로 자동 판정. 추세 매수는 시장이 상승추세일 때만 인정(가짜 신호 제거)."""
    # 추세 매수: KOSPI 200일선 위(상승장)일 때만 인정
    if market_bullish and trend_total >= 70:
        return "📈 추세 매수"
    if rev_total >= 70:
        return "🔄 반등 매수"
    if market_bullish and trend_total >= 55:
        return "📈 추세 양호"
    if rev_total >= 55:
        return "🔄 반등 주목"
    if not market_bullish and trend_total >= 55:
        return "🛑 추세신호(장세 약세→보류)"
    if trend_total <= 24 and rev_total <= 24:
        return "⚠️ 약세(관망)"
    return "⏸️ 관망"


def overheat_signal(df: pd.DataFrame, horizon: str = "short") -> dict:
    """급등 과열 감지 → 익절 고려 경고. 점수 시스템이 못 잡는 '꼭지' 보완.
    horizon: "mid"는 며칠 급등에 안 흔들리도록 임계를 넓힘(더 긴 창·높은 문턱)."""
    mid = horizon == "mid"
    win = 40 if mid else 5           # 급등 판정 창 (거래일; 중장기는 40일)
    if df.empty or len(df) < win + 1:
        return {"level": 0, "text": "", "conds": []}
    last = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(last["Close"])
    rsi = last.get("RSI")
    bb_u = last.get("BB_Upper")
    chg1 = (close - prev["Close"]) / prev["Close"] * 100 if prev["Close"] else 0
    close_w = float(df["Close"].iloc[-(win + 1)])
    chg_w = (close - close_w) / close_w * 100 if close_w else 0
    vol = last["Volume"]
    vma = last.get("VOL_MA20")
    vol_ratio = vol / vma if (pd.notna(vma) and vma > 0) else 0

    # 중장기는 높은 RSI(78)·큰 급등폭 요구 → 잔파동에 익절경고 안 뜸
    rsi_hi = 78 if mid else 70
    spike1_th = 15 if mid else 8
    spike_w_th = 60 if mid else 18
    c_rsi = pd.notna(rsi) and rsi >= rsi_hi
    c_band = pd.notna(bb_u) and close >= bb_u
    c_spike1 = chg1 >= spike1_th
    c_spike5 = chg_w >= spike_w_th
    c_vol = vol_ratio >= 2

    conds = [
        (c_rsi, f"RSI {rsi:.0f} ({rsi_hi}↑ 과매수)" if pd.notna(rsi) else "RSI 데이터 없음"),
        (c_band, "볼린저 상단 돌파(과열)"),
        (c_spike1, f"1일 급등 {chg1:+.1f}%"),
        (c_spike5, f"{win}일 급등 {chg_w:+.1f}%"),
        (c_vol, f"거래량 {vol_ratio:.1f}배"),
    ]
    met = sum(1 for ok, _ in conds if ok)
    # 강한 과열(익절 고려): 과매수+확인, 또는 급등+밴드돌파/대량거래, 또는 3개 이상 동시
    strong = (
        (c_rsi and (c_band or c_spike1))
        or (c_spike1 and (c_band or c_vol))
        or met >= 3
    )
    if strong:
        return {"level": 2, "text": "🔥 과열 — 일부 익절 고려", "conds": conds, "met": met}
    if c_band or c_spike1 or met >= 2:
        return {"level": 1, "text": "⚠️ 과열 주의", "conds": conds, "met": met}
    return {"level": 0, "text": "", "conds": conds, "met": met}


def position_action(buy_price, cur_price, trend, rev, oh_level, market_bullish=True,
                    horizon="short", strategy="auto"):
    """내 매수가·손익 + 기술 신호를 합친 종목별 행동 제안.
    매수가가 없으면 빈 문자열(포지션 정보 없음).

    strategy: 종목을 '왜 샀는지'(trend=추세추종 / reversion=과매도반등)에 맞춰 관리.
      "auto"면 현재 점수가 우세한 축으로 추론 → 반등으로 산 종목을 '추세 약하다'는
      이유로 손절하지 않음(산 이유와 파는 이유의 축을 일치시켜 가짜 손절 방지).
    horizon: "mid"는 손절/익절폭을 넓게(+50%/-18%)."""
    if not buy_price or buy_price <= 0 or not cur_price:
        return ""
    mid = horizon == "mid"
    tp, sl = (50, -18) if mid else (20, -8)   # 익절 목표 / 손절 라인
    hard = sl * 2.5                            # 치명적 손실은 축 무관 손절(short -20%/mid -45%)
    pl = (cur_price - buy_price) / buy_price * 100
    p = f"{pl:+.1f}%"

    # 관리 축 결정: 태그 있으면 그대로, 없으면 우세 점수로 추론
    axis = strategy if strategy in ("trend", "reversion") else \
        ("reversion" if rev > trend else "trend")

    # (공통) 과열 + 수익 → 익절
    if oh_level >= 2 and pl > 0:
        return f"🔥 익절 고려 · 손익 {p} (과열)"
    # (공통) 치명적 손실 → 축 무관 손절 (진짜 망가진 포지션은 반드시 정리)
    if pl <= hard:
        return f"✂️ 손절 검토 · 손익 {p} (손실 과다)"

    if axis == "trend":
        # 추세로 산 종목: 추세가 살아있으면 보유, 깨지면 손절
        if pl >= tp and trend < 55:
            return f"💰 익절 고려 · 손익 {p} (추세 둔화)"
        if pl <= sl and trend <= 40:
            return f"✂️ 손절 검토 · 손익 {p} (추세 이탈)"
        if pl >= 0 and market_bullish and trend >= 70:
            return f"📈 보유·추가매수 여지 · 손익 {p} (추세 강함)"
        if trend >= 55:
            return f"✅ 보유 지속 · 손익 {p} (추세 유효)"
        return f"⏸️ 관망 · 손익 {p}"

    # axis == "reversion": 추세 낮은 건 당연 → 추세로 손절하지 않음
    rev_sl = sl * 1.6   # 반등은 손절 더 여유(변동성 큼): short≈-13% / mid≈-29%
    if pl >= tp:
        return f"💰 익절 고려 · 손익 {p} (반등 목표 도달)"
    if pl <= rev_sl and rev < 50:
        return f"✂️ 손절 검토 · 손익 {p} (반등 실패)"
    if rev >= 55:
        return f"⏳ 반등 대기 · 손익 {p} (반등 신호 유효)"
    return f"⏸️ 관망 · 손익 {p}"


@st.cache_data(ttl=86400)
def load_marcap() -> dict:
    """종목코드(6자리) → 시가총액(원). 시총 분류용, 1일 캐시."""
    try:
        df = fdr.StockListing("KRX")[["Code", "Marcap"]].dropna()
        return {str(c).zfill(6): int(m) for c, m in zip(df["Code"], df["Marcap"])}
    except Exception:
        return {}


def portfolio_diagnosis(portfolio, market_bullish, horizon="short"):
    """등록된 보유종목 전체를 비중·시총·신호·테마쏠림으로 진단 → markdown.

    개별 종목 신호(위 expander)가 아니라, '이 포트폴리오를 어떻게 굴릴지'를
    실제 보유 데이터(평가비중·시총·추세/반등·치트시트 테마)로 진단한다."""
    mcap = load_marcap()
    tmap = leader_theme_map()
    holds = [it for it in portfolio if it.get("code") and (it.get("quantity", 0) or 0) > 0]
    # 데이터 병렬 로드 — 순차면 FDR 느릴 때 종목수×시간(무한대기). 병렬이면 가장 느린 1개 시간.
    from concurrent.futures import ThreadPoolExecutor
    codes = [it["code"] for it in holds]
    with ThreadPoolExecutor(max_workers=12) as ex:
        dfmap = dict(zip(codes, ex.map(load_stock_data, codes)))
    rows = []
    for it in holds:
        code = it["code"]
        qty = it.get("quantity", 0) or 0
        df = dfmap.get(code)
        if df is None or df.empty:
            continue
        di = add_indicators(df)
        price = int(di.iloc[-1]["Close"])
        t = trend_score(di, horizon)["total"]
        r = reversion_score(di, horizon)["total"]
        oh = overheat_signal(di, horizon)
        buy = it.get("buy_price", 0) or 0
        rows.append({
            "name": it.get("name", code), "val": price * qty, "t": t, "r": r,
            "act": position_action(buy, price, t, r, oh["level"], market_bullish, horizon,
                                   it.get("strategy", "auto")),
            "cap": mcap.get(code, 0), "theme": tmap.get(it.get("name", code), "기타"),
        })
    total = sum(x["val"] for x in rows)
    if not rows or total <= 0:
        return None
    for x in rows:
        x["w"] = x["val"] / total * 100
    rows.sort(key=lambda x: -x["w"])

    L = [f"**📊 내 포트폴리오 진단** · {len(rows)}종목 · 평가 {total:,.0f}원"]
    n = len(rows)

    # ① 비중 배분
    top3 = sum(x["w"] for x in rows[:3])
    L.append("\n**① 비중 배분**")
    L.append(f"- 최대: **{rows[0]['name']} {rows[0]['w']:.0f}%** · 상위3 합 {top3:.0f}%")
    over = [x for x in rows if x["w"] > 20]
    if over:
        L.append("- ⚠️ 20% 초과 과집중: "
                 + ", ".join(f"{x['name']} {x['w']:.0f}%" for x in over) + " → 일부 축소")
    else:
        L.append("- ✅ 한 종목 20% 초과 없음(분산 양호)")
    if n < 5:
        L.append(f"- ⚠️ {n}종목뿐 — 5종목↑ 분산 권장")
    elif n > 15:
        L.append(f"- ⚠️ {n}종목 — 관리 어려움(15개 이내 권장)")
    else:
        L.append(f"- ✅ 종목수 {n}개 — 적정(5~15)")

    # ② 시총 구성
    big = sum(x["w"] for x in rows if x["cap"] >= 5e12)
    mid = sum(x["w"] for x in rows if 1e12 <= x["cap"] < 5e12)
    small = sum(x["w"] for x in rows if 0 < x["cap"] < 1e12)
    unk = sum(x["w"] for x in rows if x["cap"] <= 0)
    L.append("\n**② 시총 구성**")
    seg = f"대형(5조↑) {big:.0f}% · 중형(1~5조) {mid:.0f}% · 소형(1조↓) {small:.0f}%"
    if unk > 1:
        seg += f" · 미상 {unk:.0f}%"
    L.append("- " + seg)
    if small + unk >= 40:
        L.append("- ⚠️ 중소형 비중 높음 — 변동성↑, 하락장엔 대형주 비중 늘리기")
    elif big >= 60:
        L.append("- ✅ 대형주 중심 — 안정적(상승탄력은 제한적)")

    # ③ 신호 움직임
    trend_w = sum(x["w"] for x in rows if x["t"] >= 70)
    rev_w = sum(x["w"] for x in rows if x["r"] >= 70)
    cut = [x for x in rows if x["act"].startswith("✂️")]
    tp = [x for x in rows if x["act"].startswith(("🔥", "💰"))]
    L.append("\n**③ 신호 움직임**")
    L.append(f"- 📈 추세주(70↑) {trend_w:.0f}% · 🔄 반등주(70↑) {rev_w:.0f}%")
    if cut:
        L.append("- ✂️ 손절 신호: " + ", ".join(x["name"] for x in cut) + " → 비중 정리 검토")
    if tp:
        L.append("- 🔥 익절 고려: " + ", ".join(x["name"] for x in tp) + " → 분할 익절")
    if not cut and not tp:
        L.append("- 즉각 정리/익절 신호 없음")

    # ④ 섹터·테마 쏠림 (치트시트 매칭분)
    th = {}
    for x in rows:
        th[x["theme"]] = th.get(x["theme"], 0) + x["w"]
    named = sorted(((k, v) for k, v in th.items() if k != "기타"), key=lambda kv: -kv[1])
    L.append("\n**④ 섹터·테마 쏠림**")
    if named:
        L.append("- " + " · ".join(f"{k} {v:.0f}%" for k, v in named[:3]))
        if named[0][1] >= 35:
            L.append(f"- ⚠️ **{named[0][0]}에 {named[0][1]:.0f}% 집중** — 섹터 악재 시 동반 하락, 분산 고려")
        if th.get("기타", 0) > 1:
            L.append(f"- (치트시트 미매칭 {th['기타']:.0f}%는 분류 제외)")
    else:
        L.append("- 치트시트 대장주와 매칭되는 종목이 없어 테마 분류 생략")

    # ⑤ 레짐 정합성
    L.append("\n**⑤ 레짐 정합성**")
    if market_bullish:
        if trend_w >= 50:
            L.append(f"- 📈 상승장 · 추세주 {trend_w:.0f}% → ✅ 방향 일치(추세 추종 적합)")
        else:
            L.append(f"- 📈 상승장인데 추세주 {trend_w:.0f}%뿐 → 추세 강한 종목 비중 확대 여지")
    else:
        if rev_w >= 40 or big >= 50:
            L.append(f"- 📉 하락·횡보장 · 반등주 {rev_w:.0f}%/대형 {big:.0f}% → ✅ 방어적 구성")
        else:
            L.append(f"- 📉 하락·횡보장인데 추세주 {trend_w:.0f}% 과다 → 비중 축소·현금 확보 권장")

    # ⑥ 스트레스 시나리오 (어떤 충격에 약한가)
    midsmall = small + unk
    growth_themes = {"AI", "반도체", "2차전지", "바이오", "로봇", "우주항공", "양자컴퓨터"}
    growth_w = sum(x["w"] for x in rows if x["theme"] in growth_themes)
    defensive = [x for x in rows if any(k in x["name"]
                 for k in ("텔레콤", "KT&", "금융", "지주", "은행", "보험", "리츠", "가스", "전력", "통신"))]
    def_w = sum(x["w"] for x in defensive)
    L.append("\n**⑥ 스트레스 시나리오** (어떤 충격에 약한가)")
    L.append(f"- 📉 시장 급락: 중소형 {midsmall:.0f}% → "
             + ("변동성 커 깊게 빠질 위험" if midsmall >= 30 else "대형주 중심이라 상대적 방어"))
    L.append(f"- 📈 금리 인상: 성장·고밸류(AI·반도체·2차전지·바이오) {growth_w:.0f}% → "
             + ("밸류 부담으로 타격 가능" if growth_w >= 30 else "민감도 낮은 편"))
    L.append(f"- 🏭 경기 침체: 방어·배당주 {def_w:.0f}% → "
             + ("일부 완충" if def_w >= 15 else "방어주 부족해 취약"))

    # ⑦ 강점·약점 Top3
    strengths, weaknesses = [], []
    if big >= 50:
        strengths.append(f"대형주 {big:.0f}%로 안정성")
    if not over:
        strengths.append("한 종목 20% 초과 없는 분산")
    if 5 <= n <= 15:
        strengths.append(f"적정 종목수({n}개)")
    if market_bullish and trend_w >= 50:
        strengths.append(f"추세주 {trend_w:.0f}%로 상승참여 적극")
    if not cut:
        strengths.append("즉각 손절 위험종목 없음")
    if over:
        weaknesses.append(f"{over[0]['name']} {over[0]['w']:.0f}% 과집중")
    if named and named[0][1] >= 35:
        weaknesses.append(f"{named[0][0]} 테마 {named[0][1]:.0f}% 쏠림")
    if midsmall >= 40:
        weaknesses.append(f"중소형 {midsmall:.0f}%로 변동성 큼")
    if cut:
        weaknesses.append(f"{cut[0]['name']} 손절 신호")
    if growth_w >= 40:
        weaknesses.append(f"성장주 {growth_w:.0f}%로 금리 민감")
    weaknesses.append("100% 국내주식 — 자산군 분산 없음")   # 구조적·항상
    L.append("\n**⑦ 강점·약점 (Top3)**")
    L.append("- 💪 강점: " + (" · ".join(strengths[:3]) if strengths else "뚜렷한 강점 부족"))
    L.append("- 🩹 약점: " + " · ".join(weaknesses[:3]))

    return "\n".join(L)


def longterm_view(portfolio):
    """🏛️ 장기 자산관리 관점 — 단기매매와 *별개의 큰그림* 참고(정성).

    이 시스템은 국내주식 단기매매 전용이라, 멀티에셋·장기배분은 데이터로 못 다룬다.
    대신 '100% 국내주식'이라는 구조적 사실 + 일반 자산배분 원칙을 참고로 제시."""
    names = [it.get("name", "") for it in portfolio if (it.get("quantity", 0) or 0) > 0]
    n = len(names)
    if not n:
        return None
    def_kw = ("텔레콤", "KT&", "금융", "지주", "은행", "보험", "리츠", "전력", "가스", "통신")
    has_def = [nm for nm in names if any(k in nm for k in def_kw)]
    L = ["**🏛️ 장기 자산관리 관점** · 단기매매와 *별개의 큰그림* 참고"]
    L.append("\n**자산군 분산**")
    L.append(f"- 현재 **100% 국내주식**({n}종목). 채권·금·현금·해외가 없어 "
             "**시장 전체 급락(시스템 리스크)에 통째로 노출**됩니다.")
    L.append("- 장기 자금이라면 일반 배분(주식 60~70%·채권/현금 20~30%·금/대체 5~10%)을 참고해 "
             "**나머지 자산군은 별도 계좌/상품으로** 보완 검토.")
    L.append("\n**장기 보완 포인트**")
    if has_def:
        L.append(f"- 방어·배당 성격: {', '.join(has_def[:4])} 보유 → 변동성 완충에 일부 기여")
    else:
        L.append("- ⚠️ 방어·배당주(통신·금융·리츠 등)가 거의 없음 → 인컴/하락 방어가 약함")
    L.append("- 백테스트상 액티브가 코스피를 위험대비 일관 초과 못 함 → 장기 자금 일부는 "
             "**인덱스 ETF 패시브**로 두는 게 합리적.")
    L.append("\n_※ 위는 장기 자산배분 일반 원칙입니다. 이 시스템의 단기 매매신호와는 목적이 달라 "
             "**섞지 말고 별개로** 운용하세요._")
    return "\n".join(L)


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
def compute_daily_scores(df_full: pd.DataFrame, horizon: str = "short") -> list:
    """일별 (date, price, trend, reversion) 리스트. 지표 1회 계산 후 재사용."""
    if df_full is None or df_full.empty or len(df_full) < 80:
        return []
    df_ind = add_indicators(df_full)
    mkt = get_market_uptrend_series()
    out = []
    for i in range(60, len(df_ind)):
        window = df_ind.iloc[: i + 1]
        date = window.index[-1]
        if mkt is not None and not mkt.empty:
            v = mkt.asof(pd.to_datetime(date))
            market_ok = True if pd.isna(v) else bool(v)
        else:
            market_ok = True
        out.append({
            "date": date,
            "price": float(window.iloc[-1]["Close"]),
            "trend": trend_score(window, horizon)["total"],
            "reversion": reversion_score(window, horizon)["total"],
            "market_ok": market_ok,
        })
    return out


def simulate_trades(
    daily_scores: list,
    buy_threshold: int,
    sell_threshold: int,
    initial_capital: float = 1_000_000,
    score_key: str = "trend",
    market_filter: bool = False,
) -> dict:
    """일별 점수로 매수/매도 시뮬. market_filter=True면 KOSPI 상승추세일 때만 매수."""
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
        score = d[score_key]
        price = d["price"]
        date = d["date"]
        if (
            not holding
            and prev_score is not None
            and prev_score < buy_threshold
            and score >= buy_threshold
            and cash > 0
            and (not market_filter or d.get("market_ok", True))
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
    score_key: str = "trend",
    market_filter: bool = False,
) -> list:
    """모든 (매수, 매도) 조합 시뮬 → 결과 리스트. buy > sell 조건만."""
    results = []
    for b in buy_range:
        for s in sell_range:
            if b <= s:
                continue
            sim = simulate_trades(daily_scores, b, s, initial_capital,
                                  score_key=score_key, market_filter=market_filter)
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


def _render_one_backtest(results: list, label: str) -> None:
    """한 전략(추세 또는 반등)의 백테스트 결과 블록."""
    if not results:
        st.caption(f"{label}: 결과 없음")
        return
    results.sort(key=lambda x: (-x["system_return"], x["trades"]))
    best = results[0]

    st.markdown(f"**{label}**")
    c1, c2, c3 = st.columns(3)
    c1.metric("최적 시스템", f"{best['system_return']:+.1f}%")
    c2.metric("그냥 보유(B&H)", f"{best['bh_return']:+.1f}%")
    c3.metric("초과 수익", f"{best['excess']:+.1f}%p")
    st.caption(
        f"최적 조합 매수≥{best['buy']} · 매도≤{best['sell']} · "
        f"거래 {best['trades']}회 · 승률 {best['win_rate']:.0f}% · MDD {best['mdd']:.1f}%"
    )

    # 자동 해석 — 거래 충분한(10회+) 조합 기준
    active = [r for r in results if r["trades"] >= 10]
    active_win = [r for r in active if r["excess"] > 0]
    if not active:
        st.info("📌 거래가 거의 없어 검증 어려움 → 이 전략엔 안 맞는 종목.")
    else:
        ratio = len(active_win) / len(active)
        best_active_excess = max(r["excess"] for r in active)
        msg = f"거래 충분한 {len(active)}개 중 {len(active_win)}개가 보유 초과 → "
        if ratio >= 0.5 and best_active_excess > 10:
            st.success("📌 " + msg + "**이 전략이 통하는 편**")
        elif ratio >= 0.3:
            st.warning("📌 " + msg + "**애매함**")
        else:
            st.error("📌 " + msg + "**보유가 나음(이 전략 불리)**")

    # 자산 곡선 (최적 조합) — 시스템 vs 그냥 보유(B&H) 비교
    bt = best.get("_sim")
    if bt and bt.get("equity"):
        eq_df = pd.DataFrame(bt["equity"])
        fp = eq_df.iloc[0]["price"]
        eq_df["bh_value"] = bt["initial_capital"] * (eq_df["price"] / fp)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=eq_df["date"], y=eq_df["value"],
                                 name="시스템", line=dict(color="#22C55E", width=2)))
        fig.add_trace(go.Scatter(x=eq_df["date"], y=eq_df["bh_value"],
                                 name="그냥 보유", line=dict(color="#A1A1AA", dash="dot")))
        fig.update_layout(height=280, margin=dict(t=20, b=10),
                          yaxis_tickformat=",.0f", yaxis_ticksuffix="원",
                          legend=dict(orientation="h", yanchor="bottom", y=1.0, x=0))
        st.plotly_chart(fig, use_container_width=True, key=f"eq_one_{label}")
        st.caption("초록=이 전략대로 매매 · 회색점선=처음에 사서 계속 보유. 초록이 위면 전략이 이득.")

    with st.expander("📊 전체 조합 결과 (수익률 순)"):
        st.dataframe(
            pd.DataFrame([{
                "매수≥": r["buy"], "매도≤": r["sell"],
                "수익률": f"{r['system_return']:+.1f}%",
                "B&H 대비": f"{r['excess']:+.1f}%p",
                "거래": r["trades"], "승률": f"{r['win_rate']:.0f}%",
                "MDD": f"{r['mdd']:.1f}%",
            } for r in results]),
            use_container_width=True, hide_index=True,
        )


def render_portfolio_backtest(code: str, name: str, period_days: int = 1120, horizon: str = "short") -> None:
    """포트폴리오 행에서 펼치는 백테스트 요약 (추세·반등 각각, 최근 3년)."""
    if not code:
        st.warning("종목 코드가 없어 백테스트할 수 없습니다.")
        return
    with st.spinner(f"{format_stock(name, code)} 백테스트 중..."):
        df_bt = load_stock_data(code, days=period_days)
        if df_bt.empty or len(df_bt) < 80:
            st.warning("백테스트에 충분한 데이터가 없습니다 (최소 80일 필요).")
            return
        scores = compute_daily_scores(df_bt, horizon)
        trend_res = sweep_thresholds(scores, score_key="trend", market_filter=True) if scores else []
        rev_res = sweep_thresholds(scores, score_key="reversion") if scores else []
    if not trend_res and not rev_res:
        st.warning("유효한 백테스트 결과를 만들지 못했습니다.")
        return

    st.markdown(f"**🧪 {format_stock(name, code)} 백테스트 (최근 3년)**{desc_html(code)}", unsafe_allow_html=True)
    t1, t2 = st.tabs(["📈 추세 전략", "🔄 반등 전략"])
    with t1:
        _render_one_backtest(trend_res, "📈 추세 점수 기준")
    with t2:
        _render_one_backtest(rev_res, "🔄 반등 점수 기준")


def _render_full_backtest(results: list, label: str) -> None:
    """백테스트 페이지용 상세 결과 (메트릭 + 자산곡선 + 표 + 거래기록)."""
    if not results:
        st.info(f"{label} 전략: 유효한 조합이 없습니다.")
        return
    results.sort(key=lambda x: (-x["system_return"], x["trades"]))
    best = results[0]
    bt = best["_sim"]

    st.markdown(f"#### 🏆 {label} 최적: 매수 ≥ **{best['buy']}** · 매도 ≤ **{best['sell']}**")
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("시스템 수익률", f"{best['system_return']:+.2f}%")
    m2.metric("매수후보유", f"{best['bh_return']:+.2f}%")
    m3.metric("초과 수익", f"{best['excess']:+.2f}%p")
    m4.metric("거래 횟수", f"{best['trades']}회")
    m5.metric("승률", f"{best['win_rate']:.1f}%")
    st.caption(f"📉 MDD: {best['mdd']:.2f}% · 최종 평가 {int(bt['final_value']):,}원"
               + (" · ⚠️ 종료 시점 보유 중" if bt["holding_at_end"] else ""))

    # 자동 해석
    active = [r for r in results if r["trades"] >= 10]
    aw = [r for r in active if r["excess"] > 0]
    if not active:
        st.info("📌 거래가 거의 없어 검증 어려움 → 이 전략엔 안 맞는 종목.")
    else:
        ratio = len(aw) / len(active)
        bx = max(r["excess"] for r in active)
        m = f"거래 충분한 {len(active)}개 중 {len(aw)}개가 보유 초과 → "
        if ratio >= 0.5 and bx > 10:
            st.success("📌 " + m + "**이 전략이 통하는 편**")
        elif ratio >= 0.3:
            st.warning("📌 " + m + "**애매함**")
        else:
            st.error("📌 " + m + "**보유가 나음(이 전략 불리)**")

    # 자산 곡선
    eq_df = pd.DataFrame(bt["equity"])
    fp = eq_df.iloc[0]["price"]
    eq_df["bh_value"] = bt["initial_capital"] * (eq_df["price"] / fp)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=eq_df["date"], y=eq_df["value"],
                             name="시스템", line=dict(color="#22C55E", width=2)))
    fig.add_trace(go.Scatter(x=eq_df["date"], y=eq_df["bh_value"],
                             name="매수후보유", line=dict(color="#A1A1AA", dash="dot")))
    fig.update_layout(height=360, margin=dict(t=30),
                      yaxis_tickformat=",.0f", yaxis_ticksuffix="원")
    st.plotly_chart(fig, use_container_width=True, key=f"eq_{label}")

    with st.expander(f"📊 전체 {len(results)}개 조합 결과 (수익률 순)"):
        st.dataframe(pd.DataFrame([{
            "매수≥": r["buy"], "매도≤": r["sell"],
            "수익률": f"{r['system_return']:+.2f}%",
            "B&H 대비": f"{r['excess']:+.2f}%p",
            "거래": r["trades"], "승률": f"{r['win_rate']:.1f}%",
            "MDD": f"{r['mdd']:.2f}%",
        } for r in results]), use_container_width=True, hide_index=True, key=f"tbl_{label}")

    if bt["trades"]:
        with st.expander(f"📜 최적 조합 거래 기록 ({len(bt['trades'])}건)"):
            st.dataframe(pd.DataFrame([{
                "매수일": t["entry_date"].strftime("%Y-%m-%d"),
                "매수가": f"{int(t['entry_price']):,}원",
                "매도일": t["exit_date"].strftime("%Y-%m-%d"),
                "매도가": f"{int(t['exit_price']):,}원",
                "수익률": f"{t['profit_pct']:+.2f}%",
            } for t in bt["trades"]]), use_container_width=True, hide_index=True, key=f"trd_{label}")


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
def get_broker_reports_cached(code: str, limit: int = 6) -> list:
    """증권사 리서치 리포트(네이버 금융 리서치) — 공신력 있는 애널리스트 리포트."""
    try:
        from broker_report import broker_reports
        return broker_reports(code, limit)
    except Exception:
        return []


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
def render_analysis_detail(df_ind: pd.DataFrame, result: dict, name: str, code: str, buy_price: float = 0, horizon: str = "short") -> None:
    last = df_ind.iloc[-1]
    prev = df_ind.iloc[-2] if len(df_ind) >= 2 else last
    chg = (last["Close"] - prev["Close"]) / prev["Close"] * 100

    st.markdown(f"## 📌 {format_stock(name, code)}{desc_html(code)}", unsafe_allow_html=True)

    # 펀더멘털 한 줄 요약 (PER/PBR/배당률/EPS)
    fund = get_fundamentals(code)
    st.markdown(f"📊 {format_fundamentals_line(fund)}")

    # 추세/반등 점수 분리 + 자동 판정 (시장 추세 필터 반영)
    tr = trend_score(df_ind, horizon)
    rv = reversion_score(df_ind, horizon)
    _mkt = get_market_regime()
    verdict = dual_verdict(tr["total"], rv["total"], _mkt.get("bullish", True))
    st.caption("⚡ 단기 관점" if horizon == "short" else "📆 중장기(1~3개월) 관점 — MA60/120 기준")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("현재가", f"{int(last['Close']):,}원", f"{chg:+.2f}%")
    c2.metric("📈 추세 점수", f"{tr['total']}/100")
    c3.metric("🔄 반등 점수", f"{rv['total']}/100")
    if buy_price > 0:
        pl = (last["Close"] - buy_price) / buy_price * 100
        c4.metric("평가 손익", f"{pl:+.2f}%")
    else:
        c4.metric("판정", verdict)

    st.markdown(f"#### 신호: {verdict}")
    st.caption("📈 추세=오르는 흐름에 올라타기 · 🔄 반등=많이 빠진 종목의 반등 노리기 (둘은 반대 전략)")

    # 급등 과열 경고 (점수 시스템이 못 잡는 꼭지 보완)
    oh = overheat_signal(df_ind, horizon)
    if oh["level"] >= 1:
        met_conds = " · ".join(desc for ok, desc in oh["conds"] if ok)
        if oh["level"] == 2:
            st.error(f"{oh['text']}  —  {met_conds}\n\n급등 과열 구간입니다. 분할 익절(일부 매도)로 수익을 확정하는 것을 고려하세요.")
        else:
            st.warning(f"{oh['text']}  —  {met_conds}")

    # 내 매수가·손익 기준 행동 제안 (매수가 입력 시)
    if buy_price > 0:
        _act = position_action(buy_price, int(last["Close"]), tr["total"], rv["total"],
                               oh["level"], _mkt.get("bullish", True), horizon)
        if _act:
            st.info(f"👉 **내 포지션 기준 제안:** {_act}")

    cda, cdb = st.columns(2)
    with cda:
        st.markdown("**📈 추세 점수 상세**")
        for k, (s, msg) in tr["details"].items():
            st.write(f"- **{k}**: {s}/5 — {msg}")
    with cdb:
        st.markdown("**🔄 반등 점수 상세**")
        for k, (s, msg) in rv["details"].items():
            st.write(f"- **{k}**: {s}/5 — {msg}")

    st.markdown("### 💡 매매 팁")
    tip_line, glossary_line = make_technical_tip(result)
    st.markdown(f"- {tip_line}")
    if glossary_line:
        st.markdown(
            f"<div style='color:#A1A1AA;font-size:13px;margin-left:14px;margin-top:-6px'>🧭 풀이 — {glossary_line}</div>",
            unsafe_allow_html=True,
        )

    # 증권사 리포트 (공신력 있는 애널리스트 리포트 — 일반 뉴스보다 우선)
    reports = get_broker_reports_cached(code, 6)
    if reports:
        st.markdown("### 📑 증권사 리포트")
        for rp in reports:
            st.markdown(
                f"- **[{rp['증권사']}]** {rp['제목']}"
                + (f" <span style='color:#A1A1AA;font-size:12px'>· {rp['날짜']}</span>" if rp.get('날짜') else ""),
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

    # ===== 재무 · 손익 구조 + 기업 분석 =====
    fin = get_financials(code)
    if fin and fin.get("매출액"):
        st.markdown("### 🏦 재무 · 손익 구조")
        summary = analyze_financials(fin)
        if summary:
            st.info(f"🔍 **기업 분석:** {summary}")
        periods = fin.get("periods", [])
        def _fmt(v, pct=False):
            if v is None:
                return "—"
            return f"{v:.1f}%" if pct else f"{v:,.0f}"
        header = "| 지표 | " + " | ".join(periods) + " |"
        divider = "|---|" + "---|" * len(periods)
        lines = [header, divider]
        for label, key, pct in [
            ("매출액(억)", "매출액", False), ("영업이익(억)", "영업이익", False),
            ("당기순이익(억)", "당기순이익", False), ("영업이익률", "영업이익률", True),
            ("ROE", "ROE", True), ("부채비율", "부채비율", True),
        ]:
            vals = fin.get(key, [])
            if not vals:
                continue
            cells = " | ".join(_fmt(v, pct) for v in vals)
            lines.append(f"| **{label}** | {cells} |")
        st.markdown("\n".join(lines))
        st.caption("단위: 매출·이익=억원 · (E)=증권사 추정치 · 출처: 네이버 금융 기업실적분석")

    # ===== 수급 (투자자별) — 키움(사모 중심) 우선, 없으면 네이버 폴백 =====
    kf = get_kiwoom_supply(code)
    if kf:
        st.markdown("### 💰 수급 (투자자별 · 키움)")
        # ── 당일(장중 실시간 잠정치) 순매수 — 20일 누적에 묻히는 오늘 방향을 최상단 강조 ──
        td = kf.get("today") or {}
        if td:
            dt_s = td["dt"]
            dt_fmt = f"{dt_s[:4]}.{dt_s[4:6]}.{dt_s[6:]}" if len(dt_s) == 8 else dt_s
            # 당일 외국인·기관이 20일 누적과 반대(누적 순매수인데 당일 1만주↑ 매도)면 경고
            th = 10000
            frgn_flip = kf.get("frgn_net", 0) > 0 and td["frgn"] <= -th
            orgn_flip = kf.get("orgn_net", 0) > 0 and td["orgn"] <= -th
            if frgn_flip or orgn_flip:
                who = " · ".join(w for w, f in [("외국인", frgn_flip), ("기관", orgn_flip)] if f)
                st.error(f"⚠️ **당일({dt_fmt}) {who} 대량 순매도** — 20일 누적은 순매수지만 오늘 수급 급반전(장중 잠정치). 최신 흐름 우선 판단!")
            st.markdown(f"**📆 당일 순매수 ({dt_fmt} · 장중 잠정)**")
            tc = st.columns(4)
            tc[0].metric("사모", f"{td['samo']/1e4:+,.1f}만주")
            tc[1].metric("기관계", f"{td['orgn']/1e4:+,.1f}만주")
            tc[2].metric("개인", f"{td['ind']/1e4:+,.1f}만주")
            tc[3].metric("외국인", f"{td['frgn']/1e4:+,.1f}만주")
        st.info(kf["headline"])
        if kf.get("phase"):
            emoji, title, desc = kf["phase"]
            st.markdown(f"**{emoji} {title}** — {desc}")
        st.markdown(f"**📊 20일 누적 (n={kf['n']}일)**")
        cs = st.columns(4)
        cs[0].metric("사모", f"{kf['samo_net']/1e4:+,.0f}만주", help="예측력 1위(검증)")
        cs[1].metric("기관계", f"{kf['orgn_net']/1e4:+,.0f}만주")
        cs[2].metric("개인", f"{kf['ind_net']/1e4:+,.0f}만주", help="−(마이너스)=개미이탈=좋은 신호")
        cs[3].metric("외국인", f"{kf['frgn_net']/1e4:+,.0f}만주", help="예측력 낮음~역신호")
        r5 = kf.get("recent5")
        if r5:
            st.caption(f"↳ 최근 5일 누적: 사모 {r5['samo']:+,} · 기관 {r5['orgn']:+,} · 개인 {r5['ind']:+,} · 외국인 {r5['frgn']:+,} (주)")
        daily = kf.get("daily") or []
        if daily:
            with st.expander(f"📋 일별 순매수 {len(daily)}일 펼쳐보기 (단위: 주 · 최신순)"):
                dl = ["| 날짜 | 사모 | 기관계 | 개인 | 외국인 |", "|---|---|---|---|---|"]
                for d in daily:
                    ds = d["dt"]
                    ds = f"{ds[4:6]}/{ds[6:]}" if len(ds) == 8 else ds
                    dl.append(f"| {ds} | {d['samo']:+,} | {d['orgn']:+,} | {d['ind']:+,} | {d['frgn']:+,} |")
                st.markdown("\n".join(dl))
        st.caption("⚠️ 검증(강세장 100일·생존편향): 사모>금융투자>투신>기관 예측력 · 외국인은 무의미~역신호 · **매수 확신 보조**용. · 당일은 장중 잠정치(마감 후 확정).")
    else:
        sd = get_supply_demand(code)
        if sd and sd.get("rows"):
            st.markdown("### 💰 수급 (외국인 · 기관)")
            f5, i5 = sd["frgn_5"], sd["inst_5"]
            # 5일 누적 방향으로 판단
            if f5 > 0 and i5 > 0:
                judge = "🟢 외국인·기관 **동반 순매수**"
            elif f5 < 0 and i5 < 0:
                judge = "🔴 외국인·기관 **동반 순매도** — 수급 이탈"
            elif f5 > 0:
                judge = "🟡 외국인 순매수 · 기관 순매도"
            elif i5 > 0:
                judge = "🟡 기관 순매수 · 외국인 순매도"
            else:
                judge = "⚪ 뚜렷한 수급 방향 없음"
            st.info(f"{judge}")
            cs1, cs2 = st.columns(2)
            cs1.metric("외국인 5일 누적", f"{f5:+,.0f}주", f"20일 {sd['frgn_20']:+,.0f}")
            cs2.metric("기관 5일 누적", f"{i5:+,.0f}주", f"20일 {sd['inst_20']:+,.0f}")
            with st.expander(f"📋 일별 순매매 {sd['n']}일 펼쳐보기"):
                dl = ["| 날짜 | 종가 | 기관 | 외국인 | 외국인보유율 |", "|---|---|---|---|---|"]
                for row in sd["rows"]:
                    dl.append(
                        f"| {row['date']} | {row['close']:,.0f} | "
                        f"{(row['inst'] or 0):+,.0f} | {(row['frgn'] or 0):+,.0f} | {row['frgn_ratio']} |"
                    )
                st.markdown("\n".join(dl))
            st.caption("단위: 주 · (+)순매수 / (−)순매도 · 출처: 네이버 금융 · ⚠️키움 연결 시 사모까지 분석")

    if tr["total"] >= 55 or rv["total"] >= 55:
        st.markdown("### 💵 매수 가격대 제안")
        st.caption("추세 또는 반등 점수가 충분할 때만 노출됩니다. 분할 매수 시 단계별 진입 가격으로 활용하세요.")
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

    st.markdown("### 📈 차트")
    st.plotly_chart(make_chart(df_ind, format_stock(name, code)), use_container_width=True)

    # 차트 아래: 가장 최근 종가 캔들 분석 (기본 요약은 항상 + 특이사항은 있을 때)
    if len(df_ind) >= 2:
        _last, _prev = df_ind.iloc[-1], df_ind.iloc[-2]
        _o, _c = float(_last["Open"]), float(_last["Close"])
        _pc = float(_prev["Close"])
        _idx = df_ind.index[-1]
        _date_s = _idx.strftime("%m/%d") if hasattr(_idx, "strftime") else str(_idx)
        _chg = (_c - _pc) / _pc * 100 if _pc else 0
        _candle = "🟢 양봉" if _c > _o else ("🔴 음봉" if _c < _o else "⚪ 보합")
        _vma = float(_last.get("VOL_MA20", 0) or 0)
        _vol_s = f" · 거래량 평소 **{float(_last['Volume']) / _vma:.1f}배**" if _vma > 0 else ""
        st.markdown("### 🕯️ 최근 종가 캔들 분석")
        st.markdown(f"📅 **{_date_s}** 종가 **{_c:,.0f}원** "
                    f"(전일 대비 **{_chg:+.1f}%**) · {_candle}{_vol_s}")
        _obs = analyze_candle(df_ind)
        if _obs:
            for _o2 in _obs:
                st.markdown(f"- {_o2}")
        else:
            st.caption("특이 신호 없는 평이한 캔들입니다.")


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


# ===== 모의매수 (가상 매매 장부) =====
SIM_FILE = "sim_portfolio.json"


def load_sim() -> list:
    """모의매수 종목 로드 (개인데이터, gitignore)."""
    if not os.path.exists(SIM_FILE):
        return []
    try:
        with open(SIM_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_sim(items: list) -> None:
    with open(SIM_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


# ===== 분석 기록 저장/로드 =====
def load_history() -> list:
    # 클라우드: Secrets(history_json) 우선 — 개인데이터는 gitignore라 클라우드엔 Secrets로만 전달
    try:
        raw = st.secrets["history_json"]
    except Exception:
        raw = None
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
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
MODES = ["🔍 단일 종목 분석", "📋 포트폴리오 관리", "🌐 테마·이슈", "📜 분석 기록", "🧪 백테스트", "🔭 종목 발굴", "📒 모의매수"]
MODE_KEYS = {"single": MODES[0], "portfolio": MODES[1], "theme": MODES[2], "history": MODES[3], "backtest": MODES[4], "screener": MODES[5], "sim": MODES[6]}
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
# 시장 국면 먼저 계산 → 관점 추천에 사용
_reg = get_market_regime()
# 추천 관점: '확실한 상승'(200선 위+완충대 밖+기울기 상승/유지)일 때만 중장기,
#            중립(전환기)·하락은 방어적으로 단기. → 기울기+완충대 결합
_regime = _reg.get("regime", "중립")
_rec = "mid" if _regime == "상승" else "short"
_rec_label = "📆 중장기" if _rec == "mid" else "⚡ 단기"
_slope_txt = {"up": "200선 상승", "flat": "200선 횡보", "down": "200선 하락"}.get(_reg.get("slope"), "")
if _regime == "상승":
    _rec_why = f"상승 국면({_slope_txt}) → 추세를 길게 태우는 중장기가 유리"
elif _regime == "하락":
    _rec_why = f"하락 국면({_slope_txt}) → 리스크 방어 위해 단기가 유리"
else:
    _rec_why = f"중립·전환기({_slope_txt}, 완충대 안) → 방향 확정 전이라 단기가 안전"

# 투자 관점 토글: 자동(시장따라) / 단기 / 중장기. 신호 점수·행동제안·과열판정에 반영
HZ_LABELS = {"🤖 자동 (시장 따라)": "auto", "⚡ 단기": "short", "📆 중장기": "mid"}
_hz_label = st.sidebar.radio(
    "투자 관점", list(HZ_LABELS.keys()), key="horizon_label",
    help="자동: 시장 국면(KOSPI 200일선)으로 단기/중장기 자동 선택 / "
         "단기: MA5/20 크로스·좁은 손절폭(데이·스윙) / "
         "중장기: MA60/120 크로스·1~3개월 보유 전제 넓은 손절·익절폭",
)
_hz_sel = HZ_LABELS[_hz_label]
HORIZON = _rec if _hz_sel == "auto" else _hz_sel

if _reg.get("ok"):
    st.sidebar.info(f"📊 시장 기준 추천 관점: **{_rec_label}**\n\n{_rec_why}")
    if _hz_sel == "auto":
        st.sidebar.caption(f"🤖 자동 적용 중 → 현재 **{_rec_label}** 관점으로 분석합니다")
    elif _hz_sel != _rec:
        st.sidebar.caption(f"⚠️ 지금은 추천({_rec_label})과 다른 관점을 보고 있어요")
    # 국면 상세: 지수-200선 이격 + 200선 기울기 (완충대 ±2%)
    _gap, _sl = _reg.get("gap_pct", 0), _reg.get("slope_pct", 0)
    _detail = f"지수 {_gap:+.1f}% (200선 대비) · {_slope_txt} ({_sl:+.1f}%/20일)"
    # ⚠️ 급락 경보 최우선: 200선은 후행이라 폭락 중에도 '상승'으로 뜸 → 급락 감지 시 오버라이드 표시
    if _reg.get("crash"):
        _ddp, _m20 = _reg.get("dd_from_peak", 0), _reg.get("mom20", 0)
        st.sidebar.error(
            f"💥 **급락 경보** (200선은 아직 위지만 단기 폭락 중)\n\n"
            f"최근 고점 대비 **{_ddp:+.1f}%** · 20일 **{_m20:+.1f}%**\n\n"
            f"→ 떨어지는 칼날 주의 · 신규매수는 분할·관망")
        st.sidebar.caption(f"(200선 기준 표면 국면: {_regime} · {_detail})")
    elif _regime == "상승":
        st.sidebar.success(f"📈 시장 국면: **상승**\n\n{_detail}")
    elif _regime == "하락":
        st.sidebar.error(f"📉 시장 국면: **하락**\n\n{_detail}")
    else:
        st.sidebar.warning(f"🔄 시장 국면: **중립·전환기**\n\n{_detail}")
    if not _reg.get("bullish"):
        st.sidebar.caption("※ 200일선 아래 → 추세 매수 신호 보류(반등 신호는 유효)")
st.sidebar.caption("완충대 ±2% · 기울기 20일 기준  ·  ⚡단기=데이·스윙  📆중장기=1~3개월")
st.sidebar.caption("⚠️ 본 도구는 참고용입니다. 모든 매매 결정의 책임은 본인에게 있습니다.")


@st.cache_data(ttl=1800)
def _us_overnight_cached():
    return us_market.get_us_overnight()


def render_us_overnight_banner():
    """🌙 간밤 미국 → 오늘 시초가 편향 배너 (정보 참고용, 매매 개입 X)."""
    try:
        us = _us_overnight_cached()
    except Exception:
        return
    if not us or us.get("nasdaq", {}).get("chg") is None:
        return
    gb = us.get("gap_bias")
    box = st.error if (gb is not None and gb <= -1.0) else (
        st.success if (gb is not None and gb >= 1.0) else st.info)
    c = st.columns([1, 1, 1, 1, 2])
    for col, key in zip(c[:4], ("sp", "nasdaq", "sox", "vix")):
        m = us[key]
        if key == "vix":
            col.metric(m["label"], f"{m['value']:.0f}" if m["value"] else "—",
                       f"{m['chg']:+.1f}%" if m["chg"] is not None else None)
        else:
            col.metric(m["label"], f"{m['chg']:+.1f}%" if m["chg"] is not None else "—")
    with c[4]:
        box(f"🌙 간밤 미국 ({us.get('date','')}) → {us_market.bias_text(us)}")
        rt = us_market.risk_text(us)
        if rt:
            st.caption(rt)


# ===== 메인 화면 =====
# 💥 급락 경보: 200선 국면판정이 놓치는 단기 폭락을 메인 상단에 크게 표시
if _reg.get("crash"):
    st.error(
        f"💥 **급락 경보** — 코스피 최근 고점 대비 **{_reg.get('dd_from_peak',0):+.1f}%** · "
        f"20일 **{_reg.get('mom20',0):+.1f}%** 폭락 중 "
        f"(200일선은 아직 위라 표면 국면은 '{_regime}'이지만 후행 지표라 급락을 못 잡음)  \n"
        f"→ 떨어지는 칼날 주의. 신규매수는 **분할·관망**. 역대 통계상 급락 깊을수록 이후 회복폭↑이나 **길게(3~6개월) 봐야**.")
with st.expander("🌙 간밤 미국증시 → 오늘 시초가 참고", expanded=True):
    render_us_overnight_banner()
    st.caption("미국 영향은 시초가 갭에 대부분 반영(상관 0.57)·장중은 무관(0.01). 매매는 참고만.")
if mode == "🔍 단일 종목 분석":
    st.title("🔍 단일 종목 분석")

    market_news = get_market_news(3)
    if market_news:
        with st.container(border=True):
            st.markdown("#### 📰 오늘의 증시 주요 뉴스 Top 3")
            st.caption("주가 흐름에 영향을 줄 만한 거시·시장 이슈 (Google News · 30분 캐시)")
            for idx, n in enumerate(market_news, 1):
                ago = _relative_time(n.get("pub", ""))
                meta = " · ".join(x for x in [n["source"], ago] if x)
                meta = f"  <span style='color:#A1A1AA;font-size:12px'>· {meta}</span>" if meta else ""
                if n["link"]:
                    st.markdown(f"**{idx}.** [{n['title']}]({n['link']}){meta}", unsafe_allow_html=True)
                else:
                    st.markdown(f"**{idx}.** {n['title']}{meta}", unsafe_allow_html=True)
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
            st.session_state.pop("single_target", None)
        else:
            # 새 분석일 때만 기록 저장 (관점 토글 재실행 때는 중복저장 안 함)
            df_ind = add_indicators(df)
            _t = trend_score(df_ind, HORIZON)["total"]
            _r = reversion_score(df_ind, HORIZON)["total"]
            last = df_ind.iloc[-1]
            prev = df_ind.iloc[-2] if len(df_ind) >= 2 else last
            chg = (last["Close"] - prev["Close"]) / prev["Close"] * 100
            append_history({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "code": code,
                "name": name,
                "price": int(last["Close"]),
                "change_pct": round(chg, 2),
                "score": max(_t, _r),
                "verdict": dual_verdict(_t, _r, get_market_regime().get("bullish", True)),
                "mode": "단일",
            })
            # 분석 대상 기억 → 관점(단기/중장기) 토글 시 재분석 없이 즉시 반영
            st.session_state["single_target"] = {"code": code, "name": name, "buy": int(buy_price)}

    # 저장된 분석 대상을 매 재실행마다 현재 HORIZON으로 렌더 (라디오만 바꿔도 갱신)
    _tgt = st.session_state.get("single_target")
    if _tgt:
        _df = load_stock_data(_tgt["code"])
        if not _df.empty:
            _df_ind = add_indicators(_df)
            _res = score_signal(_df_ind)
            render_analysis_detail(_df_ind, _res, _tgt["name"], _tgt["code"], _tgt["buy"], HORIZON)

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

    st.caption("📈 추세 전략과 🔄 반등 전략을 따로 백테스트해, 이 종목에 어느 쪽이 맞는지 비교합니다.")
    if st.button("최적 임계점 분석", type="primary"):
        code, name, _ = resolve_stock(bt_query)
        with st.spinner(f"{format_stock(name, code)} 데이터 가져오는 중..."):
            df = load_stock_data(code, days=days_map[bt_period])
        if df.empty or len(df) < 80:
            st.error("백테스트에 충분한 데이터가 없습니다 (최소 80일 필요).")
        else:
            with st.spinner("일별 점수 계산..."):
                scores = compute_daily_scores(df, HORIZON)
            if not scores:
                st.error("점수 계산 실패")
            else:
                with st.spinner("임계점 조합 시뮬 중..."):
                    trend_res = sweep_thresholds(scores, score_key="trend", market_filter=True)
                    rev_res = sweep_thresholds(scores, score_key="reversion")
                st.markdown(f"### {format_stock(name, code)} · {bt_period}{desc_html(code)}", unsafe_allow_html=True)
                tt, tr = st.tabs(["📈 추세 전략", "🔄 반등 전략"])
                with tt:
                    _render_full_backtest(trend_res, "추세")
                with tr:
                    _render_full_backtest(rev_res, "반등")
                st.warning(
                    "⚠️ 과거 데이터에 최적화된 결과로 미래 수익을 보장하지 않습니다. "
                    "여러 종목·여러 기간으로 교차 검증하셔야 의미가 있습니다 (과최적화 위험)."
                )

elif mode == "🌐 테마·이슈":
    def _theme_show_detail(name):
        """장기자산 종목명 클릭 → 코드 변환 후 풀 상세 분석 렌더 (현재 관점 반영)."""
        code, nm, found = resolve_stock(name)
        if not found:
            st.warning(f"'{name}' 종목을 찾지 못했습니다. (종목명이 바뀌었을 수 있어요)")
            return
        _df = load_stock_data(code)
        if _df.empty:
            st.error(f"{nm} 데이터를 가져올 수 없습니다.")
            return
        _di = add_indicators(_df)
        render_analysis_detail(_di, score_signal(_di), nm, code, 0, HORIZON)

    render_theme_tracker(
        get_market_news=get_market_news,
        load_stock_data=load_stock_data,
        add_indicators=add_indicators,
        trend_score=trend_score,
        reversion_score=reversion_score,
        dual_verdict=dual_verdict,
        overheat_signal=overheat_signal,
        market_bullish=get_market_regime().get("bullish", True),
        show_detail=_theme_show_detail,
    )

elif mode == "🔭 종목 발굴":
    def _screener_show_detail(code, name):
        """발굴·관심종목 클릭 → 단일 종목 분석과 동일한 풀 상세(재무·수급·매수가격대 포함)."""
        _df = load_stock_data(code)
        if _df is None or _df.empty:
            st.error(f"{name}({code}) 데이터를 가져올 수 없습니다.")
            return
        _di = add_indicators(_df)
        render_analysis_detail(_di, score_signal(_di), name, code, 0, HORIZON)

    def _add_to_sim(code, name, qty, price=0, note=""):
        """매집 카드 → 모의매수 바로 담기. price=0이면 오늘 종가 자동. 성공 시 True."""
        bp = int(price)
        if bp <= 0:
            _df = load_stock_data(code)
            bp = int(_df.iloc[-1]["Close"]) if _df is not None and not _df.empty else 0
        if bp <= 0:
            return False
        _sim = load_sim()
        _sim.append({
            "code": code, "name": name, "buy_price": bp, "quantity": int(qty),
            "buy_date": datetime.now().date().isoformat(), "note": note,
            "added_at": datetime.now().isoformat(),
        })
        save_sim(_sim)
        return True

    render_screener(
        load_stock_data=load_stock_data,
        add_indicators=add_indicators,
        score_signal=score_signal,
        make_chart=make_chart,
        trend_score=trend_score,
        reversion_score=reversion_score,
        momentum_score=momentum_score,
        get_fundamentals=get_fundamentals,
        show_detail=_screener_show_detail,
        market_regime=get_market_regime(),
        add_to_sim=_add_to_sim,
    )

elif mode == "📒 모의매수":
    st.title("📒 모의매수 시뮬레이션")
    st.caption(
        "발굴한 매집 종목을 **'샀다 치고'** 담아 실제 계좌처럼 손익을 추적하는 **가상 장부**입니다. "
        "실제 돈은 안 나갑니다. 매집 발굴+매수가 실제로 먹히는지 시간을 두고 검증하세요. "
        "각 매수일부터 **KODEX200(지수) 대비 초과수익**도 함께 계산합니다."
    )
    sim = load_sim()

    def _sim_detail(code, name):
        _df = load_stock_data(code)
        if _df is None or _df.empty:
            st.error(f"{name}({code}) 데이터를 가져올 수 없습니다.")
            return
        _di = add_indicators(_df)
        render_analysis_detail(_di, score_signal(_di), name, code, 0, HORIZON)

    # ----- 추가 폼 -----
    with st.form("sim_add", clear_on_submit=True):
        c1, c2, c3, c4 = st.columns([2.2, 1, 1.3, 1.3])
        q = c1.text_input("종목 코드/이름", placeholder="예: 008930 또는 한미사이언스")
        qty = c2.number_input("수량", min_value=0, step=1)
        price = c3.number_input("매수가(0=매수일 종가 자동)", min_value=0, step=100)
        buy_date = c4.date_input("매수일", value=datetime.now().date())
        note = st.text_input("메모(선택) — 왜 담았는지", placeholder="예: 저변동 매집·체결강도 155")
        if st.form_submit_button("➕ 모의매수 담기", type="primary"):
            if q and qty > 0:
                code, name, found = resolve_stock(q)
                if not found:
                    st.error(f"'{q}' 종목을 찾을 수 없습니다. 코드/이름을 확인하세요.")
                else:
                    bp = int(price)
                    if bp <= 0:   # 매수가 미입력 → 매수일 종가 자동
                        try:
                            _bd = fdr.DataReader(code, buy_date)
                            bp = int(_bd.iloc[0]["Close"]) if _bd is not None and not _bd.empty else 0
                        except Exception:
                            bp = 0
                    if bp <= 0:
                        st.error("매수일 종가를 가져오지 못했습니다. 매수가를 직접 입력해 주세요.")
                    else:
                        sim.append({
                            "code": code, "name": name, "buy_price": bp, "quantity": int(qty),
                            "buy_date": buy_date.isoformat(), "note": note.strip(),
                            "added_at": datetime.now().isoformat(),
                        })
                        save_sim(sim)
                        st.success(f"{format_stock(name, code)} {int(qty)}주 @ {bp:,}원 담음")
                        st.rerun()
            else:
                st.warning("종목과 수량(1주 이상)을 입력하세요.")

    if not sim:
        st.info("아직 모의매수 종목이 없습니다. 위에서 담아보세요. "
                "(🔭 종목 발굴 → 매집 종목의 코드를 여기에 추가)")
    else:
        # KODEX200 벤치마크 시계열(넉넉히 로드) — 각 매수일부터 지금까지 수익
        kodex = load_stock_data("069500", days=1500)

        def _kodex_ret(from_date):
            if kodex is None or kodex.empty:
                return None
            try:
                base = kodex[kodex.index >= pd.Timestamp(from_date)]
                if base.empty:
                    return None
                return float(kodex.iloc[-1]["Close"] / base.iloc[0]["Close"] - 1)
            except Exception:
                return None

        rows = []
        tot_cost = tot_val = 0
        bench_num = 0.0   # 투자금 가중 벤치마크 수익 합
        for it in sim:
            code = it.get("code", ""); name = it.get("name", "")
            bp = it.get("buy_price", 0) or 0; qty = it.get("quantity", 0) or 0
            df = load_stock_data(code) if code else pd.DataFrame()
            cur = int(df.iloc[-1]["Close"]) if df is not None and not df.empty else 0
            cost = bp * qty; val = cur * qty
            pl = val - cost; plpct = (pl / cost * 100) if cost else 0
            tot_cost += cost; tot_val += val
            br = _kodex_ret(it.get("buy_date"))
            if br is not None:
                bench_num += br * cost
            try:
                held = (datetime.now().date() - datetime.fromisoformat(it["buy_date"]).date()).days
            except Exception:
                held = 0
            rows.append({**it, "cur": cur, "cost": cost, "val": val, "pl": pl,
                         "plpct": plpct, "bench": (br * 100 if br is not None else None),
                         "held": held})

        # ----- 요약 -----
        tot_pl = tot_val - tot_cost
        tot_plpct = (tot_pl / tot_cost * 100) if tot_cost else 0
        bench_pct = (bench_num / tot_cost * 100) if tot_cost else 0
        alpha = tot_plpct - bench_pct
        m = st.columns(4)
        m[0].metric("총 투자금(모의)", f"{tot_cost:,}원")
        m[1].metric("현재 평가금", f"{tot_val:,}원", f"{tot_pl:+,}원")
        m[2].metric("모의 수익률", f"{tot_plpct:+.1f}%")
        m[3].metric("지수(KODEX200) 대비", f"{alpha:+.1f}%p", f"지수 {bench_pct:+.1f}%")
        st.caption("⚠️ 참고용 시뮬레이션 · 매집 신호는 워치리스트 참고(자동매매 아님). "
                   "지수대비는 각 매수일부터 계산·투자금 가중.")
        st.markdown("---")

        # ----- 종목별 카드 -----
        for i, r in enumerate(rows):
            with st.container(border=True):
                cols = st.columns([2.6, 1.5, 1.6, 1.9, 0.7])
                sel = st.session_state.get("sim_detail") == r["code"]
                if cols[0].button(format_stock(r.get("name", ""), r["code"]),
                                  key=f"sim_b_{i}", use_container_width=True,
                                  type="primary" if sel else "secondary"):
                    st.session_state["sim_detail"] = None if sel else r["code"]
                    st.rerun()
                _d = stock_desc(r["code"])
                if _d:
                    cols[0].caption(_d)
                if r.get("note"):
                    cols[0].caption(f"📝 {r['note']}")
                cols[1].caption("매수가 × 수량")
                cols[1].write(f"{r['buy_price']:,} × {r['quantity']}")
                cols[2].caption(f"현재가 · 보유 {r['held']}일")
                cols[2].write(f"{r['cur']:,}원")
                cols[3].caption("평가손익")
                cols[3].write(f"{r['pl']:+,}원 ({r['plpct']:+.1f}%)")
                if r["bench"] is not None:
                    cols[3].caption(f"지수 {r['bench']:+.1f}% · 초과 {r['plpct'] - r['bench']:+.1f}%p")
                if cols[4].button("🗑️", key=f"sim_del_{i}", help="삭제"):
                    if st.session_state.get("sim_detail") == r["code"]:
                        st.session_state["sim_detail"] = None
                    sim.pop(i); save_sim(sim); st.rerun()
                if sel:
                    _sim_detail(r["code"], r.get("name", ""))

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
        with st.expander("📖 신호 보는 법 (추세/반등)"):
            st.markdown(
                "각 종목은 **📈 추세 점수**와 **🔄 반등 점수**(0~100)로 따로 평가됩니다.\n\n"
                "- **📈 추세 점수 높음** → 오르는 흐름에 올라타는 전략에 적합 (정배열·골든크로스·건강한 RSI·거래량↑)\n"
                "- **🔄 반등 점수 높음** → 많이 빠진 종목의 반등을 노리는 전략에 적합 (과매도·볼린저 하단·낙폭 과대·반등 시작)\n"
                "- 둘은 **반대 전략**이라, 종목마다 어느 쪽이 맞는지 다릅니다. 🧪 백테스트로 확인하세요.\n\n"
                "판정: 추세 70↑ → **📈 추세 매수**, 반등 70↑ → **🔄 반등 매수**, "
                "55↑ → 양호/주목, 둘 다 낮으면 **⏸️ 관망**."
            )

        _hz_name = "단기 트레이딩" if HORIZON == "short" else "중장기(몇 주)"
        with st.expander(f"🧭 내 포트폴리오 진단 — {_hz_name}", expanded=True):
            st.caption("이 시스템의 본령. 보유종목 **전체**를 비중·시총·신호·테마쏠림·스트레스로 진단합니다.")
            with st.spinner("보유종목 분석 중..."):
                _diag = portfolio_diagnosis(portfolio, get_market_regime().get("bullish", True), HORIZON)
            if _diag:
                st.markdown(_diag)
            else:
                st.caption("진단할 보유종목(수량>0)이 없습니다.")

        with st.expander("🏛️ 장기 자산관리 관점 (참고 — 단기매매와 별개)"):
            st.caption("⚠️ 이 시스템은 국내주식 단기매매 전용입니다. 아래는 자산관리사 시각의 *큰그림 참고*이며, 매매 신호와 섞지 마세요.")
            _lt = longterm_view(portfolio)
            if _lt:
                st.markdown(_lt)
            else:
                st.caption("진단할 보유종목이 없습니다.")

        # 데스크톱: 표 헤더 (모바일은 카드형이라 헤더 생략)
        col_widths = [2.4, 0.5, 3, 2, 1, 2, 0.6, 0.6, 0.6]
        if not mobile:
            h = st.columns(col_widths, vertical_alignment="center")
            h[0].caption("종목")
            h[2].caption("매매 신호")
            h[3].caption("매수가")
            h[4].caption("수량")
            h[5].caption("평가금액")

        detail_idx = st.session_state.get("portfolio_detail")
        edit_idx = st.session_state.get("portfolio_edit")
        bt_idx = st.session_state.get("portfolio_backtest")
        _mkt_bull = get_market_regime().get("bullish", True)
        for i, item in enumerate(portfolio):
            label = format_stock(item.get("name", ""), item.get("code", ""))

            # 종목별 분석 (캐시되어 있으니 재호출은 빠름)
            code = item.get("code", "")
            df_data = load_stock_data(code) if code else pd.DataFrame()
            if not df_data.empty:
                df_ind = add_indicators(df_data)
                result = score_signal(df_ind)
                _t = trend_score(df_ind, HORIZON)["total"]
                _r = reversion_score(df_ind, HORIZON)["total"]
                _oh = overheat_signal(df_ind, HORIZON)
                cur_price = int(df_ind.iloc[-1]["Close"])
                _buy = item.get("buy_price", 0) or 0
                _action = position_action(_buy, cur_price, _t, _r, _oh["level"], _mkt_bull, HORIZON,
                                          item.get("strategy", "auto"))
                if _action:
                    # 매수가 있으면 내 포지션 기준 행동 제안 우선
                    signal_text = f"{_action}  (📈{_t} 🔄{_r})"
                else:
                    _oh_tag = (_oh["text"] + " · ") if _oh["level"] == 2 else ("⚠️과열 · " if _oh["level"] == 1 else "")
                    signal_text = f"{_oh_tag}{dual_verdict(_t, _r, _mkt_bull)} (📈{_t} 🔄{_r})"
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
                            st.session_state.pop("portfolio_backtest", None)
                        st.rerun()
                    _desc = stock_desc(code)
                    if _desc:
                        st.caption(_desc)
                    st.markdown(
                        "<div style='font-size:13px;line-height:1.9;color:var(--ds-text-muted)'>"
                        f"🔔 {signal_text}<br>"
                        f"매수가 <b style='color:var(--ds-text)'>{buy:,}</b> · "
                        f"수량 <b style='color:var(--ds-text)'>{qty}</b> · "
                        f"평가 <b style='color:var(--ds-text)'>{eval_text}</b>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                    bc = st.columns(3)
                    if bc[0].button("🧪 백테스트", key=f"bt_{i}", use_container_width=True):
                        if bt_idx == i:
                            st.session_state.pop("portfolio_backtest", None)
                        else:
                            st.session_state["portfolio_backtest"] = i
                            st.session_state.pop("portfolio_detail", None)
                            st.session_state.pop("portfolio_edit", None)
                        st.rerun()
                    if bc[1].button("✏️ 수정", key=f"edit_{i}", use_container_width=True):
                        if edit_idx == i:
                            st.session_state.pop("portfolio_edit", None)
                        else:
                            st.session_state["portfolio_edit"] = i
                            st.session_state.pop("portfolio_detail", None)
                            st.session_state.pop("portfolio_backtest", None)
                        st.rerun()
                    if bc[2].button("🗑️ 삭제", key=f"del_{i}", use_container_width=True):
                        portfolio.pop(i)
                        save_portfolio(portfolio)
                        st.session_state.pop("portfolio_detail", None)
                        st.session_state.pop("portfolio_edit", None)
                        st.session_state.pop("portfolio_backtest", None)
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
                        st.session_state.pop("portfolio_backtest", None)
                    st.rerun()
                _desc = stock_desc(code)
                if _desc:
                    cols[0].caption(_desc)
                # cols[1]은 종목 ↔ 매매신호 사이 여백 (spacer)
                cols[2].write(signal_text)
                cols[3].write(f"{buy:,}")
                cols[4].write(f"{qty}")
                cols[5].write(eval_text)
                if cols[6].button("🧪", key=f"bt_{i}", help="백테스트"):
                    if bt_idx == i:
                        st.session_state.pop("portfolio_backtest", None)
                    else:
                        st.session_state["portfolio_backtest"] = i
                        st.session_state.pop("portfolio_detail", None)
                        st.session_state.pop("portfolio_edit", None)
                    st.rerun()
                if cols[7].button("✏️", key=f"edit_{i}", help="매수가·수량 수정"):
                    if edit_idx == i:
                        st.session_state.pop("portfolio_edit", None)
                    else:
                        st.session_state["portfolio_edit"] = i
                        st.session_state.pop("portfolio_detail", None)
                        st.session_state.pop("portfolio_backtest", None)
                    st.rerun()
                if cols[8].button("🗑️", key=f"del_{i}", help="삭제"):
                    portfolio.pop(i)
                    save_portfolio(portfolio)
                    st.session_state.pop("portfolio_detail", None)
                    st.session_state.pop("portfolio_edit", None)
                    st.session_state.pop("portfolio_backtest", None)
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
                            item.get("buy_price", 0), HORIZON,
                        )

            # 백테스트 버튼 클릭 시 행 바로 아래에 요약 펼치기
            if bt_idx == i:
                with st.container(border=True):
                    render_portfolio_backtest(item.get("code", ""), item.get("name", ""), horizon=HORIZON)
    else:
        st.info("아직 등록된 종목이 없습니다.")
