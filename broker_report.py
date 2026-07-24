"""
증권사 리서치 리포트 (네이버 금융 리서치)
==========================================
일반 뉴스 대신 '증권사 리포트'(유진투자증권·하나증권 등 공신력 있는 애널리스트 리포트)를 가져온다.
 - broker_reports(code)      : 특정 종목의 증권사 리포트
 - broker_reports()          : 최근 전체 종목분석 리포트
반환: [{"종목","제목","증권사","날짜"}, ...]
"""
import io
import requests
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0"}
BASE = "https://finance.naver.com/research/company_list.naver"


def broker_reports(code: str = None, n: int = 15) -> list:
    """증권사 종목분석 리포트. code 주면 그 종목만, 없으면 최근 전체."""
    url = f"{BASE}?searchType=itemCode&itemCode={str(code).zfill(6)}" if code else BASE
    try:
        html = requests.get(url, headers=UA, timeout=8).content.decode("euc-kr", "replace")
        for t in pd.read_html(io.StringIO(html)):
            cols = [str(c) for c in t.columns]
            if "제목" in cols and "증권사" in cols:
                t = t.dropna(subset=["제목", "증권사"])
                out = []
                for _, r in t.head(n).iterrows():
                    out.append({"종목": str(r.get("종목명", "")).strip(),
                                "제목": str(r["제목"]).strip(),
                                "증권사": str(r["증권사"]).strip(),
                                "날짜": str(r.get("작성일", "")).strip()})
                return out
    except Exception:
        pass
    return []


if __name__ == "__main__":
    print("=== 삼성전자(005930) 증권사 리포트 ===")
    for x in broker_reports("005930", 6):
        print(f"  [{x['증권사']}] {x['제목']} ({x['날짜']})")
    print("\n=== 최근 전체 리포트 ===")
    for x in broker_reports(None, 5):
        print(f"  {x['종목']} · [{x['증권사']}] {x['제목']} ({x['날짜']})")
