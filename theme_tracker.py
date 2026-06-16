"""
🌐 테마·이슈 트래커
- 아침 거시 체크 (금리·나스닥·엔비디아·환율·유가·코스피) + 테마 힌트
- 오늘의 강세/약세 테마 (네이버 테마 시세) + 주도주
- 테마 클릭 → 구성종목
- 이벤트→테마→업종 치트시트 (사용자 제공)
"""
from datetime import datetime, timedelta

import pandas as pd
import requests
import streamlit as st
import FinanceDataReader as fdr
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0"}


# ===== 1. 거시 지표 =====
@st.cache_data(ttl=1800)
def get_macro_dashboard() -> list:
    """아침 체크용 거시 지표. (라벨, 값, 등락%, 단위) 리스트."""
    specs = [
        ("美 10년물 금리", "US10YT", "%"),
        ("나스닥", "IXIC", ""),
        ("엔비디아", "NVDA", "$"),
        ("원/달러 환율", "USD/KRW", "원"),
        ("WTI 유가", "CL=F", "$"),
        ("코스피", "KS11", ""),
    ]
    end = datetime.now()
    start = end - timedelta(days=25)
    out = []
    for label, sym, unit in specs:
        try:
            s = fdr.DataReader(sym, start, end)["Close"].dropna()
            if len(s) < 2:
                out.append({"label": label, "value": None, "chg": None, "unit": unit})
                continue
            cur, prev = float(s.iloc[-1]), float(s.iloc[-2])
            chg = (cur - prev) / prev * 100 if prev else 0
            out.append({"label": label, "value": cur, "chg": chg, "unit": unit})
        except Exception:
            out.append({"label": label, "value": None, "chg": None, "unit": unit})
    return out


def macro_theme_hints(macro: list) -> list:
    """거시 방향 → 치트시트 기반 오늘 주목 테마 힌트."""
    by = {m["label"]: m for m in macro}
    hints = []

    def up(label):
        m = by.get(label)
        return m and m["chg"] is not None and m["chg"] > 0

    def down(label):
        m = by.get(label)
        return m and m["chg"] is not None and m["chg"] < 0

    if down("美 10년물 금리"):
        hints.append("📉 금리 하락 → **성장주**(반도체·AI·바이오·2차전지) 우호")
    elif up("美 10년물 금리"):
        hints.append("📈 금리 상승 → **금융**(은행·보험) 우호, 성장주 부담")
    if up("나스닥") or up("엔비디아"):
        hints.append("🤖 나스닥·엔비디아 강세 → **AI·반도체(HBM)** 주목")
    if up("원/달러 환율"):
        hints.append("💱 환율 상승 → **수출주**(반도체·자동차·조선) 유리")
    elif down("원/달러 환율"):
        hints.append("💱 환율 하락 → **내수주**(항공·여행·유통) 유리")
    if up("WTI 유가"):
        hints.append("🛢️ 유가 상승 → **에너지**(정유·LNG)")
    elif down("WTI 유가"):
        hints.append("🛢️ 유가 하락 → **항공·운송**(비용↓)")
    return hints


# ===== 2. 네이버 테마 시세 =====
@st.cache_data(ttl=1800)
def get_naver_themes() -> list:
    """네이버 테마 시세 전체 (등락률 포함). [{name,no,chg,chg3,up,down,leads}]."""
    themes = []
    for page in (1, 2, 3, 4, 5, 6):
        try:
            r = requests.get(
                f"https://finance.naver.com/sise/theme.naver?&page={page}",
                headers=UA, timeout=10,
            )
            r.encoding = "euc-kr"
            soup = BeautifulSoup(r.text, "html.parser")
            rows = soup.select("table.type_1 tr")
        except Exception:
            break
        found = False
        for tr in rows:
            a = tr.select_one("td.col_type1 a")
            tds = tr.select("td")
            if not a or len(tds) < 8:
                continue
            href = a.get("href", "")
            no = href.split("no=")[-1] if "no=" in href else ""

            def _pct(txt):
                try:
                    return float(txt.replace("%", "").replace("+", "").replace(",", ""))
                except Exception:
                    return None

            leads = [x.get_text(strip=True) for x in (tds[6].select("a") + tds[7].select("a"))]
            if not leads:
                leads = [tds[6].get_text(strip=True), tds[7].get_text(strip=True)]
            themes.append({
                "name": a.get_text(strip=True),
                "no": no,
                "chg": _pct(tds[1].get_text(strip=True)),
                "chg3": _pct(tds[2].get_text(strip=True)),
                "up": tds[3].get_text(strip=True),
                "down": tds[5].get_text(strip=True),
                "leads": [x for x in leads if x][:3],
            })
            found = True
        if not found:
            break
    return [t for t in themes if t["chg"] is not None]


@st.cache_data(ttl=1800)
def get_theme_stocks(no: str) -> list:
    """특정 테마(no)의 구성종목. [{name,code,price,chg}]."""
    if not no:
        return []
    try:
        r = requests.get(
            f"https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no={no}",
            headers=UA, timeout=10,
        )
        r.encoding = "euc-kr"
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return []
    out = []
    for tr in soup.select("table.type_5 tr"):
        a = tr.select_one("td.name a, div.name_area a")
        if not a:
            continue
        href = a.get("href", "")
        code = href.split("code=")[-1] if "code=" in href else ""
        tds = tr.select("td")
        price = tds[2].get_text(strip=True) if len(tds) > 2 else ""
        chg = ""
        for td in tds:
            t = td.get_text(strip=True)
            if "%" in t:
                chg = t
                break
        out.append({"name": a.get_text(strip=True), "code": code, "price": price, "chg": chg})
    return out


@st.cache_data(ttl=1800)
def get_theme_volume_ratio(no: str):
    """테마 구성종목 거래량합 ÷ 전일거래량합 (자금 유입 척도)."""
    if not no:
        return None
    try:
        r = requests.get(
            f"https://finance.naver.com/sise/sise_group_detail.naver?type=theme&no={no}",
            headers=UA, timeout=10)
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


@st.cache_data(ttl=86400)
def get_marcap_map() -> dict:
    """종목코드 → 시가총액 (대장주 판별용, 1일 캐시)."""
    try:
        df = fdr.StockListing("KRX")
        df = df[["Code", "Marcap"]].dropna()
        df["Code"] = df["Code"].astype(str).str.zfill(6)
        return dict(zip(df["Code"], df["Marcap"]))
    except Exception:
        return {}


def _pct_to_float(txt):
    try:
        return float(str(txt).replace("%", "").replace("+", "").replace(",", ""))
    except Exception:
        return 0.0


def stock_timing_tag(trend, rev, oh_level):
    """진입 타이밍 라벨 (거르지 않고 상태만 표시)."""
    if oh_level >= 2:
        return "🔥 과열(늦음주의)"
    if trend >= 70:
        return "⚡ 강세(추세 양호)"
    if trend >= 55:
        return "🌱 추세 초입"
    if rev >= 70:
        return "🔄 반등 시도"
    return "⏸️ 관망"


def analyze_theme_constituents(stocks, marcap, load_stock_data, add_indicators,
                               trend_score, reversion_score, dual_verdict,
                               market_bullish, overheat_signal=None, limit=15):
    """구성종목을 시총 상위 N개로 추리고 추세/반등/과열 점수 계산. 대장주 표시."""
    for s in stocks:
        s["code"] = str(s.get("code", "")).zfill(6)
        s["mcap"] = marcap.get(s["code"], 0)
    ranked = sorted(stocks, key=lambda x: x["mcap"], reverse=True)[:limit]
    for idx, s in enumerate(ranked):
        s["is_leader"] = (idx == 0 and s["mcap"] > 0)
        df = load_stock_data(s["code"]) if s["code"] else None
        if df is None or df.empty or len(df) < 60:
            s["trend"], s["rev"], s["oh"], s["tag"] = 0, 0, 0, "데이터 없음"
            continue
        di = add_indicators(df)
        s["trend"] = trend_score(di)["total"]
        s["rev"] = reversion_score(di)["total"]
        s["oh"] = overheat_signal(di)["level"] if overheat_signal else 0
        s["tag"] = stock_timing_tag(s["trend"], s["rev"], s["oh"])
    return ranked


# ===== 뉴스 → 테마 매핑 (치트시트 기반) =====
# (테마명, [키워드들], 대표 업종)
THEME_KEYWORDS = [
    ("금리/성장주", ["금리", "인하", "인상", "FOMC", "연준", "기준금리", "동결", "파월"], "반도체·바이오·2차전지"),
    ("AI/반도체", ["AI", "인공지능", "엔비디아", "데이터센터", "HBM", "반도체", "메모리", "D램", "TSMC", "GPU"], "반도체·전력설비"),
    ("환율/수출주", ["환율", "원달러", "원/달러", "달러", "강달러", "약달러"], "반도체·자동차·조선"),
    ("유가/에너지", ["유가", "원유", "OPEC", "WTI", "정유", "감산"], "정유·LNG / 항공·운송"),
    ("방산", ["방산", "무기", "국방", "수출 계약", "K방산", "한화에어로", "미사일"], "방산"),
    ("전쟁/재건", ["전쟁", "종전", "휴전", "이스라엘", "이란", "우크라", "러시아", "재건", "정전"], "방산·건설·철강"),
    ("원전/SMR", ["원전", "SMR", "원자력", "소형모듈"], "원전 기자재"),
    ("2차전지", ["2차전지", "전기차", "배터리", "양극재", "리튬", "전고체"], "2차전지"),
    ("조선", ["조선", "선박", "수주", "LNG선", "방산조선"], "조선"),
    ("중국소비", ["중국", "위안", "단체관광", "면세", "유커", "광군제"], "화장품·면세점·엔터"),
    ("바이오", ["바이오", "신약", "임상", "FDA", "제약", "백신"], "바이오"),
    ("건설/부동산", ["건설", "부동산", "재건축", "SOC", "주택", "분양"], "건설·시멘트"),
    ("전력설비", ["전력", "변압기", "송전", "전선", "그리드", "전력망"], "변압기·전선"),
    ("우주항공", ["우주", "발사체", "누리호", "위성", "항공우주"], "항공우주"),
    ("로봇", ["로봇", "휴머노이드", "자동화"], "자동화"),
    ("코인/블록체인", ["비트코인", "가상자산", "코인", "블록체인", "가상화폐", "이더리움"], "거래소·블록체인"),
    ("금", ["금값", "금 가격", "안전자산", "골드"], "금 관련주"),
    ("희토류", ["희토류", "수출규제", "광물"], "희토류"),
]


def analyze_news_themes(titles: list) -> list:
    """뉴스 제목들에서 테마 키워드를 탐지해 빈도순으로 반환."""
    results = []
    for theme, kws, sectors in THEME_KEYWORDS:
        hits, examples = 0, []
        for t in titles:
            if any(k in t for k in kws):
                hits += 1
                if len(examples) < 2:
                    examples.append(t)
        if hits:
            results.append({"theme": theme, "count": hits, "sectors": sectors, "examples": examples})
    results.sort(key=lambda x: x["count"], reverse=True)
    return results


def early_momentum_themes(themes: list, top=8, vol_fn=None, min_vol=1.2) -> list:
    """막 살아나는 테마: 3일 흐름 위 + 오늘 가속 + 아직 안 터짐(<4%) + (거래량 증가)."""
    cand = []
    for t in themes:
        c, c3 = t.get("chg"), t.get("chg3")
        if c is None or c3 is None:
            continue
        pace = c3 / 3.0  # 3일 평균 일등락
        if c3 > 0 and 0 < c < 4 and c > pace:  # 상승 흐름 + 오늘 가속 + 아직 과열 전
            cand.append(t)
    cand.sort(key=lambda x: x["chg"] - x["chg3"] / 3.0, reverse=True)  # 가속도 순
    if vol_fn is None:
        return cand[:top]
    # 거래량 증가(자금 유입) 확인 — 상위 가속 후보부터
    out = []
    for t in cand[:15]:
        vr = vol_fn(t.get("no", ""))
        if vr is None or vr < min_vol:
            continue
        out.append({**t, "vol": vr})
        if len(out) >= top:
            break
    return out


# ===== 3. 치트시트 (사용자 제공) =====
CHEAT_EVENT = """
| 이벤트 | 테마 | 대표 업종 |
|---|---|---|
| 금리 인하 | AI, 성장주, 리츠 | 반도체, IT, 바이오, 2차전지 |
| 금리 인상 | 금융 | 은행, 보험 |
| 미국 CPI 하락 | 성장주 | 반도체, AI |
| 미국 CPI 상승 | 방어주 | 은행, 보험 |
| 미국 경기 호황 | AI, 소비 | 반도체, 자동차 |
| 미국 경기 침체 | 방어주 | 통신, 필수소비재 |
| 원달러 환율 상승 | 수출주 | 반도체, 자동차, 조선 |
| 원달러 환율 하락 | 내수주 | 항공, 여행, 유통 |
| 유가 상승 | 에너지 | 정유, LNG |
| 유가 하락 | 소비회복 | 항공, 운송 |
| 중국 경기 회복 | 중국 소비 | 화장품, 면세점, 엔터 |
| 중국 단체관광 허용 | 중국 소비 | 화장품, 면세점, 카지노 |
| 전쟁 발생 | 방산, 금, 원유 | 방산, 에너지 |
| 휴전·종전 | 재건 | 건설, 철강 |
| 감염병 확산 | 진단키트, 백신 | 바이오 |
| 감염병 종료 | 리오프닝 | 항공, 호텔, 카지노 |
| 부동산 규제 완화 | 재건축 | 건설, 시멘트 |
| 부동산 규제 강화 | 금융투자 | 증권 |
| 대선 | 정책주 | 정책 관련 업종 |
| 정부 SOC 확대 | 인프라 | 건설, 철강 |
| 원전 확대 | 원전, SMR | 원전 기자재 |
| AI 투자 확대 | AI, 데이터센터 | 반도체, 전력설비 |
| 전력 부족 | 전력 인프라 | 변압기, 전선 |
| 전기차 확대 | 배터리 | 2차전지 |
| 로봇 보급 | 로봇 | 자동화 |
| 드론 전쟁 | 드론 | 방산, 드론 |
| 우주개발 | 우주항공 | 항공우주 |
| 반도체 업황 개선 | 반도체 | 반도체 |
| 가상화폐 상승 | 코인 | 거래소, 블록체인 |
| 금 가격 상승 | 금 | 금 관련주 |
"""

CHEAT_REGULAR = """
| 테마 | 대표 이슈 |
|---|---|
| AI | 엔비디아, 데이터센터 |
| 반도체 | HBM, 메모리 가격 |
| 2차전지 | 전기차 판매량 |
| 방산 | 전쟁, 무기 수출 |
| 원전 | 원전 수출, SMR |
| 전력설비 | AI 전력수요 |
| 로봇 | 자동화 |
| 드론 | 전쟁, 물류 |
| 우주항공 | 누리호, 우주산업 |
| 화장품 | 중국 소비 |
| 엔터 | K-POP, 콘텐츠 |
| 바이오 | 신약, 백신 |
| 재건축 | 부동산 정책 |
| STO | 토큰증권 |
| 블록체인 | 비트코인 |
| LNG | 에너지 |
| 조선 | 선박 발주 |
| 해운 | 운임 상승 |
| 희토류 | 중국 수출규제 |
| 양자컴퓨터 | 기술 뉴스 |
"""

CHEAT_SEASON = """
| 시기 | 테마 | 업종 |
|---|---|---|
| 1~2월 | 난방 | LNG, 도시가스 |
| 2~4월 | 황사·미세먼지 | 공기청정기, 마스크 |
| 3~5월 | 벚꽃·여행 | 항공, 호텔 |
| 5~8월 | 냉방 | 전력, 에어컨 |
| 6~9월 | 태풍·장마 | 건자재, 복구 |
| 7~8월 | 여름휴가 | 여행, 항공 |
| 9~11월 | 독감백신 | 제약, 바이오 |
| 10~11월 | 난방 준비 | LNG |
| 11~12월 | 쇼핑 시즌 | 유통 |
| 12월 | 배당 | 금융, 통신 |
| 연말 | 산타랠리 | 대형주 |
"""

CHEAT_CORE10 = """
| 중요도 | 테마 |
|---|---|
| ★★★★★ | 금리 |
| ★★★★★ | AI |
| ★★★★★ | 반도체 |
| ★★★★★ | 환율 |
| ★★★★★ | 중국 소비 |
| ★★★★☆ | 방산 |
| ★★★★☆ | 원전 |
| ★★★★☆ | 전력설비 |
| ★★★★☆ | 2차전지 |
| ★★★☆☆ | 바이오 |
"""


# ===== 페이지 렌더 =====
def render_theme_tracker(get_market_news=None, load_stock_data=None, add_indicators=None,
                         trend_score=None, reversion_score=None, dual_verdict=None,
                         overheat_signal=None, market_bullish=True):
    st.title("🌐 테마·이슈")

    # --- 1. 아침 거시 체크 ---
    st.markdown("### 🌅 아침 거시 체크")
    st.caption("매일 순서대로: 금리 → 나스닥 → 엔비디아 → 환율 → 유가 → 그날 강한 테마 찾기")
    with st.spinner("거시 지표 불러오는 중..."):
        macro = get_macro_dashboard()
    cols = st.columns(len(macro))
    for c, m in zip(cols, macro):
        if m["value"] is None:
            c.metric(m["label"], "-")
        else:
            v = m["value"]
            vtxt = f"{v:,.2f}{m['unit']}" if m["unit"] in ("%", "$") else f"{v:,.0f}{m['unit']}"
            c.metric(m["label"], vtxt, f"{m['chg']:+.2f}%")
    hints = macro_theme_hints(macro)
    if hints:
        st.markdown("**📌 오늘 거시 기준 주목 테마**")
        for h in hints:
            st.markdown(f"- {h}")
    st.caption("※ 미국 CPI·경기지표는 발표일에 직접 확인하세요(여기선 자동 표시 안 됨).")

    st.markdown("---")

    # --- 1.5 뉴스로 뜨는 이슈 테마 (이틀치 뉴스 분석) ---
    st.markdown("### 📰 뉴스로 뜨는 이슈 테마 (최근 2일)")
    st.caption("시장에 영향 주는 뉴스를 분석해 어떤 테마가 거론되는지 — 가격이 움직이기 전에 흐름 포착")
    news_titles = []
    if get_market_news is not None:
        try:
            news_all = get_market_news(30)
            news_titles = [n["title"] for n in news_all]
        except Exception:
            news_all = []
    news_themes = analyze_news_themes(news_titles) if news_titles else []
    if news_themes:
        for nt in news_themes[:6]:
            ex = nt["examples"][0] if nt["examples"] else ""
            st.markdown(
                f"- **{nt['theme']}** `{nt['count']}건` · 업종: {nt['sectors']}"
                + (f"\n  <span style='color:#A1A1AA;font-size:12px'>예: {ex}</span>" if ex else ""),
                unsafe_allow_html=True,
            )
    else:
        st.info("최근 뉴스에서 뚜렷한 테마 키워드를 찾지 못했습니다.")

    st.markdown("---")

    # --- 2. 오늘의 강세/약세 테마 ---
    st.markdown("### 🔥 오늘의 테마 시세 (네이버)")
    with st.spinner("테마 시세 불러오는 중..."):
        themes = get_naver_themes()
    if not themes:
        st.info("테마 시세를 불러오지 못했습니다.")
    else:
        themes_sorted = sorted(themes, key=lambda x: x["chg"], reverse=True)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**📈 강세 테마 Top 10**")
            st.dataframe(pd.DataFrame([{
                "테마": t["name"], "등락률": f"{t['chg']:+.2f}%",
                "3일": f"{t['chg3']:+.2f}%" if t["chg3"] is not None else "-",
                "주도주": ", ".join(t["leads"]),
            } for t in themes_sorted[:10]]), use_container_width=True, hide_index=True)
        with c2:
            st.markdown("**📉 약세 테마 Top 10**")
            st.dataframe(pd.DataFrame([{
                "테마": t["name"], "등락률": f"{t['chg']:+.2f}%",
                "3일": f"{t['chg3']:+.2f}%" if t["chg3"] is not None else "-",
                "주도주": ", ".join(t["leads"]),
            } for t in themes_sorted[-10:][::-1]]), use_container_width=True, hide_index=True)

        # 🌱 막 살아나는 테마 (초기 모멘텀 + 거래량 증가)
        with st.spinner("초기 모멘텀 테마 확인 중 (거래량 체크)..."):
            early = early_momentum_themes(themes, vol_fn=get_theme_volume_ratio)
        st.markdown("**🌱 막 살아나는 테마** (흐름 위 + 거래량 유입 — 선점 후보)")
        if early:
            st.dataframe(pd.DataFrame([{
                "테마": t["name"], "오늘": f"{t['chg']:+.2f}%",
                "3일": f"{t['chg3']:+.2f}%",
                "거래량": f"{t['vol']:.1f}배" if t.get("vol") else "-",
                "주도주": ", ".join(t["leads"]),
            } for t in early]), use_container_width=True, hide_index=True)
            st.caption("거래량 = 구성종목 거래량합 ÷ 전일거래량합 (1.2배 이상만 = 자금 유입 동반)")
        else:
            st.caption("조건(3일 상승 + 오늘 가속 + 4% 미만 + 거래량 1.2배↑)에 맞는 테마가 지금은 없습니다.")

        # 테마 클릭 → 구성종목 (대장주 + 추세순 카드 + 그룹 리스트)
        st.markdown("#### 🔎 테마 구성종목 분석")
        name_to_no = {f"{t['name']} ({t['chg']:+.2f}%)": t["no"] for t in themes_sorted}
        pick = st.selectbox("테마 선택", ["선택하세요"] + list(name_to_no.keys()))
        can_score = all(f is not None for f in
                        (load_stock_data, add_indicators, trend_score, reversion_score, dual_verdict))
        if pick != "선택하세요":
            with st.spinner("구성종목 분석 중... (추세 점수 계산)"):
                stocks = get_theme_stocks(name_to_no[pick])
                if stocks and can_score:
                    marcap = get_marcap_map()
                    ranked = analyze_theme_constituents(
                        stocks, marcap, load_stock_data, add_indicators,
                        trend_score, reversion_score, dual_verdict, market_bullish,
                        overheat_signal=overheat_signal)
                else:
                    ranked = []
            if not stocks:
                st.info("구성종목을 불러오지 못했습니다.")
            elif not ranked:
                # 점수 계산 불가 시 단순 표
                st.dataframe(pd.DataFrame([{
                    "종목": s["name"], "현재가": s["price"], "등락률": s["chg"],
                } for s in stocks]), use_container_width=True, hide_index=True)
            else:
                leader = next((s for s in ranked if s.get("is_leader")), None)
                if leader:
                    st.markdown(
                        f"👑 **대장주: {leader['name']}** "
                        f"(시총 1위 · {leader['price']}원 · {leader['chg']} · 추세 {leader['trend']})"
                    )

                # 추세 강한 순 카드 4개 (과열도 거르지 않고 라벨로 표시)
                by_trend = sorted(ranked, key=lambda x: x["trend"], reverse=True)
                cards = by_trend[:4]
                st.markdown("**📈 추세 강한 종목 Top 4** (라벨로 진입 타이밍 구분)")
                ccols = st.columns(4)
                for col, s in zip(ccols, cards):
                    with col:
                        with st.container(border=True):
                            crown = "👑 " if s.get("is_leader") else ""
                            st.markdown(f"**{crown}{s['name']}**")
                            st.markdown(
                                f"<div style='font-size:13px;line-height:1.7'>"
                                f"{s['price']}원 · {s['chg']}<br>"
                                f"📈 추세 <b>{s['trend']}</b> · 🔄 반등 <b>{s['rev']}</b><br>"
                                f"<b>{s['tag']}</b></div>",
                                unsafe_allow_html=True,
                            )

                # 나머지 → 타이밍 라벨별 그룹 리스트
                rest = by_trend[4:]
                if rest:
                    st.markdown("**📋 나머지 종목 (진입 타이밍별)**")
                    order = ["⚡ 강세(추세 양호)", "🌱 추세 초입", "🔄 반등 시도",
                             "🔥 과열(늦음주의)", "⏸️ 관망", "데이터 없음"]
                    by_tag = {}
                    for s in rest:
                        by_tag.setdefault(s["tag"], []).append(s)
                    for tag in order:
                        items = by_tag.get(tag, [])
                        if not items:
                            continue
                        st.markdown(f"**{tag}**")
                        for s in items:
                            crown = "👑 " if s.get("is_leader") else ""
                            st.markdown(
                                f"- {crown}**{s['name']}** · {s['chg']} · 📈{s['trend']} 🔄{s['rev']}"
                            )
                st.caption("⚡강세=추세 좋음 · 🌱초입=진입 적기 · 🔥과열=이미 급등(늦음주의) · "
                           "시총 상위 15개 분석 · 👉 종목명으로 '🔍 단일 종목 분석'에서 상세 확인")

    st.markdown("---")

    # --- 3. 시장·경제 뉴스 ---
    if get_market_news is not None:
        st.markdown("### 📰 시장·경제 주요 뉴스")
        news = get_market_news(5)
        if news:
            for i, n in enumerate(news, 1):
                src = f"  ·  {n['source']}" if n.get("source") else ""
                if n.get("link"):
                    st.markdown(f"**{i}.** [{n['title']}]({n['link']}){src}")
                else:
                    st.markdown(f"**{i}.** {n['title']}{src}")
        st.markdown("---")

    # --- 4. 치트시트 ---
    st.markdown("### 📋 이벤트 → 테마 → 업종 치트시트")
    st.caption("단기매매 사고 순서: ① 무슨 이벤트인지 → ② 어떤 테마인지 → ③ 어떤 업종인지")
    with st.expander("⚡ 이벤트별 (금리·환율·유가·전쟁 등)", expanded=True):
        st.markdown(CHEAT_EVENT)
    with st.expander("🔁 단골 테마 (매년 반복)"):
        st.markdown(CHEAT_REGULAR)
    with st.expander("📅 계절 테마"):
        st.markdown(CHEAT_SEASON)
    with st.expander("⭐ 핵심 10개 (먼저 외우기)"):
        st.markdown(CHEAT_CORE10)
