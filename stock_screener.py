"""
종목 발굴 (스크리닝) 모듈
KOSPI 200 + KOSDAQ 150 약 350개 종목에서 매수 신호 종목 발굴.

app.py 사용법:
    from stock_screener import render_screener
    ...
    elif mode == "🔭 종목 발굴":
        render_screener(
            load_stock_data=load_stock_data,
            add_indicators=add_indicators,
            score_signal=score_signal,
            calc_stop_levels=calc_stop_levels,
            make_chart=make_chart,
        )
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import FinanceDataReader as fdr
import streamlit as st

from candle_analyzer import analyze_candle


WATCHLIST_FILE = "watchlist.json"


# 종목 업종·주요제품 한 줄 설명(종목명 옆 회색 글씨) — 공용 모듈
from stock_meta import stock_desc, desc_html  # noqa: E402,F401


def _autotrade_path() -> str:
    """autotrade 폴더를 sys.path에 넣어 키움 API·매집 스캐너를 import 가능하게. 경로 반환."""
    import sys
    ad = os.path.join(os.path.dirname(os.path.abspath(__file__)), "autotrade")
    if os.path.isdir(ad) and ad not in sys.path:
        sys.path.insert(0, ad)
    return ad

DEFAULT_THRESHOLD = 72   # 매수 관심 (강함) 이상 — 100점 만점 기준
STRONG_THRESHOLD = 80    # 강력 매수 — 100점 만점 기준
MAX_SCORE = 100


# ===== 워치리스트 (관심종목) =====
def load_watchlist() -> list:
    # 클라우드: Secrets(watchlist_json) 우선 — 개인데이터는 gitignore라 클라우드엔 Secrets로만 전달
    try:
        raw = st.secrets["watchlist_json"]
    except Exception:
        raw = None
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            pass
    if os.path.exists(WATCHLIST_FILE):
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_watchlist(items: list) -> None:
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


# ===== ⭐ 별표 (보기용 즐겨찾기 — 관리 없이 눈에 띄게 표시만) =====
STARRED_FILE = "starred.json"


def load_starred() -> set:
    if os.path.exists(STARRED_FILE):
        try:
            with open(STARRED_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_starred(codes) -> None:
    with open(STARRED_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(codes), f, ensure_ascii=False, indent=2)


def toggle_starred(code: str) -> set:
    s = load_starred()
    s.discard(code) if code in s else s.add(code)
    save_starred(s)
    return s


# ===== 종목 풀 (KOSPI 200 + KOSDAQ 150) =====
@st.cache_data(ttl=86400)  # 하루 캐시 (인덱스 구성종목은 잘 안 바뀜)
def get_screening_universe() -> list:
    """KOSPI 200 + KOSDAQ 150 종목 코드/이름 리스트."""
    try:
        kospi200 = fdr.StockListing("KOSPI200")
        kosdaq150 = fdr.StockListing("KOSDAQ150")
        rows = []
        for _, r in kospi200.iterrows():
            rows.append({"code": r["Code"], "name": r["Name"], "market": "KOSPI200"})
        for _, r in kosdaq150.iterrows():
            rows.append({"code": r["Code"], "name": r["Name"], "market": "KOSDAQ150"})
        return rows
    except Exception:
        # 인덱스 리스트 못 가져오면 시가총액 상위로 대체
        kospi = fdr.StockListing("KOSPI")
        kosdaq = fdr.StockListing("KOSDAQ")
        rows = []
        for _, r in kospi.head(200).iterrows():
            rows.append({"code": r["Code"], "name": r["Name"], "market": "KOSPI"})
        for _, r in kosdaq.head(150).iterrows():
            rows.append({"code": r["Code"], "name": r["Name"], "market": "KOSDAQ"})
        return rows


# ===== 메인 화면 =====
def financial_health(fund: dict) -> dict:
    """펀더멘털(PER/PBR/EPS/배당) → 재무 건전성 등급.
    과매도 반등의 '떨어지는 칼날(부실기업)' 걸러내기용. EPS(흑자 여부)가 핵심."""
    if not fund:
        return {"ok": True, "grade": "❔정보없음", "reason": "재무 데이터 없음(판단 보류)"}
    eps = fund.get("EPS")
    per = fund.get("PER")
    div = fund.get("DIV")
    # 적자(EPS≤0) = 가장 큰 위험 신호 → 반등이 아니라 구조적 하락일 수 있음
    if eps is not None and eps <= 0:
        return {"ok": False, "grade": "🔴위험",
                "reason": f"적자(EPS {int(eps):,}) — 떨어지는 칼날 위험"}
    goods = []
    if eps is not None and eps > 0:
        goods.append("흑자")
    if div is not None and div > 0:
        goods.append(f"배당 {div:.1f}%")
    if per is not None and per > 0:
        goods.append(f"PER {per:.1f}")
    if goods:
        return {"ok": True, "grade": "🟢건전", "reason": " · ".join(goods)}
    return {"ok": True, "grade": "🟡주의", "reason": "이익 지표 불명확(보류)"}


def _recommend_mode(reg: dict):
    """시장 국면(get_market_regime) → 추천 발굴 기준 (모드, 근거, 배너레벨).
    검증(verify_regime.py, 최근 급락+반등 40일): 반등 유일 지수초과(+0.6%p·승률38%),
    모멘텀 최악(-1.9%p). OOS(backtest_oos): 추세=강세장 강함, 반등=하락/횡보 방어 우월."""
    regime = reg.get("regime", "중립")
    gap = reg.get("gap_pct", 0)         # 지수 vs MA200
    if reg.get("crash"):                # 급락(후 반등초입) → 낙폭과대 반등
        return ("🔄 반등(과매도)",
                f"급락 국면(60일 고점대비 {reg.get('dd_from_peak', 0):+.0f}%) — 낙폭과대 반등 노림. "
                f"최근검증상 이 국면 **유일하게 지수 초과**(+0.6%p·승률 최고), 모멘텀 최악.", "warning")
    if regime == "상승":
        return ("📈 단기 추세 · 🚀 모멘텀",
                f"상승추세(지수 MA200 {gap:+.0f}%) — 추세 추종·모멘텀 유리(강세장 검증).", "success")
    if regime == "하락":
        return ("🔄 반등(과매도)",
                "하락 국면 — 낙폭과대 방어 반등이 검증상 우월(승률↑).", "warning")
    return ("🔄 반등(과매도) · ⚖️ 종합",
            "중립/횡보 — 뚜렷한 추세 없음, 반등·종합이 무난.", "info")


def _render_regime_reco(reg: dict):
    """발굴 페이지 상단: 현재 시장 국면 + 추천 발굴 기준 배너."""
    if not reg or not reg.get("ok", True):
        return
    mode, why, level = _recommend_mode(reg)
    head = f"🧭 **현재 시장 국면: {reg.get('regime', '?')}**"
    if reg.get("crash"):
        head += " · ⚠️급락"
    head += (f"　(KOSPI {reg.get('index', 0):,.0f} · MA200대비 {reg.get('gap_pct', 0):+.0f}% "
             f"· 20일 {reg.get('mom20', 0):+.0f}%)")
    msg = f"{head}\n\n👉 **지금은 이걸로 발굴: {mode}** — {why}"
    {"success": st.success, "warning": st.warning, "info": st.info}.get(level, st.info)(msg)
    st.caption("국면 기반 추천(참고) · 급락장은 어떤 발굴도 절대수익 마이너스 가능 · 검증 `autotrade/verify_regime.py`")


def render_screener(
    load_stock_data,
    add_indicators,
    score_signal,
    calc_stop_levels=None,
    make_chart=None,
    trend_score=None,
    reversion_score=None,
    momentum_score=None,
    get_fundamentals=None,
    show_detail=None,
    market_regime=None,
    add_to_sim=None,
):
    """종목 발굴 화면. show_detail: 종목 클릭 시 풀 상세를 렌더할 콜백(code,name) — 없으면 축약.
    add_to_sim: 매집 카드에서 모의매수로 바로 담는 콜백(app.py sim_portfolio)."""
    st.title("🔭 종목 발굴")
    if market_regime:
        _render_regime_reco(market_regime)
    st.caption(
        "KOSPI 200 + KOSDAQ 150 약 350개 종목 중 매수 신호가 강한 종목을 찾습니다. "
        "첫 스캔은 2-4분 정도 걸려요."
    )

    # ----- 발굴 기준 (라디오) -----
    # 각 기준: (라벨, 점수함수(df)->{"total":..}, 설명)
    modes = []
    if trend_score:
        modes.append(("📈 단기 추세", lambda df: trend_score(df, "short"),
                      "정배열·골든크로스 등 단기 상승 추세가 살아있는 종목"))
        modes.append(("📆 중장기 추세", lambda df: trend_score(df, "mid"),
                      "MA60/120 기준 큰 추세가 상승인 중장기 종목"))
    if reversion_score:
        modes.append(("🔄 반등(과매도)", lambda df: reversion_score(df, "short"),
                      "많이 빠진 낙폭과대 + 반등 조짐이 보이는 종목"))
    if momentum_score:
        modes.append(("🚀 모멘텀", lambda df: momentum_score(df),
                      "최근 상승률·가속·신고가 근접·거래량이 강한 종목"))
    modes.append(("⚖️ 종합(구)", lambda df: score_signal(df),
                  "이평·크로스·RSI·볼린저·거래량 5개 종합(기존 기준)"))
    # 📥 매집: 가격점수가 아닌 키움 투자자별 수급 기반 → 전용 렌더로 분기(mode_fn 없음)
    modes.append(("📥 조용한 매집", None,
                  "사모·기관이 조용히 사모으고 개미는 빠진 종목(키움 수급). 참고용 워치리스트."))
    # 🚨 실시간 급등: 키움 등락률상위 → 모니터링 전용(매수신호 아님, 백테스트상 기대값 음)
    modes.append(("🚨 실시간 급등", None,
                  "오늘 15%↑ + 체결강도·거래량 강한 종목(키움 실시간). ⚠️모니터링 전용."))

    mode_map = {m[0]: (m[1], m[2]) for m in modes}
    sel_mode = st.radio("발굴 기준", list(mode_map.keys()), horizontal=True,
                        key="screen_mode",
                        help="기준마다 '강한 종목'의 정의가 달라집니다. 추세=오르는 중, "
                             "반등=많이 빠진 것, 모멘텀=최근 강하게 오르는 것, "
                             "매집=사모·기관이 조용히 모으는 것(수급).")

    # 매집은 가격 채점이 아니라 수급 스캔 → 완전히 다른 화면
    if sel_mode.startswith("📥"):
        st.caption(f"📌 **{sel_mode}** — {mode_map[sel_mode][1]}")
        _render_accumulation(
            load_stock_data=load_stock_data, add_indicators=add_indicators,
            score_signal=score_signal, calc_stop_levels=calc_stop_levels,
            make_chart=make_chart, add_to_sim=add_to_sim, show_detail=show_detail,
        )
        # 매집에서 담은 관심종목을 이 화면 하단에도 바로 표시 (목록은 저장정보만 → 가벼움)
        st.markdown("---")
        _render_watchlist(
            load_watchlist(), save_watchlist,
            load_stock_data, add_indicators, score_signal, calc_stop_levels, make_chart,
            show_detail=show_detail,
        )
        return

    # 실시간 급등: 키움 등락률상위 → 모니터링 전용 화면
    if sel_mode.startswith("🚨"):
        _render_surge_monitor()
        return

    mode_fn, mode_desc = mode_map[sel_mode]
    st.caption(f"📌 **{sel_mode}** — {mode_desc}")

    # 과매도 반등은 '떨어지는 칼날'(부실기업) 위험 → 재무 검증 옵션 제공
    is_reversion = sel_mode.startswith("🔄")
    exclude_risky = False
    if is_reversion and get_fundamentals is not None:
        exclude_risky = st.checkbox(
            "🩺 재무 부실(적자) 종목 제외 — 과매도 반등의 '떨어지는 칼날' 방지",
            value=True, key="screen_health",
            help="과매도는 일시적 눌림일 수도, 구조적 하락(부실)일 수도 있어요. "
                 "EPS 적자 기업은 반등이 아니라 계속 빠질 위험이 커서 제외합니다.",
        )

    # ----- 컨트롤 -----
    c1, c2, c3 = st.columns([3, 1.2, 1.2])
    with c1:
        threshold = st.slider(
            "최소 점수 (높을수록 더 강한 신호만)",
            min_value=60, max_value=100, value=DEFAULT_THRESHOLD, step=4,
            help="72점 이상 권장. 60-68점은 노이즈 많음, 80점 이상은 매우 엄격."
        )
    with c2:
        scan_btn = st.button(
            "🔍 스캔 시작", type="primary", use_container_width=True
        )
    with c3:
        clear_btn = st.button(
            "결과 지우기", use_container_width=True
        )

    # 세션 상태 초기화
    if "screen_results" not in st.session_state:
        st.session_state.screen_results = None
        st.session_state.screen_time = None
        st.session_state.screen_threshold = threshold

    if clear_btn:
        st.session_state.screen_results = None
        st.session_state.screen_time = None
        st.rerun()

    if st.session_state.screen_time:
        ago = (datetime.now() - st.session_state.screen_time).total_seconds() / 60
        _used = st.session_state.get("screen_mode_used", "")
        st.caption(
            f"마지막 스캔: {st.session_state.screen_time.strftime('%H:%M')} "
            f"({int(ago)}분 전 · 기준 {_used} · 임계값 {st.session_state.screen_threshold}점)"
        )

    # ----- 스캔 실행 -----
    if scan_btn:
        universe = get_screening_universe()
        total = len(universe)
        if total == 0:
            st.error("종목 리스트를 가져오지 못했습니다. 인터넷 연결 확인.")
            return

        # 1) 데이터 병렬 로드 — 350종목을 순차로 받으면 5분+, 스레드로 대폭 단축
        #    (load_stock_data는 내부에 st.* 호출이 없어 스레드에서 안전)
        progress = st.progress(0.0, text=f"데이터 불러오는 중... 0/{total}")
        data_map = {}
        done = 0
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = {ex.submit(load_stock_data, s["code"]): s for s in universe}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    data_map[s["code"]] = fut.result()
                except Exception:
                    data_map[s["code"]] = None
                done += 1
                progress.progress(done / total, text=f"데이터 불러오는 중... {done}/{total}")

        # 2) 채점 (CPU 연산이라 빠름)
        results = []
        for stock in universe:
            df = data_map.get(stock["code"])
            if df is None or df.empty or len(df) < 60:
                continue
            try:
                df = add_indicators(df)
                sc = mode_fn(df).get("total", 0)   # 종목 점수
                if sc >= threshold:
                    rec = {
                        "code": stock["code"],
                        "name": stock["name"],
                        "market": stock["market"],
                        "score": sc,
                        "verdict": "🚀 강력" if sc >= STRONG_THRESHOLD else "✅ 관심",
                        "price": int(df.iloc[-1]["Close"]),
                        "rsi": float(df.iloc[-1].get("RSI", 0) or 0),
                    }
                    # 반등 모드: 통과 종목만 재무 검증 (몇 개뿐 → 빠름)
                    if is_reversion and get_fundamentals is not None:
                        health = financial_health(get_fundamentals(stock["code"]))
                        rec["health"] = health
                        if exclude_risky and not health["ok"]:
                            continue   # 적자(부실) 종목 제외
                    results.append(rec)
            except Exception:
                pass
        progress.empty()

        results.sort(key=lambda x: -x["score"])
        st.session_state.screen_results = results
        st.session_state.screen_time = datetime.now()
        st.session_state.screen_threshold = threshold
        st.session_state.screen_mode_used = sel_mode
        st.rerun()

    # ----- 결과 표시 -----
    results = st.session_state.screen_results
    if results is None:
        st.info("위 '🔍 스캔 시작' 버튼을 눌러 발굴을 시작하세요.")
        return

    if not results:
        st.warning(
            f"점수 {st.session_state.screen_threshold}점 이상의 종목이 없습니다. "
            "임계값을 낮춰 다시 스캔해보세요."
        )
        return

    # 그룹별 분류
    strong = [r for r in results if r["score"] >= STRONG_THRESHOLD]
    interest = [r for r in results if r["score"] < STRONG_THRESHOLD]

    # 요약 메트릭
    m1, m2, m3 = st.columns(3)
    m1.metric("총 발굴 종목", f"{len(results)}개")
    m2.metric("🚀 강력 매수 (80+)", f"{len(strong)}개")
    m3.metric(f"✅ 매수 관심 ({st.session_state.screen_threshold}-79)", f"{len(interest)}개")

    st.markdown("---")

    watchlist = load_watchlist()
    watchlist_codes = {w["code"] for w in watchlist}

    sim_qty = 10
    if add_to_sim:
        sim_qty = int(st.number_input(
            "📒 모의매수 기본 수량(주)", min_value=1, value=10, step=1, key="screen_sim_qty",
            help="발굴 결과의 '📒' 버튼을 누르면 현재가로 이 수량만큼 모의매수에 담깁니다. "
                 "📒 모의매수 메뉴에서 지수(KODEX200) 대비 성과를 추적하세요."))

    # 강력 매수 섹션 (종목 클릭 시 그 행 아래에 상세 아코디언)
    if strong:
        st.markdown(f"### 🚀 강력 매수 ({len(strong)}개)")
        _render_result_table(strong, watchlist, watchlist_codes, save_watchlist, prefix="s",
                             load_stock_data=load_stock_data, add_indicators=add_indicators,
                             score_signal=score_signal, calc_stop_levels=calc_stop_levels,
                             make_chart=make_chart, show_detail=show_detail,
                             add_to_sim=add_to_sim, sim_qty=sim_qty)

    # 매수 관심 섹션
    if interest:
        if strong:
            st.markdown("")
        st.markdown(f"### ✅ 매수 관심 ({len(interest)}개)")
        _render_result_table(interest, watchlist, watchlist_codes, save_watchlist, prefix="i",
                             load_stock_data=load_stock_data, add_indicators=add_indicators,
                             score_signal=score_signal, calc_stop_levels=calc_stop_levels,
                             make_chart=make_chart, show_detail=show_detail,
                             add_to_sim=add_to_sim, sim_qty=sim_qty)

    # 워치리스트 표시
    st.markdown("---")
    _render_watchlist(
        watchlist, save_watchlist,
        load_stock_data, add_indicators, score_signal,
        calc_stop_levels, make_chart,
        show_detail=show_detail,
    )


# ===== 📥 조용한 매집 (키움 수급) =====
@st.cache_data(ttl=60, show_spinner=False)
def _fetch_surge(min_rate: float, min_cs: float, min_vol: int) -> list:
    """키움 등락률상위(ka10027) → 등락률 min_rate↑ + 체결강도·거래량 조건 필터. 60초 캐시.
    키움 미연결(클라우드)이면 예외 → 호출측이 안내. 반환: dict 리스트(체결강도·거래량순)."""
    _autotrade_path()
    from kiwoom_api import from_secrets
    api = from_secrets()
    body = {"mrkt_tp": "000", "sort_tp": "1", "trde_qty_cnd": "0000", "stk_cnd": "0",
            "crd_cnd": "0", "updown_incls": "1", "pric_cnd": "0", "trde_prica_cnd": "0",
            "stex_tp": "1"}
    r = api._post("/api/dostk/rkinfo", "ka10027", body)
    out = []
    for x in r.get("pred_pre_flu_rt_upper", []):
        try:
            rate, cs, vol = float(x["flu_rt"]), float(x["cntr_str"]), int(x["now_trde_qty"])
        except Exception:
            continue
        if abs(rate) >= min_rate and cs >= min_cs and vol >= min_vol:
            out.append({"code": x["stk_cd"], "name": x["stk_nm"], "rate": rate,
                        "price": str(x["cur_prc"]).lstrip("+"), "cs": cs, "vol": vol})
    out.sort(key=lambda d: (d["cs"], d["vol"]), reverse=True)
    return out


_SURGE_THEMES = [
    ("이차전지", ["이차전지", "2차전지", "배터리", "양극", "음극", "전지", "리튬", "전해"]),
    ("반도체", ["반도체", "웨이퍼", "HBM", "패키지", "후공정", "소부장", "파운드리"]),
    ("AI·데이터", ["AI", "인공지능", "데이터", "클라우드", "머신러닝"]),
    ("바이오·제약", ["바이오", "제약", "의약", "진단", "치료", "신약", "백신", "항체", "세포"]),
    ("방산·우주항공", ["방산", "우주", "항공", "무기", "위성", "방위"]),
    ("조선·해양", ["조선", "해양", "선박", "플랜트", "기자재"]),
    ("로봇", ["로봇", "협동로봇", "자동화"]),
    ("원자력·에너지", ["원자력", "원전", "발전", "태양광", "풍력", "에너지", "전력", "수소", "열병합"]),
    ("화학·소재", ["화학", "소재", "필름", "화학물질"]),
    ("자동차", ["자동차", "차량", "전장", "타이어"]),
    ("엔터·콘텐츠", ["엔터", "미디어", "콘텐츠", "드라마", "음원", "게임", "웹툰"]),
    ("소비재", ["화장품", "뷰티", "의류", "패션", "식품", "음료"]),
    ("건설·부동산", ["건설", "건축", "토목", "부동산", "주택"]),
]


def _surge_common(rows: list) -> str:
    """급등 리스트 한 줄 브리핑: 주도 섹터 + 평균등락 + 상한가 + 매수세 흐름."""
    from collections import Counter
    cnt = Counter()
    for d in rows:
        text = stock_desc(d["code"], 40) + " " + d["name"]
        for theme, kws in _SURGE_THEMES:
            if any(k in text for k in kws):
                cnt[theme] += 1
                break
    n = len(rows) or 1
    avg_cs = sum(d["cs"] for d in rows) / n
    avg_rate = sum(d["rate"] for d in rows) / n
    limit_up = sum(1 for d in rows if d["rate"] >= 29.5)
    # 주도 섹터
    if cnt:
        theme, c = cnt.most_common(1)[0]
        if c >= 2 and c / n >= 0.25:
            sec = f"**{theme}** 섹터 주도 ({c}/{n}종목)"
        else:
            sec = "테마 분산 (" + " · ".join(f"{t} {v}" for t, v in cnt.most_common(3)) + ")"
    else:
        sec = "공통 테마 없음 (개별 이슈)"
    # 매수세 흐름(체결강도)
    if avg_cs >= 100:
        flow = "매수세 강함 · 흐름 살아있음"
    elif avg_cs >= 80:
        flow = "매수세 보통"
    else:
        flow = "매수세 식는 중 · 추격 주의"
    lu = f" · 상한가 {limit_up}종목" if limit_up else ""
    return (f"🔎 **오늘 급등 요약**: {sec} · 평균 +{avg_rate:.0f}%{lu} · "
            f"{flow}(체결강도 {avg_cs:.0f})")


def _render_surge_monitor():
    """실시간 급등주 모니터링(키움 등락률상위). ⚠️ 매수신호 아님 — 백테스트상 기대값 음(−)."""
    st.caption("📌 **🚨 실시간 급등** — 오늘 15%↑ + 체결강도·거래량 강한 종목 (키움 실시간)")
    st.error(
        "⚠️ **모니터링 전용 · 매수 신호 아님.** 소형주 25,360표본 백테스트상 '급등 낌새'로 "
        "매수 시 이후 20일 기대값 **음(−4~−6%)** 확인됨. 급등을 더 잡을수록 폭락도 더 잡혀 "
        "기대값이 악화됩니다. **'오늘 뭐가 터졌나' 참고용으로만** 보세요."
    )
    c1, c2, c3 = st.columns(3)
    min_rate = c1.slider("최소 등락률(%)", 10, 30, 15, key="surge_rate")
    min_cs = c2.slider("최소 체결강도", 50, 150, 85, key="surge_cs",
                       help="100↑=매수세 우위. 세력 개입 흔적")
    min_vol = c3.slider("최소 거래량(만주)", 0, 200, 50, key="surge_vol") * 10000
    if st.button("🔄 새로고침", key="surge_refresh"):
        _fetch_surge.clear()
    try:
        rows = _fetch_surge(float(min_rate), float(min_cs), int(min_vol))
    except Exception:
        st.warning("키움 미연결 — 실시간 급등은 **로컬(키움 설정)에서만** 조회됩니다. (클라우드 미지원)")
        return
    if not rows:
        st.info("조건을 충족한 급등 종목이 없습니다. (등락률 순위는 **장중**에만 값이 있어요)")
        return
    st.caption(f"총 {len(rows)}종목 · 체결강도·거래량순 · 60초 캐시 (새로고침으로 갱신)")
    tbl = ["| 종목 | 등락률 | 현재가 | 체결강도 | 거래량(만) |", "|---|---|---|---|---|"]
    for d in rows:
        nm = f"**{d['name']}**{desc_html(d['code'], 20)}"
        tbl.append(f"| {nm} | +{d['rate']:.1f}% | {d['price']} | {d['cs']:.0f} | {d['vol']//10000:,} |")
    st.markdown("\n".join(tbl), unsafe_allow_html=True)
    st.markdown("---")
    st.markdown(_surge_common(rows), unsafe_allow_html=True)


_ACCUM_SORTS = {
    "🔥 매집강도 높은순": (lambda x: x.get("intensity", 0), True),
    "🟢 저변동순(기복 적은)": (lambda x: x.get("vol", 99), False),
    "💪 체결강도 높은순": (lambda x: (x.get("cntr_str") or -1), True),
    "🔵 덜 오른순(20일수익↓)": (lambda x: x.get("ret20", 0), False),
    "🕐 매집 초기순(지속일↓)": (lambda x: x.get("accum_days", 0), False),
    "📊 매집점수 높은순": (lambda x: x.get("score", 0), True),
}


def _apply_sort_filter(rows, sort_key, f_lowvol, f_cntr, f_notrisen):
    """매집 결과에 정렬·필터 적용. 필터: 저변동(≤3.5%)·체결강도120↑·덜오른(20일≤10%)."""
    out = list(rows)
    if f_lowvol:
        out = [x for x in out if x.get("vol") is not None and x["vol"] <= 3.5]
    if f_cntr:
        out = [x for x in out if (x.get("cntr_str") or 0) >= 120]
    if f_notrisen:
        out = [x for x in out if x.get("ret20", 0) <= 10]
    kf, rev = _ACCUM_SORTS.get(sort_key, _ACCUM_SORTS["🔥 매집강도 높은순"])
    out.sort(key=kf, reverse=rev)
    return out


def _render_accumulation(load_stock_data=None, add_indicators=None, score_signal=None,
                         calc_stop_levels=None, make_chart=None, add_to_sim=None,
                         show_detail=None):
    """키움 투자자별 수급으로 사모·기관 '조용한 매집' 종목 발굴 (대형/중형 강도순). 참고용.
    add_to_sim: 카드 '📒 모의매수' 버튼. show_detail: 종목 클릭 시 풀 상세."""
    st.caption(
        "키움 투자자별 수급(ka10059)으로 **'조용한 매집'**(사모·기관 지속 순매수 + 개미 이탈) 종목을 "
        "대형/중형으로 나눠 **매집 강도순**으로 뽑습니다. "
        "⚠️ 백테스트상 지수를 이기진 못해 **자금배분용이 아닌 참고용(워치리스트)**입니다."
    )

    # 키움 모듈 지연 로드 (클라우드엔 autotrade/secrets 없을 수 있음)
    try:
        _autotrade_path()
        from accumulation import scan, TOP_N, LARGE_MAX, UNIV_RANK
        from kiwoom_api import from_secrets
    except Exception as e:
        st.warning(f"매집 모듈을 불러오지 못했습니다: {e}")
        return
    try:
        api = from_secrets()
    except Exception as e:
        st.warning(
            "키움 API 설정이 없어 매집 스캔을 할 수 없습니다. 로컬 `.streamlit/secrets.toml`의 "
            "`[kiwoom]`에 appkey/secretkey가 필요합니다(모의투자).\n\n"
            f"({e})"
        )
        return

    c1, c2 = st.columns([3, 1.4])
    c1.caption(
        f"유니버스: 시총 상위 {UNIV_RANK}종목 · "
        f"대형(1~{LARGE_MAX}위)/중형({LARGE_MAX+1}~{UNIV_RANK}위) 각 TOP{TOP_N}"
    )
    scan_btn = c2.button("🔍 매집 스캔 (1~2분)", type="primary", use_container_width=True)

    if "accum_time" not in st.session_state:
        st.session_state.accum_large = None
        st.session_state.accum_mid = None
        st.session_state.accum_time = None

    if scan_btn:
        pbar = st.progress(0.0, text="시세 프리필터 준비...")

        def cb(phase, done, total):
            frac = done / max(total, 1)
            if phase == "prefilter":
                pbar.progress(min(frac * 0.35, 0.35), text=f"시세 프리필터 {done}/{total}")
            elif phase == "cntr":
                pbar.progress(0.9 + min(frac * 0.1, 0.1),
                              text=f"체결강도(매수세) 확인 {done}/{total}")
            else:  # supply
                pbar.progress(0.35 + min(frac * 0.55, 0.55),
                              text=f"키움 수급 확인 {done}/{total}")

        try:
            api._ensure_token()
            large, mid = scan(api, progress=cb)
        except Exception as e:
            pbar.empty()
            st.error(f"매집 스캔 실패: {e}")
            return
        pbar.empty()
        st.session_state.accum_large = large
        st.session_state.accum_mid = mid
        st.session_state.accum_time = datetime.now()
        st.rerun()

    if st.session_state.accum_time:
        ago = (datetime.now() - st.session_state.accum_time).total_seconds() / 60
        st.caption(f"마지막 매집 스캔: {st.session_state.accum_time.strftime('%H:%M')} ({int(ago)}분 전)")

    large = st.session_state.accum_large
    mid = st.session_state.accum_mid
    if large is None:
        st.info("위 '🔍 매집 스캔' 버튼을 눌러 조용한 매집 종목을 찾아보세요. (첫 스캔 1~2분)")
        return

    watchlist = load_watchlist()
    watchlist_codes = {w["code"] for w in watchlist}
    starred = load_starred()

    # ----- 🔎 정렬 · 필터 (스캔 결과 맨 위) -----
    st.markdown("#### 🔎 정렬 · 필터")
    sc1, sc2 = st.columns([2, 3])
    sort_key = sc1.selectbox("정렬", list(_ACCUM_SORTS.keys()), key="accum_sort")
    with sc2:
        st.caption("필터")
        fc = st.columns(5)
        f_lowvol = fc[0].checkbox("🟢 저변동만", key="accum_f_lowvol",
                                  help="일간 변동성 ≤3.5% (기복 적은 것만)")
        f_cntr = fc[1].checkbox("💪 체결강도120↑", key="accum_f_cntr",
                                help="매수세 강한 것만(장중값). 마감 후·조회실패면 제외될 수 있음")
        f_notrisen = fc[2].checkbox("🔵 덜오른것만", key="accum_f_notrisen",
                                    help="20일 수익률 ≤10% (매집 초기 타이밍)")
        f_uptick = fc[3].checkbox("🚀 상승초입만", key="accum_f_uptick",
                                  help="세력 매수 지속 + 개미 복귀(최근10일) — 검증상 강세장 가장 강한 국면(+6.3%·승률58%)")
        f_star = fc[4].checkbox("⭐ 별표만", key="accum_f_star",
                                help="별표(⭐) 표시한 종목만 보기")
    large = _apply_sort_filter(large, sort_key, f_lowvol, f_cntr, f_notrisen)
    mid = _apply_sort_filter(mid, sort_key, f_lowvol, f_cntr, f_notrisen)
    if f_uptick:   # 상승초입 = 세력 최근순매수(rs>0) + 개미 복귀(ri>0)
        _up = lambda x: x.get("recent_smart", 0) > 0 and x.get("recent_ind", 0) > 0
        large = [x for x in large if _up(x)]
        mid = [x for x in mid if _up(x)]
    if f_star:
        large = [x for x in large if x["code"] in starred]
        mid = [x for x in mid if x["code"] in starred]

    st.markdown("---")
    with st.expander("ℹ️ 지표 읽는 법 (처음이면 펼쳐보세요)"):
        st.markdown(
            "- **🔥 강도**: 하루 거래량의 며칠치를 스마트머니가 쓸어담았나. **높을수록 강한 매집**.\n"
            "- **🏛️ 매집 주체**: 사모·금융투자·투신·기관 중 20일 지속 순매수한 곳. **많을수록 광범위**.\n"
            "- **📈 타이밍**: 20일 수익률. **아직 안 오른(🔵) 게 좋음** — 이미 오른(🔴) 건 매집 후반.\n"
            "- **🟢 안정도(변동성)**: 일간 변동성. **낮을수록 기복 적어 보유 편함**. "
            "검증상 저변동 매집주가 이후 낙폭 얕고 수익 우위(변동성↔낙폭 상관 -0.68), "
            "고변동(>6%)은 손실+깊은낙폭이라 **목록에서 자동 제외**.\n"
            "- **체결강도**: 실시간 매수세(누적 매수/매도 체결량). **120↑=강한 매집 진행 확인** · "
            "100↓=매수세 약함. *장중에만 유효(마감 후엔 당일 최종값).*\n"
            "- **개미 이탈**: 개인이 순매도(빠짐) = 조용한 매집의 좋은 신호(목록 전체가 이 조건 충족).\n"
            "- **🚦 국면(최근 흐름)**: 🟢상승초입(세력매수+개미유입) · 🟢매집지속(세력매수·개미소외) · "
            "🔴이탈주의(세력 최근 멈춤). *백테스트 검증(강세장 기준), 하락장 미검증.*\n"
            "- ⚠️ 매수신호가 아니라 **관심 후보**입니다. 진입은 차트·추세/반등 신호를 따로 확인한 뒤에."
        )
    sim_qty = 10
    if add_to_sim:
        sim_qty = int(st.number_input(
            "📒 모의매수 기본 수량(주)", min_value=1, value=10, step=1, key="accum_sim_qty",
            help="카드의 '📒 모의매수' 버튼을 누르면 오늘 종가로 이 수량만큼 담깁니다. "
                 "📒 모의매수 메뉴에서 지수(KODEX200) 대비 성과를 추적하세요."))
    _render_accum_section("🏛️ 대형주 매집", large, watchlist, watchlist_codes, "L",
                          "광범위 기관매집(사모+금융투자+투신+기관) 유효 · 검증 사모 +5.4%p",
                          add_to_sim=add_to_sim, sim_qty=sim_qty, starred=starred, show_detail=show_detail)
    st.markdown("")
    _render_accum_section("🏗️ 중형주 매집", mid, watchlist, watchlist_codes, "M",
                          "사모 주력(+4.3%p) · 외국인 지속매수는 역신호라 제외",
                          add_to_sim=add_to_sim, sim_qty=sim_qty, starred=starred, show_detail=show_detail)


def _accum_stage(days):
    """매집 지속일수(N일째) → 단계 배지. 근거: 라이프사이클 백테스트(accumulate_lifecycle.py)
    강세장 279이벤트 = 매집 진입 후 중앙 7일째·+8%에서 고점, 8일째 세력이탈(후행)."""
    if not days:
        return None
    if days <= 3:
        return f"🟢 매집 {days}일째 (초기) — 검증상 고점(중앙 7일)까지 여유"
    if days <= 7:
        return f"🟡 매집 {days}일째 (중반) — 검증상 고점 임박(중앙 7일·+8%)"
    return f"🔴 매집 {days}일째 (후반) — 검증상 고점(중앙 7일) 경과, 세력이탈·차익 주의"


def _accum_badges(f):
    """종목 지표를 직관 배지(강도🔥 · 매집주체🏛️ · 타이밍🔵)로 변환."""
    inten = f["intensity"]
    if inten >= 5:
        fire = "🔥🔥🔥 매우 강함"
    elif inten >= 2:
        fire = "🔥🔥 강함"
    else:
        fire = "🔥 보통"
    n = len(f["smart"])
    if f["score"] >= 9:
        org = f"🏛️ 기관 총동원 ({n}곳)"
    elif f["score"] >= 7:
        org = f"🏛️ 기관 다수 ({n}곳)"
    else:
        org = f"🏛️ 사모 주도 ({n}곳)"
    r = f["ret20"]
    if r <= 3:
        timing = f"🔵 아직 초입 ({r:+.1f}%)"
    elif r <= 10:
        timing = f"🟡 상승 진행 ({r:+.1f}%)"
    else:
        timing = f"🔴 이미 상승 ({r:+.1f}%)"
    return fire, org, timing


def _accum_phase(f):
    """최근 절반(뒤 10일) 흐름으로 매집 국면 판정. 옛 결과(시점 필드 없음)면 None.
    ⚠️ 방향은 백테스트(validate_recent_flow.py, 키움 100일=강세장)로 검증:
       · 세력 최근이탈(recent_smart≤0) → 이후 20일 중앙값 −3.8%·승률 41%(피해야)
       · 세력매수+개미복귀(recent_ind>0) → 중앙값 +6.3%·승률 58%(가장 강함)
       · 세력매수+개미소외 → 중앙값 +0.5%(평범·안전)
       (개미복귀가 좋은 건 강세장 특성일 수 있음 — 하락장 미검증)"""
    if "recent_smart" not in f:
        return None
    rs, ri = f["recent_smart"], f["recent_ind"]
    if rs <= 0:
        return ":red[**🔴 이탈 주의**]", "최근 세력이 순매수를 멈췄거나 이탈 — 검증상 이후 손실 확률↑"
    if ri > 0:
        return ":green[**🟢 상승 초입**]", "세력 매수 지속 + 개미 유입 시작 — 검증상 가장 강한 국면(강세장 기준)"
    return ":green[**🟢 매집 지속**]", "세력이 최근에도 순매수 · 개미는 아직 소외(안전한 매집 진행)"


def _accum_stability(f) -> str:
    """진입시점 일간 변동성 → 안정도 배지. 검증(accumulate_volatility.py, 매집 356건):
    저변동일수록 이후 낙폭 얕고 수익 우위(변동성↔낙폭 상관 -0.68). 고변동(>6%)은 스캔서 이미 제외."""
    vol = f.get("vol")
    if vol is None:
        return ""
    if vol <= 3.5:
        return f"🟢 안정적 (일변동 {vol:.1f}% · 기복 적어 보유 편함)"
    if vol <= 5.0:
        return f"🟡 변동 보통 (일변동 {vol:.1f}%)"
    return f"🟠 변동 다소 큼 (일변동 {vol:.1f}% · 기복 주의)"


def _accum_cntr_str(f) -> str:
    """실시간 체결강도(매수세) → 매집 확인 배지. 100↑=매수우위, 120↑=강한 매집 진행중.
    ⚠️ 장중에만 유효(마감 후엔 당일 최종값), 조회 실패/미개장이면 표시 생략."""
    cs = f.get("cntr_str")
    if not cs:
        return ""
    if cs >= 120:
        return f"🟢 체결강도 {cs:.0f} (매수세 우위 · 매집 진행 확인)"
    if cs >= 100:
        return f"🟡 체결강도 {cs:.0f} (매수세 약우위)"
    return f"⚪ 체결강도 {cs:.0f} (매수세 약함 · 매집 식는 중일 수 있음)"


def _render_accum_section(title, rows, watchlist, watchlist_codes, prefix, note,
                          add_to_sim=None, sim_qty=10, starred=None, show_detail=None):
    """매집 결과 한 섹션(대형/중형): 종목별 카드 + 직관 배지 + 관심종목/모의매수/별표.
    add_to_sim(code,name,qty,price,note)->bool: 있으면 '📒 모의매수' 버튼 표시(price=0→오늘종가).
    starred(set): 별표(⭐) 코드 집합. show_detail(code,name): 종목명 클릭 시 풀 상세(발굴·포트폴리오와 동일)."""
    starred = starred if starred is not None else set()
    st.markdown(f"### {title} ({len(rows)}개)")
    st.caption(note)
    if not rows:
        st.info("조건을 충족한 종목이 없습니다.")
        return
    for i, f in enumerate(rows):
        fire, org, timing = _accum_badges(f)
        phase = _accum_phase(f)
        with st.container(border=True):
            if add_to_sim:
                top = st.columns([2.6, 1.15, 1.35, 0.5])
            else:
                top = st.columns([3.0, 1.3, 0.5])
            _star = "⭐ " if f["code"] in starred else ""
            _issel = st.session_state.get("accum_sel_code") == f["code"]
            if show_detail:
                if top[0].button(f"{i+1}. {_star}{f['name']} ({f['code']})",
                                 key=f"accum_name_{prefix}_{i}", use_container_width=True,
                                 type="primary" if _issel else "secondary"):
                    st.session_state["accum_sel_code"] = None if _issel else f["code"]
                    st.rerun()
                _dsc = stock_desc(f["code"])
                top[0].caption(f"시총 {f['rank']}위" + (f" · {_dsc}" if _dsc else ""))
            else:
                top[0].markdown(f"**{i+1}. {_star}{f['name']}**　`{f['code']}` · 시총 {f['rank']}위{desc_html(f['code'])}",
                                unsafe_allow_html=True)
            if phase:
                top[0].markdown(phase[0])
                top[0].caption(phase[1])
            _stage = _accum_stage(f.get("accum_days"))
            if _stage:
                top[0].markdown(_stage)
            if f["code"] in watchlist_codes:
                top[1].markdown("✓ 담김")
            elif top[1].button("➕ 관심추가", key=f"accum_add_{prefix}_{i}",
                               use_container_width=True):
                watchlist.append({
                    "code": f["code"],
                    "name": f["name"],
                    "added_at": datetime.now().isoformat(),
                    "note": f"매집 강도 {f['intensity']:.1f}d·점수 {f['score']}",
                })
                save_watchlist(watchlist)
                st.rerun()
            # 📒 모의매수 바로 담기 (콜백은 app.py 소유 — sim_portfolio.json). price=0→오늘 종가 자동
            if add_to_sim:
                if top[2].button("📒 모의매수", key=f"accum_sim_{prefix}_{i}",
                                 use_container_width=True,
                                 help=f"오늘 종가로 {sim_qty}주 모의매수 담기(📒 모의매수 메뉴에서 추적)"):
                    _note = f"매집 강도 {f['intensity']:.1f}d·변동성 {f.get('vol', 0):.1f}%"
                    ok = add_to_sim(f["code"], f["name"], sim_qty, 0, _note)
                    if ok:
                        st.success(f"📒 {f['name']} {sim_qty}주 모의매수 담음 (📒 모의매수 메뉴에서 확인)")
                    else:
                        st.error("현재가를 가져오지 못해 담지 못했습니다.")
            # ⭐ 별표 토글 (보기용 즐겨찾기)
            _star_col = top[3] if add_to_sim else top[2]
            _is_star = f["code"] in starred
            if _star_col.button("⭐" if _is_star else "☆", key=f"accum_star_{prefix}_{i}",
                                use_container_width=True,
                                help="별표 해제" if _is_star else "별표(보기용 표시)"):
                toggle_starred(f["code"])
                st.rerun()
            b = st.columns(3)
            b[0].markdown(fire)
            b[0].caption(f"강도 {f['intensity']:.1f}일치")
            b[1].markdown(org)
            b[1].caption("·".join(f["smart"]))
            b[2].markdown(timing)
            b[2].caption(f"개미 {f['ind']/1e4:+,.0f}만주 이탈")
            # 안정도(변동성)·체결강도(매수세) — 사용자 실전피드백 반영(저변동+체결강도로 매집 확인)
            extra = [x for x in (_accum_stability(f), _accum_cntr_str(f)) if x]
            if extra:
                st.markdown("　".join(extra))
            # 종목명 클릭 시 풀 상세(발굴·포트폴리오와 동일 구조) 펼침
            if show_detail and _issel:
                with st.container(border=True):
                    show_detail(f["code"], f["name"])


def _render_result_table(results, watchlist, watchlist_codes, save_fn, prefix="",
                         load_stock_data=None, add_indicators=None, score_signal=None,
                         calc_stop_levels=None, make_chart=None, show_detail=None,
                         add_to_sim=None, sim_qty=10):
    """발굴 결과 테이블. 종목명 클릭 시 그 행 바로 아래에 상세를 아코디언처럼 펼침.
    add_to_sim: 있으면 '📒' 모의매수 버튼 열 추가(현재가로 sim_qty주 담기)."""
    # 헤더
    COL_RATIOS = [3, 3, 1.5, 2, 1.2, 1.5, 1.2] if add_to_sim else [3, 3, 1.5, 2, 1.2, 1.5]
    h = st.columns(COL_RATIOS)
    h[0].caption("종목")
    h[1].caption("매매 신호")
    h[2].caption("점수")
    h[3].caption("현재가")
    h[4].caption("RSI")
    h[5].caption("관심종목")
    if add_to_sim:
        h[6].caption("모의매수")

    for i, r in enumerate(results):
        cols = st.columns(COL_RATIOS)
        # 종목명 클릭 → 이 행 바로 아래에 상세 분석 토글
        is_sel = st.session_state.get("sc_selected_code") == r["code"]
        if cols[0].button(f"{r['name']} ({r['code']})", key=f"sc_btn_{prefix}_{i}",
                          use_container_width=True,
                          type="primary" if is_sel else "secondary"):
            if is_sel:
                st.session_state.sc_selected_code = None
            else:
                st.session_state.sc_selected_code = r["code"]
                st.session_state.sc_selected_name = r["name"]
            st.rerun()
        _d = stock_desc(r["code"])
        if _d:
            cols[0].caption(_d)
        # 재무 건전성 배지+이유(반등 모드에서만 존재)
        _h = r.get("health")
        if _h:
            cols[0].caption(f"🩺 {_h['grade']} · {_h['reason']}")
        verdict_txt = r["verdict"] + (f"  {_h['grade']}" if _h else "")
        cols[1].write(verdict_txt)
        cols[2].write(f"{r['score']}/100")
        cols[3].write(f"{r['price']:,}원")
        cols[4].write(f"{r['rsi']:.1f}")

        in_wl = r["code"] in watchlist_codes
        if in_wl:
            cols[5].markdown("✓ 추가됨")
        else:
            if cols[5].button("➕ 추가", key=f"add_{prefix}_{i}"):
                watchlist.append({
                    "code": r["code"],
                    "name": r["name"],
                    "added_at": datetime.now().isoformat(),
                    "score_at_add": r["score"],
                })
                save_fn(watchlist)
                st.rerun()

        # 📒 모의매수 바로 담기 (현재가로 sim_qty주) — 콜백은 app.py 소유(sim_portfolio.json)
        if add_to_sim:
            if cols[6].button("📒", key=f"sim_{prefix}_{i}",
                              help=f"현재가로 {sim_qty}주 모의매수 담기(📒 모의매수 메뉴에서 추적)"):
                ok = add_to_sim(r["code"], r["name"], sim_qty, r.get("price", 0),
                                f"발굴 {r['score']}점")
                if ok:
                    st.success(f"📒 {r['name']} {sim_qty}주 모의매수 담음 (📒 모의매수 메뉴에서 확인)")
                else:
                    st.error("현재가를 가져오지 못해 담지 못했습니다.")

        # 클릭된 행 바로 아래에 상세 분석 펼치기 (아코디언)
        if is_sel and load_stock_data:
            def _close_sc():
                st.session_state.sc_selected_code = None
            with st.container(border=True):
                if show_detail:   # 단일분석과 동일한 풀 상세
                    if st.button("✕ 닫기", key=f"close_sc_{prefix}_{i}", use_container_width=True):
                        _close_sc(); st.rerun()
                    show_detail(r["code"], r["name"])
                else:
                    _render_stock_detail(
                        r["code"], r["name"],
                        load_stock_data, add_indicators, score_signal,
                        calc_stop_levels, make_chart,
                        close_cb=_close_sc, key_suffix=f"sc_{prefix}_{i}",
                    )


def _render_stock_detail(code, name, load_stock_data, add_indicators, score_signal,
                         calc_stop_levels=None, make_chart=None,
                         score_at_add=None, close_cb=None, key_suffix=""):
    """종목 상세 분석 패널 (발굴 결과·관심종목 공용)."""
    st.markdown("---")
    head_cols = st.columns([6, 1])
    head_cols[0].markdown(f"### 📊 {name} ({code}) 상세 분석")
    if head_cols[1].button("✕ 닫기", use_container_width=True, key=f"close_{key_suffix}"):
        if close_cb:
            close_cb()
        st.rerun()

    with st.spinner("분석 중..."):
        df = load_stock_data(code)
        if df.empty or len(df) < 60:
            st.error(f"{code} 데이터를 가져올 수 없습니다.")
            return
        df = add_indicators(df)
        result = score_signal(df)

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    cur = int(last["Close"])
    day_chg = (last["Close"] - prev["Close"]) / prev["Close"] * 100
    rsi = float(last.get("RSI", 0) or 0)

    score_now = result["total"]
    score_delta = (score_now - score_at_add) if score_at_add else None

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("현재가", f"{cur:,}원", f"{day_chg:+.2f}%")
    m2.metric("점수", f"{score_now}/100",
              f"{score_delta:+}점 (추가 후)" if score_delta is not None else None)
    m3.metric("매매 신호", result["verdict"])
    m4.metric("RSI", f"{rsi:.1f}")

    candle_obs = analyze_candle(df)
    if candle_obs:
        st.markdown("**🕯️ 오늘의 캔들 특이사항**")
        for obs in candle_obs:
            st.markdown(f"- {obs}")

    st.markdown("**🔬 분석 상세**")
    for k, v in result["details"].items():
        if isinstance(v, tuple) and len(v) == 2:
            s, msg = v
            st.write(f"- **{k}**: {s}점 — {msg}")
        else:
            st.write(f"- **{k}**: {v}")

    if calc_stop_levels:
        st.markdown("**💰 지금 매수한다면 손절 / 익절**")
        levels = calc_stop_levels(cur)
        sc1, sc2 = st.columns(2)
        sc1.markdown("🛑 **손절가**")
        for k, v in levels.items():
            if "손절" in k:
                sc1.write(f"{k}: {v:,}원")
        sc2.markdown("🎯 **익절가**")
        for k, v in levels.items():
            if "익절" in k:
                sc2.write(f"{k}: {v:,}원")

    if make_chart:
        st.markdown("**📈 차트**")
        st.plotly_chart(make_chart(df, name), use_container_width=True,
                        key=f"chart_{key_suffix}")


def _render_watchlist(
    watchlist, save_fn,
    load_stock_data=None, add_indicators=None, score_signal=None,
    calc_stop_levels=None, make_chart=None, show_detail=None,
):
    """관심종목 (워치리스트) 표시 + 클릭 상세 분석. show_detail 있으면 풀 상세 콜백 사용."""
    st.markdown(f"### 💾 관심종목 ({len(watchlist)}개)")
    if not watchlist:
        st.caption("발굴된 종목 옆 '➕ 추가' 버튼으로 관심종목을 모을 수 있어요.")
        return

    # ----- 리스트 (포트폴리오 관리와 동일하게 종목명 클릭으로 상세) -----
    COL_RATIOS = [3, 1.8, 1.8, 1.4]
    h = st.columns(COL_RATIOS)
    h[0].caption("종목 (클릭 시 상세 분석)")
    h[1].caption("추가일")
    h[2].caption("추가 당시 점수")
    h[3].caption("")

    for i, w in enumerate(watchlist):
        cols = st.columns(COL_RATIOS)
        # 종목명 자체를 버튼으로 → 클릭하면 상세 분석 패널 토글
        is_selected = st.session_state.get("wl_selected_code") == w["code"]
        btn_label = f"{w.get('name', w['code'])} ({w['code']})"
        if cols[0].button(
            btn_label,
            key=f"wl_btn_{i}",
            use_container_width=True,
            type="primary" if is_selected else "secondary",
        ):
            if is_selected:
                # 같은 종목 다시 누르면 닫기
                st.session_state.wl_selected_code = None
            else:
                st.session_state.wl_selected_code = w["code"]
                st.session_state.wl_selected_name = w.get("name", w["code"])
            st.rerun()
        _dw = stock_desc(w["code"])
        if _dw:
            cols[0].caption(_dw)

        try:
            added = datetime.fromisoformat(w.get("added_at", "")).strftime("%m/%d %H:%M")
        except Exception:
            added = "-"
        cols[1].write(added)
        cols[2].write(f"{w.get('score_at_add', '-')}/100" if w.get("score_at_add") else "-")
        # 삭제 (포트폴리오 관리와 동일한 '🗑️ 삭제' 스타일로 통일)
        if cols[3].button("🗑️ 삭제", key=f"wl_del_{i}", use_container_width=True):
            if st.session_state.get("wl_selected_code") == w["code"]:
                st.session_state.wl_selected_code = None
            watchlist.pop(i)
            save_fn(watchlist)
            st.rerun()

    # ----- 상세 분석 패널 -----
    selected_code = st.session_state.get("wl_selected_code")
    if not selected_code or not load_stock_data:
        return

    # 워치리스트에서 해당 종목 찾기
    selected_w = next((w for w in watchlist if w["code"] == selected_code), None)
    if not selected_w:
        st.session_state.wl_selected_code = None
        return

    selected_name = st.session_state.get("wl_selected_name", selected_code)

    def _close_wl():
        st.session_state.wl_selected_code = None

    if show_detail:   # 단일분석과 동일한 풀 상세
        if st.button("✕ 닫기", key=f"close_wl_{selected_code}", use_container_width=True):
            _close_wl(); st.rerun()
        show_detail(selected_code, selected_name)
    else:
        _render_stock_detail(
            selected_code, selected_name,
            load_stock_data, add_indicators, score_signal,
            calc_stop_levels, make_chart,
            score_at_add=selected_w.get("score_at_add"),
            close_cb=_close_wl, key_suffix=f"wl_{selected_code}",
        )
