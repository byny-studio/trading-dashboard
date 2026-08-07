"""종목 메타(업종·주요제품) 한 줄 설명 — 종목명 옆 회색 글씨용 공용 모듈.
app.py · stock_screener.py · theme_tracker.py 공용(중복 제거).

'어떤 주식인지'를 구체적으로: KRX-DESC의 Products(주요제품·브랜드) 우선,
없으면 Industry(업종)로 폴백. 예) F&F → 'MLB, MLB KIDS, DISCOVERY 등 패션의류제품'.
"""
import streamlit as st
import FinanceDataReader as fdr


def _clean(s: str) -> str:
    """제품문자열 정리: 개행·중복공백 축소, 앞뒤 구두점 제거."""
    return " ".join(str(s).split()).strip(" ,·/")


@st.cache_data(ttl=86400)
def _desc_map() -> dict:
    """KRX 상장사 설명 맵 {6자리코드: 설명}. 하루 캐시.
    Products(구체적) 우선 · 없으면 Industry(광범위) 폴백."""
    try:
        df = fdr.StockListing("KRX-DESC").dropna(subset=["Code"])
        m = {}
        for _, r in df.iterrows():
            d = r.get("Products")                    # 주요제품(브랜드·품목) 우선
            if not d or isinstance(d, float):
                d = r.get("Industry")                # 없으면 업종
            if d and not isinstance(d, float):
                m[str(r["Code"]).zfill(6)] = _clean(d)
        return m
    except Exception:
        return {}


def stock_desc(code: str, maxlen: int = 38) -> str:
    """종목 설명 한 줄(길면 …로 절단). 없으면 ''."""
    if not code:
        return ""
    d = _desc_map().get(str(code).zfill(6), "")
    return (d[:maxlen] + "…") if len(d) > maxlen else d


def desc_html(code: str, maxlen: int = 38) -> str:
    """종목명 옆 회색·소형·얇은 글씨 span(앞 공백 포함). unsafe_allow_html 필요. 없으면 ''."""
    d = stock_desc(code, maxlen)
    if not d:
        return ""
    return (f" <span style='color:#9aa0a6;font-size:0.72em;font-weight:300;"
            f"opacity:0.75;'>{d}</span>")
