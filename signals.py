"""
공용 신호 로직 — 지표·점수·과열·포지션액션·크로스.
app.py(대시보드)와 check_signals.py(디스코드 알림)가 **둘 다 import**해서
동일 규칙을 보장(과거의 수작업 미러링 제거). 순수 함수(streamlit 비의존).
검증: python3 autotrade/verify_parity.py
"""
import numpy as np
import pandas as pd


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


def _recent_cross(df, fast_n, slow_n, lookback=3):
    """최근 lookback 거래일 내 골든/데드크로스 발생 → ('golden'|'dead', 며칠전) 또는 None.
    며칠전: 0=오늘, 1=하루 전 …. (추세 전환 순간 포착)"""
    f = df.get(f"MA{fast_n}"); s = df.get(f"MA{slow_n}")
    if f is None or s is None or len(df) < slow_n + 2:
        return None
    for k in range(1, lookback + 1):
        a, b = -k, -k - 1
        if pd.notna(f.iloc[a]) and pd.notna(s.iloc[a]) and pd.notna(f.iloc[b]) and pd.notna(s.iloc[b]):
            if f.iloc[b] <= s.iloc[b] and f.iloc[a] > s.iloc[a]:
                return ("golden", k - 1)
            if f.iloc[b] >= s.iloc[b] and f.iloc[a] < s.iloc[a]:
                return ("dead", k - 1)
    return None
