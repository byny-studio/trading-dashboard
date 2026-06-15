"""
캔들 패턴 / 특이사항 분석 모듈.

가장 최근 거래일의 캔들을 보고 "오늘 왜 이렇게 움직였는지" 한 줄짜리 설명을 만들어줍니다.
- 윗꼬리/아랫꼬리 (장중 급등락)
- 갭 (전일 대비 크게 시작)
- 거래량 폭증/저조
- 장대 양봉/음봉
- 도지 (방향 불확실)
- 52주 신고가/신저가

사용법:
    from candle_analyzer import analyze_candle
    observations = analyze_candle(df)  # list of strings
"""

import pandas as pd


def analyze_candle(df: pd.DataFrame) -> list:
    """가장 최근 캔들의 특이사항을 우선순위 순으로 반환.

    Args:
        df: 일봉 데이터프레임 (Open, High, Low, Close, Volume 필수,
                              VOL_MA20 있으면 거래량 분석 추가됨)

    Returns:
        한 줄짜리 메시지 리스트 (최대 3개). 비면 특이사항 없음.
    """
    if df.empty or len(df) < 2:
        return []

    last = df.iloc[-1]
    prev = df.iloc[-2]

    open_p = float(last["Open"])
    high_p = float(last["High"])
    low_p = float(last["Low"])
    close_p = float(last["Close"])
    volume = float(last["Volume"])
    prev_close = float(prev["Close"])

    if open_p <= 0 or prev_close <= 0:
        return []

    obs = []  # (priority, message) — priority 낮을수록 중요

    body = abs(close_p - open_p)
    upper_wick = high_p - max(open_p, close_p)
    lower_wick = min(open_p, close_p) - low_p
    day_range = high_p - low_p

    # 1) 갭 상승/하락 (전일 종가 대비 시가가 크게 떨어진 경우)
    gap_pct = (open_p - prev_close) / prev_close * 100
    if gap_pct > 3:
        obs.append((1, f"📍 갭 상승 시작 (전일 종가 대비 +{gap_pct:.1f}%)"))
    elif gap_pct < -3:
        obs.append((1, f"📍 갭 하락 시작 (전일 종가 대비 {gap_pct:.1f}%)"))

    # 2) 윗꼬리 — 장중 급등 후 매도세 (듀켐바이오 케이스)
    if day_range > 0:
        upper_ratio = upper_wick / day_range
        pct_from_high = (high_p - close_p) / high_p * 100 if high_p > 0 else 0
        if upper_ratio > 0.5 and pct_from_high > 2:
            obs.append((
                2,
                f"⚠️ 장중 고가({high_p:,.0f}원)에서 {pct_from_high:.1f}% 빠진 채 마감 — 매도세 우세 (긴 윗꼬리)"
            ))

    # 3) 아랫꼬리 — 장중 급락 후 반등 (저점 매수세)
    if day_range > 0:
        lower_ratio = lower_wick / day_range
        pct_from_low = (close_p - low_p) / low_p * 100 if low_p > 0 else 0
        if lower_ratio > 0.5 and pct_from_low > 2:
            obs.append((
                2,
                f"💪 장중 저가({low_p:,.0f}원)에서 {pct_from_low:.1f}% 회복 — 저점 매수세 유입 (긴 아랫꼬리)"
            ))

    # 4) 거래량 폭증/저조
    if "VOL_MA20" in df.columns:
        vol_ma = float(last.get("VOL_MA20", 0) or 0)
        if vol_ma > 0:
            vol_ratio = volume / vol_ma
            if vol_ratio > 3:
                obs.append((3, f"🔥 거래량 평소 대비 {vol_ratio:.1f}배 — 시장 관심 급증"))
            elif vol_ratio > 2:
                obs.append((4, f"📊 거래량 평소 대비 {vol_ratio:.1f}배 — 관심 증가"))
            elif vol_ratio < 0.3:
                obs.append((5, f"💤 거래량 평소 대비 {vol_ratio:.1f}배 — 거래 매우 적음"))

    # 5) 장대 양봉/음봉 (몸통이 매우 큼)
    if day_range > 0 and body / day_range > 0.7:
        body_pct = body / open_p * 100
        if close_p > open_p and body_pct > 5:
            obs.append((3, f"🟢 장대 양봉 ({body_pct:.1f}% 상승) — 강한 매수세"))
        elif close_p < open_p and body_pct > 5:
            obs.append((3, f"🔴 장대 음봉 ({body_pct:.1f}% 하락) — 강한 매도세"))

    # 6) 도지 — 시가와 종가가 거의 같지만 장중 변동은 있었음
    if day_range > 0 and body / day_range < 0.1 and day_range / open_p > 0.02:
        obs.append((4, "🎯 도지 캔들 — 매수세·매도세 팽팽, 방향 전환 가능성"))

    # 7) 52주 신고가/신저가
    if len(df) >= 252:
        recent_52w = df.tail(252)
        max_close = float(recent_52w["Close"].max())
        min_close = float(recent_52w["Close"].min())
        if close_p >= max_close * 0.99:
            obs.append((1, "🚀 52주 신고가 근접 (1% 이내)"))
        elif close_p <= min_close * 1.02:
            obs.append((1, "📉 52주 신저가 근접 (2% 이내)"))

    # 우선순위 순 정렬, 상위 3개만 반환
    obs.sort(key=lambda x: x[0])
    return [msg for _, msg in obs[:3]]


def candle_summary_line(df: pd.DataFrame) -> str:
    """모든 특이사항을 한 줄로 합쳐서 반환. 없으면 빈 문자열."""
    items = analyze_candle(df)
    return " · ".join(items) if items else ""
