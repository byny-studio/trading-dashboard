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
def render_theme_tracker(get_market_news=None):
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

        # 테마 클릭 → 구성종목
        st.markdown("#### 🔎 테마 구성종목 보기")
        name_to_no = {f"{t['name']} ({t['chg']:+.2f}%)": t["no"] for t in themes_sorted}
        pick = st.selectbox("테마 선택", ["선택하세요"] + list(name_to_no.keys()))
        if pick != "선택하세요":
            with st.spinner("구성종목 불러오는 중..."):
                stocks = get_theme_stocks(name_to_no[pick])
            if stocks:
                st.dataframe(pd.DataFrame([{
                    "종목": s["name"], "코드": s["code"],
                    "현재가": s["price"], "등락률": s["chg"],
                } for s in stocks]), use_container_width=True, hide_index=True)
                st.caption("👉 관심 종목은 '🔍 단일 종목 분석'에서 코드/이름으로 추세·반등 점수를 확인하세요.")
            else:
                st.info("구성종목을 불러오지 못했습니다.")

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
