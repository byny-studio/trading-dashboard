"""
간밤 미국증시 참고 모듈 (대시보드 · 디스코드 공용)
==================================================
자동 매매에 개입하지 않는다. '오늘 시초가 편향'을 정보로만 제공.
근거(실측 2022~): 전날 미국 → 코스피 시초가 갭 상관 0.57, 장중은 0.01(무관).
  → 미국 영향은 개장 갭에 대부분 반영. SOX(반도체)는 국내 반도체주에 나스닥보다 약간 더 밀접.
의존성 최소(fdr/pandas)만 사용 → check_signals(GitHub Actions)에서도 임포트 가능.
"""
import warnings
warnings.filterwarnings("ignore")
from datetime import datetime, timedelta

import pandas as pd
import FinanceDataReader as fdr

# 나스닥 등락 → 코스피 시초가 갭 근사 계수 (실측 회귀: -1.5%→-0.99%, -1%→-0.80%, +1.5%→+0.94%)
GAP_BETA = 0.65


def _chg(sym, start, end):
    """최근 2거래일 종가로 등락률(%)·현재값 반환. (value, chg, last_date)."""
    try:
        s = fdr.DataReader(sym, start, end)["Close"].dropna()
        if len(s) < 2:
            return None, None, ""
        cur, prev = float(s.iloc[-1]), float(s.iloc[-2])
        chg = (cur - prev) / prev * 100 if prev else 0.0
        return cur, chg, str(s.index[-1].date())
    except Exception:
        return None, None, ""


def get_us_overnight() -> dict:
    """간밤 미국 지수(S&P·나스닥·SOX·VIX) + 예상 코스피 시초가 편향."""
    end = datetime.now()
    start = end - timedelta(days=20)
    out, last_date = {}, ""
    for key, label, sym in [("sp", "S&P500", "US500"),
                            ("nasdaq", "나스닥", "IXIC"),
                            ("sox", "SOX 반도체", "^SOX")]:
        val, chg, d = _chg(sym, start, end)
        out[key] = {"label": label, "value": val, "chg": chg}
        if d and d > last_date:
            last_date = d
    # VIX (공포지수) — 방향보다 '변동성 커진다' 신호
    vval, vchg, _ = _chg("VIX", start, end)
    out["vix"] = {"label": "VIX", "value": vval, "chg": vchg}

    nq = out["nasdaq"]["chg"]
    out["gap_bias"] = round(nq * GAP_BETA, 2) if nq is not None else None
    out["date"] = last_date

    # 리스크 레벨(VIX 기준)
    if vval is None:
        out["risk"] = "unknown"
    elif vval >= 30:
        out["risk"] = "high"
    elif vval >= 20:
        out["risk"] = "elevated"
    else:
        out["risk"] = "normal"
    return out


def bias_text(us: dict) -> str:
    """예상 시초가 편향 한 줄."""
    gb = us.get("gap_bias")
    if gb is None:
        return "시초가 편향: 데이터 없음"
    if gb <= -1.0:
        tag = "📉 강한 갭다운 예상 — 저가 매수 관점(급락은 오히려 싸게 진입 기회)"
    elif gb <= -0.3:
        tag = "🔻 약한 갭다운 예상"
    elif gb >= 1.0:
        tag = "📈 강한 갭업 예상 — 추격 주의(시초가 과열 가능)"
    elif gb >= 0.3:
        tag = "🔺 약한 갭업 예상"
    else:
        tag = "➡️ 시초가 보합권 예상"
    return f"예상 코스피 시초가 편향 **{gb:+.1f}%** — {tag}"


def risk_text(us: dict) -> str:
    """VIX 리스크 한 줄 (normal이면 빈 문자열)."""
    v = us.get("vix", {})
    lvl = us.get("risk")
    if lvl == "high":
        return f"⚠️ VIX {v['value']:.0f} (공포권) — 변동성 확대, 시초가 매수 신중"
    if lvl == "elevated":
        return f"🟡 VIX {v['value']:.0f} (경계) — 평소보다 변동성 큼"
    return ""


def overnight_lines(us: dict) -> list:
    """디스코드/텍스트용 간밤 미국 요약 라인들."""
    def fmt(m):
        if m.get("chg") is None:
            return f"{m['label']} —"
        return f"{m['label']} {m['chg']:+.1f}%"
    idx = "  ·  ".join(fmt(us[k]) for k in ("sp", "nasdaq", "sox"))
    lines = [f"🌙 **간밤 미국** ({us.get('date', '')})", idx, bias_text(us)]
    rt = risk_text(us)
    if rt:
        lines.append(rt)
    return lines


if __name__ == "__main__":
    u = get_us_overnight()
    print("\n".join(overnight_lines(u)))
