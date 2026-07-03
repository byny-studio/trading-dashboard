"""
장 마감 후 포트폴리오 신호 점검 → 강한 신호가 있으면 Discord로 알림.
GitHub Actions(cron)에서 실행. 앱 로직(추세/반등/과열/시장필터)을 자체 재현.
시장 국면(200선 위치+완충대±2%+기울기)으로 단기/중장기 관점을 자동선택해 점수·제안에 반영.

환경변수:
  DISCORD_WEBHOOK  : 디스코드 웹훅 URL
  PORTFOLIO_JSON   : 보유종목 JSON 배열 문자열 (portfolio.json 내용)
"""
import os
import json
import warnings
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import requests
import FinanceDataReader as fdr


# 뉴스 → 테마 키워드 (테마명, [키워드], 업종, [대표 대장주])
THEME_KEYWORDS = [
    ("금리/성장주", ["금리", "인하", "인상", "FOMC", "연준", "기준금리", "동결", "파월"], "반도체·바이오·2차전지", ["삼성전자", "SK하이닉스", "NAVER"]),
    ("AI/반도체", ["AI", "인공지능", "엔비디아", "데이터센터", "HBM", "반도체", "메모리", "D램", "TSMC", "GPU"], "반도체·전력설비", ["삼성전자", "SK하이닉스", "한미반도체"]),
    ("환율/수출주", ["환율", "원달러", "원/달러", "달러", "강달러", "약달러"], "반도체·자동차·조선", ["삼성전자", "현대차", "HD현대중공업"]),
    ("유가/에너지", ["유가", "원유", "OPEC", "WTI", "정유", "감산"], "정유·LNG / 항공·운송", ["S-Oil", "SK이노베이션", "GS"]),
    ("방산", ["방산", "무기", "국방", "K방산", "한화에어로", "미사일"], "방산", ["한화에어로스페이스", "LIG넥스원", "현대로템"]),
    ("전쟁/재건", ["전쟁", "종전", "휴전", "이스라엘", "이란", "우크라", "러시아", "재건", "정전"], "방산·건설·철강", ["한화에어로스페이스", "현대건설", "대우건설"]),
    ("원전/SMR", ["원전", "SMR", "원자력", "소형모듈"], "원전 기자재", ["두산에너빌리티", "한전기술", "비에이치아이"]),
    ("2차전지", ["2차전지", "전기차", "배터리", "양극재", "리튬", "전고체"], "2차전지", ["LG에너지솔루션", "에코프로비엠", "POSCO홀딩스"]),
    ("조선", ["조선", "선박", "수주", "LNG선"], "조선", ["HD한국조선해양", "삼성중공업", "한화오션"]),
    ("중국소비", ["중국", "위안", "단체관광", "면세", "유커", "광군제"], "화장품·면세점·엔터", ["아모레퍼시픽", "호텔신라", "신세계"]),
    ("바이오", ["바이오", "신약", "임상", "FDA", "제약", "백신"], "바이오", ["삼성바이오로직스", "셀트리온", "알테오젠"]),
    ("건설/부동산", ["건설", "부동산", "재건축", "SOC", "주택", "분양"], "건설·시멘트", ["현대건설", "GS건설", "대우건설"]),
    ("전력설비", ["전력", "변압기", "송전", "전선", "그리드", "전력망"], "변압기·전선", ["HD현대일렉트릭", "LS ELECTRIC", "효성중공업"]),
    ("우주항공", ["우주", "발사체", "누리호", "위성", "항공우주"], "항공우주", ["한화에어로스페이스", "한국항공우주", "쎄트렉아이"]),
    ("로봇", ["로봇", "휴머노이드", "자동화"], "자동화", ["두산로보틱스", "레인보우로보틱스", "에스피지"]),
    ("코인/블록체인", ["비트코인", "가상자산", "코인", "블록체인", "가상화폐", "이더리움"], "거래소·블록체인", ["우리기술투자", "한화투자증권", "위지트"]),
    ("금", ["금값", "금 가격", "안전자산", "골드"], "금 관련주", ["고려아연", "엘컴텍", "이그잭스"]),
]


def fetch_news_titles(limit=35):
    """최근 2일 시장 뉴스 제목 (Google News RSS)."""
    q = "코스피 OR 증시 OR 금리 OR 환율 OR 전쟁 OR 유가 when:2d"
    url = ("https://news.google.com/rss/search?q="
           + urllib.parse.quote(q) + "&hl=ko&gl=KR&ceid=KR:ko")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
        root = ET.fromstring(r.content)
    except Exception:
        return []
    titles = []
    for item in root.findall(".//item")[:limit]:
        t = (item.findtext("title") or "").strip()
        if " - " in t:
            t = t.rsplit(" - ", 1)[0].strip()
        if t:
            titles.append(t)
    return titles


def analyze_news_themes(titles):
    out = []
    for theme, kws, sectors, leaders in THEME_KEYWORDS:
        hits = sum(1 for t in titles if any(k in t for k in kws))
        if hits:
            out.append({"theme": theme, "count": hits, "sectors": sectors, "leaders": leaders})
    out.sort(key=lambda x: x["count"], reverse=True)
    return out


def get_naver_themes():
    """네이버 테마 시세 (등락률+주도주). [{name,chg,chg3,leads}]."""
    from bs4 import BeautifulSoup
    themes = []
    for page in (1, 2, 3, 4, 5, 6):
        try:
            r = requests.get(
                f"https://finance.naver.com/sise/theme.naver?&page={page}",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            r.encoding = "euc-kr"
            rows = BeautifulSoup(r.text, "html.parser").select("table.type_1 tr")
        except Exception:
            break
        found = False
        for tr in rows:
            a = tr.select_one("td.col_type1 a")
            tds = tr.select("td")
            if not a or len(tds) < 8:
                continue

            def _pct(t):
                try:
                    return float(t.replace("%", "").replace("+", "").replace(",", ""))
                except Exception:
                    return None

            href = a.get("href", "")
            leads = [x.get_text(strip=True) for x in (tds[6].select("a") + tds[7].select("a"))]
            themes.append({
                "name": a.get_text(strip=True),
                "no": href.split("no=")[-1] if "no=" in href else "",
                "chg": _pct(tds[1].get_text(strip=True)),
                "chg3": _pct(tds[2].get_text(strip=True)),
                "leads": [x for x in leads if x][:3],
            })
            found = True
        if not found:
            break
    return [t for t in themes if t["chg"] is not None]


def theme_volume_ratio(no):
    """테마 구성종목 거래량합 ÷ 전일거래량합."""
    from bs4 import BeautifulSoup
    if not no:
        return None
    try:
        r = requests.get(
            f"https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no={no}",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.encoding = "euc-kr"
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

    def num(t):
        try:
            return int(t.replace(",", ""))
        except Exception:
            return 0

    tv = tp = 0
    for tr in soup.select("table.type_5 tbody tr"):
        a = tr.select_one("a")
        if not a or "code=" not in a.get("href", ""):
            continue
        tds = [td.get_text(strip=True) for td in tr.select("td")]
        if len(tds) < 10:
            continue
        tv += num(tds[7])
        tp += num(tds[9])
    return (tv / tp) if tp else None


def early_momentum_themes(themes, top=3, min_vol=1.2):
    """막 살아나는 테마: 3일 상승 + 오늘 가속 + 아직 +4% 미만 + 거래량 증가."""
    cand = [t for t in themes if t["chg3"] is not None
            and t["chg3"] > 0 and 0 < t["chg"] < 4 and t["chg"] > t["chg3"] / 3.0]
    cand.sort(key=lambda x: x["chg"] - x["chg3"] / 3.0, reverse=True)
    out = []
    for t in cand[:15]:
        vr = theme_volume_ratio(t.get("no", ""))
        if vr is None or vr < min_vol:
            continue
        out.append({**t, "vol": vr})
        if len(out) >= top:
            break
    return out


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


def trend_score(df, horizon="short"):
    """단기(MA5/20)/중장기(MA20/60) 추세 점수. app.py trend_score와 동일 규칙(정수 반환판)."""
    if df.empty or len(df) < 60:
        return 0
    mid = horizon == "mid"
    fast_n, slow_n = (60, 120) if mid else (5, 20)
    last = df.iloc[-1]
    v = []
    mas = [last.get(f"MA{n}") for n in (5, 20, 60, 120)]
    if all(pd.notna(mas)):
        if mid:
            v.append(5 if mas[1] > mas[2] > mas[3] else 4 if mas[1] > mas[2]
                     else 0 if mas[1] < mas[2] < mas[3] else 2)
        else:
            v.append(5 if mas[0] > mas[1] > mas[2] > mas[3] else 4 if mas[0] > mas[1] > mas[2]
                     else 3 if mas[0] > mas[1] else 0 if mas[0] < mas[1] < mas[2] < mas[3] else 2)
    else:
        v.append(2)
    cf, cs = f"MA{fast_n}", f"MA{slow_n}"
    mf, ms = df[cf].iloc[-1], df[cs].iloc[-1]
    mfp, msp = df[cf].iloc[-2], df[cs].iloc[-2]
    v.append(5 if (mfp <= msp and mf > ms) else 0 if (mfp >= msp and mf < ms) else 4 if mf > ms else 1)
    lo, hi = (45, 75) if mid else (50, 70)
    rsi = last.get("RSI")
    v.append((5 if lo <= rsi < hi else 3 if rsi >= hi else 2 if (lo - 10) <= rsi < lo else 0) if pd.notna(rsi) else 2)
    close = last["Close"]
    bu, bl = last.get("BB_Upper"), last.get("BB_Lower")
    pos = (close - bl) / (bu - bl) if (pd.notna(bu) and bu != bl) else 0.5
    v.append(5 if 0.5 <= pos < 0.8 else 4 if pos >= 0.8 else 2 if 0.3 <= pos < 0.5 else 0)
    vma = last.get("VOL_MA20")
    r = last["Volume"] / vma if (pd.notna(vma) and vma > 0) else 1
    v.append(5 if r >= 2 else 4 if r >= 1.3 else 3 if r >= 0.8 else 1)
    return sum(v) * 4


def reversion_score(df, horizon="short"):
    """단기(MA20 이격)/중장기(MA60 이격, 더 깊게) 반등 점수. app.py와 동일 규칙(정수 반환판)."""
    if df.empty or len(df) < 60:
        return 0
    mid = horizon == "mid"
    last = df.iloc[-1]
    prev = df.iloc[-2]
    v = []
    rsi = last.get("RSI")
    v.append((5 if rsi < 30 else 4 if rsi < 40 else 2 if rsi < 50 else 0) if pd.notna(rsi) else 2)
    close = last["Close"]
    bu, bl = last.get("BB_Upper"), last.get("BB_Lower")
    pos = (close - bl) / (bu - bl) if (pd.notna(bu) and bu != bl) else 0.5
    v.append(5 if pos < 0.1 else 4 if pos < 0.25 else 2 if pos < 0.45 else 0)
    base_n = 120 if mid else 20
    t1, t2, t3 = (-0.22, -0.13, -0.06) if mid else (-0.12, -0.07, -0.03)
    base_ma = last.get(f"MA{base_n}")
    gap = (close - base_ma) / base_ma if (pd.notna(base_ma) and base_ma > 0) else 0
    v.append(5 if gap <= t1 else 4 if gap <= t2 else 2 if gap <= t3 else 1)
    vma = last.get("VOL_MA20")
    r = last["Volume"] / vma if (pd.notna(vma) and vma > 0) else 1
    v.append(5 if r >= 1.5 else 3 if r >= 1.0 else 1)
    up = close > prev["Close"]
    cu = close > last.get("Open", close)
    v.append(5 if (up and cu) else 3 if up else 1)
    return sum(v) * 4


def overheat(df, horizon="short"):
    mid = horizon == "mid"
    win = 40 if mid else 5
    if df.empty or len(df) < win + 1:
        return 0, []
    last, prev = df.iloc[-1], df.iloc[-2]
    close = float(last["Close"])
    rsi = last.get("RSI")
    bu = last.get("BB_Upper")
    chg1 = (close - prev["Close"]) / prev["Close"] * 100 if prev["Close"] else 0
    cw = float(df["Close"].iloc[-(win + 1)])
    chg5 = (close - cw) / cw * 100 if cw else 0
    vma = last.get("VOL_MA20")
    vr = last["Volume"] / vma if (pd.notna(vma) and vma > 0) else 0
    rsi_hi = 78 if mid else 70
    c_rsi = pd.notna(rsi) and rsi >= rsi_hi
    c_band = pd.notna(bu) and close >= bu
    c_s1, c_s5, c_vol = chg1 >= (15 if mid else 8), chg5 >= (60 if mid else 18), vr >= 2
    tags = [t for t, ok in [
        (f"RSI{rsi:.0f}" if pd.notna(rsi) else "RSI-", c_rsi),
        ("볼린저상단", c_band), (f"1일{chg1:+.0f}%", c_s1),
        (f"{win}일{chg5:+.0f}%", c_s5), (f"거래{vr:.1f}x", c_vol)] if ok]
    met = sum([c_rsi, c_band, c_s1, c_s5, c_vol])
    strong = (c_rsi and (c_band or c_s1)) or (c_s1 and (c_band or c_vol)) or met >= 3
    return (2 if strong else 1 if (c_band or c_s1 or met >= 2) else 0), tags


def position_action(buy_price, cur_price, trend, rev, oh_level, mb=True, horizon="short"):
    if not buy_price or buy_price <= 0 or not cur_price:
        return ""
    mid = horizon == "mid"
    tp, sl = (50, -18) if mid else (20, -8)
    pl = (cur_price - buy_price) / buy_price * 100
    p = f"{pl:+.1f}%"
    if oh_level >= 2 and pl > 0:
        return f"🔥 익절 고려 · 손익 {p} (과열)"
    if pl >= tp and not (mb and trend >= 55):
        return f"💰 익절 고려 · 손익 {p} (추세 둔화)"
    if pl <= sl and trend <= 40 and rev < 70:
        return f"✂️ 손절 검토 · 손익 {p} (추세 약세)"
    if pl < 0 and rev >= 70:
        return f"🔄 추매(물타기) 고려 · 손익 {p} (반등 신호)"
    if pl >= 0 and mb and trend >= 70:
        return f"📈 보유·추가매수 여지 · 손익 {p} (추세 강함)"
    if pl >= 0 and mb and trend >= 55:
        return f"✅ 보유 지속 · 손익 {p}"
    return f"⏸️ 관망 · 손익 {p}"


MKT_BAND = 0.02       # 완충대: 200일선 ±2% 안이면 '중립'(휩쏘 방지) — app.py와 동일
MKT_SLOPE_LB = 20     # 200일선 기울기 판단 기간(거래일)
MKT_SLOPE_TH = 0.003  # 기울기 임계: 20일간 200선 변화 ±0.3%


def market_bullish():
    """하위호환용: 200일선 위/아래만 (추세매수 보류 판정에 사용)."""
    return market_regime()["bullish"]


def market_regime():
    """app.py get_market_regime와 동일 규칙. 200선 위치(완충대 ±2%)+기울기 결합.
    {'bullish','regime'(상승/중립/하락),'slope','gap_pct','slope_pct'}."""
    base = {"bullish": True, "regime": "중립", "slope": "flat", "gap_pct": 0, "slope_pct": 0}
    try:
        end = datetime.now()
        df = fdr.DataReader("KS11", end - timedelta(days=560), end)
        ma = df["Close"].rolling(200).mean()
        cur = float(df["Close"].iloc[-1])
        ma200 = float(ma.iloc[-1])
        ma_prev = float(ma.iloc[-(MKT_SLOPE_LB + 1)]) if ma.notna().sum() > MKT_SLOPE_LB else ma200
        gap = (cur - ma200) / ma200 if ma200 else 0
        slope_pct = (ma200 - ma_prev) / ma_prev if ma_prev else 0
        band = "above" if gap > MKT_BAND else "below" if gap < -MKT_BAND else "within"
        slope = "up" if slope_pct > MKT_SLOPE_TH else "down" if slope_pct < -MKT_SLOPE_TH else "flat"
        if band == "above" and slope in ("up", "flat"):
            regime = "상승"
        elif band == "below" and slope in ("down", "flat"):
            regime = "하락"
        else:
            regime = "중립"
        return {"bullish": cur >= ma200, "regime": regime, "slope": slope,
                "gap_pct": gap * 100, "slope_pct": slope_pct * 100}
    except Exception:
        return base


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

    reg = market_regime()
    bull = reg["bullish"]
    # 시장 국면따라 관점 자동선택: 상승=중장기(추세 길게) / 중립·하락=단기(방어). app.py 자동모드와 동일
    horizon = "mid" if reg["regime"] == "상승" else "short"
    hz_label = "📆 중장기(1~3개월)" if horizon == "mid" else "⚡ 단기(데이·스윙)"
    end = datetime.now()
    start = end - timedelta(days=400)   # MA120 계산 위해 중장기는 데이터 더 필요
    take_profit, cut_loss, add_more = [], [], []
    ref_date = ""

    for it in portfolio:
        code = str(it.get("code", "")).zfill(6)
        name = it.get("name", code)
        buy = it.get("buy_price", 0) or 0
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
        t, r = trend_score(df, horizon), reversion_score(df, horizon)
        lvl, _ = overheat(df, horizon)
        act = position_action(buy, price, t, r, lvl, bull, horizon)
        if not act:
            continue
        line = f"• **{name}** {price:,}원 — {act}"
        if act.startswith(("🔥", "💰")):
            take_profit.append(line)
        elif act.startswith("✂️"):
            cut_loss.append(line)
        elif act.startswith(("🔄", "📈")):
            add_more.append(line)
        # ✅ 보유 지속 / ⏸️ 관망 → 알림 생략(노이즈 방지)

    # 오늘 뜨는 이슈 테마 (2일치 뉴스 분석) + 막 살아나는 테마 (네이버)
    news_themes = analyze_news_themes(fetch_news_titles())
    try:
        rising = early_momentum_themes(get_naver_themes())
    except Exception:
        rising = []

    if not (take_profit or cut_loss or add_more or news_themes or rising):
        print("제안·테마 없음 — 알림 생략")
        return

    _rico = {"상승": "📈", "하락": "📉", "중립": "🔄"}.get(reg["regime"], "📊")
    _slope_txt = {"up": "200선↑", "flat": "200선→", "down": "200선↓"}.get(reg["slope"], "")
    lines = [f"📊 **아침 브리핑** (장 시작 전 · {ref_date} 종가 기준)",
             f"시장 국면: {_rico} **{reg['regime']}** "
             f"(지수 {reg['gap_pct']:+.1f}% vs 200선 · {_slope_txt})"
             + ("" if bull else " · 추세매수 보류"),
             f"적용 관점: {hz_label}  _(국면 따라 자동)_"]
    if news_themes:
        lines += ["", "📰 **오늘 뜨는 이슈 테마** (2일 뉴스)"]
        lines += [f"• {t['theme']} `{t['count']}건` — {t['sectors']} · 대장주: {', '.join(t['leaders'][:3])}"
                  for t in news_themes[:5]]
    if rising:
        lines += ["", "🌱 **막 살아나는 테마** (거래량 유입 동반 · 선점 후보)"]
        lines += [f"• {t['name']} {t['chg']:+.1f}% (3일 {t['chg3']:+.1f}%, 거래량 {t.get('vol', 0):.1f}배) · 대장주: {', '.join(t['leads'][:3])}"
                  for t in rising]
    if take_profit:
        lines += ["", "💰 **익절 고려** (시초가 일부 매도)"] + take_profit
    if cut_loss:
        lines += ["", "✂️ **손절 검토**"] + cut_loss
    if add_more:
        lines += ["", "📈 **추가매수·추매 여지**"] + add_more
    lines += ["", "_내 매수가·손익 + 기술신호 + 뉴스 테마 기준. 매매 결정은 본인 판단._"]

    msg = "\n".join(lines)[:1900]
    resp = requests.post(webhook, json={"content": msg}, timeout=15)
    print("Discord 발송:", resp.status_code)


if __name__ == "__main__":
    main()
