"""
백테스트/검증 결과 기록 — autotrade 검증 스크립트가 결과를 저장하고,
app.py 🧪 백테스트 페이지에서 읽어 표시. 데이터는 backtest_log.json(gitignore).
"""
import json
import os
from datetime import datetime

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_log.json")


def load() -> list:
    if os.path.exists(LOG):
        try:
            with open(LOG, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def record(key: str, title: str, lines: list, verdict: str = "", date: str = "") -> None:
    """검증 결과 저장. 같은 key는 최신으로 덮어씀.
    key=고유키 · title=제목 · lines=요약 문자열 리스트 · verdict=한줄 결론 · date=YYYY-MM-DD."""
    recs = [r for r in load() if r.get("key") != key]
    recs.append({
        "key": key, "title": title, "lines": list(lines),
        "verdict": verdict, "date": date or datetime.now().strftime("%Y-%m-%d"),
    })
    with open(LOG, "w", encoding="utf-8") as f:
        json.dump(recs, f, ensure_ascii=False, indent=2)
