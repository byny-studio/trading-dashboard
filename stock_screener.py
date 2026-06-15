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
from datetime import datetime

import FinanceDataReader as fdr
import streamlit as st

from candle_analyzer import analyze_candle


WATCHLIST_FILE = "watchlist.json"

DEFAULT_THRESHOLD = 72   # 매수 관심 (강함) 이상 — 100점 만점 기준
STRONG_THRESHOLD = 80    # 강력 매수 — 100점 만점 기준
MAX_SCORE = 100


# ===== 워치리스트 (관심종목) =====
def load_watchlist() -> list:
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
def render_screener(
    load_stock_data,
    add_indicators,
    score_signal,
    calc_stop_levels=None,
    make_chart=None,
):
    """종목 발굴 화면."""
    st.title("🔭 종목 발굴")
    st.caption(
        "KOSPI 200 + KOSDAQ 150 약 350개 종목 중 매수 신호가 강한 종목을 찾습니다. "
        "첫 스캔은 2-4분 정도 걸려요."
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
        st.caption(
            f"마지막 스캔: {st.session_state.screen_time.strftime('%H:%M')} "
            f"({int(ago)}분 전, 임계값 {st.session_state.screen_threshold}점)"
        )

    # ----- 스캔 실행 -----
    if scan_btn:
        universe = get_screening_universe()
        total = len(universe)
        if total == 0:
            st.error("종목 리스트를 가져오지 못했습니다. 인터넷 연결 확인.")
            return

        results = []
        progress = st.progress(0.0, text=f"분석 중... 0/{total}")
        for i, stock in enumerate(universe):
            try:
                df = load_stock_data(stock["code"])
                if df.empty or len(df) < 60:
                    continue
                df = add_indicators(df)
                result = score_signal(df)
                if result["total"] >= threshold:
                    results.append({
                        "code": stock["code"],
                        "name": stock["name"],
                        "market": stock["market"],
                        "score": result["total"],
                        "verdict": result["verdict"],
                        "price": int(df.iloc[-1]["Close"]),
                        "rsi": float(df.iloc[-1].get("RSI", 0) or 0),
                    })
            except Exception:
                pass
            progress.progress((i + 1) / total, text=f"분석 중... {i + 1}/{total}")
        progress.empty()

        results.sort(key=lambda x: -x["score"])
        st.session_state.screen_results = results
        st.session_state.screen_time = datetime.now()
        st.session_state.screen_threshold = threshold
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

    # 강력 매수 섹션
    if strong:
        st.markdown(f"### 🚀 강력 매수 ({len(strong)}개)")
        _render_result_table(strong, watchlist, watchlist_codes, save_watchlist, prefix="s")

    # 매수 관심 섹션
    if interest:
        if strong:
            st.markdown("")
        st.markdown(f"### ✅ 매수 관심 ({len(interest)}개)")
        _render_result_table(interest, watchlist, watchlist_codes, save_watchlist, prefix="i")

    # 워치리스트 표시
    st.markdown("---")
    _render_watchlist(
        watchlist, save_watchlist,
        load_stock_data, add_indicators, score_signal,
        calc_stop_levels, make_chart,
    )


def _render_result_table(results, watchlist, watchlist_codes, save_fn, prefix=""):
    """발굴 결과 테이블."""
    # 헤더
    COL_RATIOS = [3, 3, 1.5, 2, 1.2, 1.5]
    h = st.columns(COL_RATIOS)
    h[0].caption("종목")
    h[1].caption("매매 신호")
    h[2].caption("점수")
    h[3].caption("현재가")
    h[4].caption("RSI")
    h[5].caption("관심종목")

    for i, r in enumerate(results):
        cols = st.columns(COL_RATIOS)
        cols[0].write(f"**{r['name']}** ({r['code']})")
        cols[1].write(r["verdict"])
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


def _render_watchlist(
    watchlist, save_fn,
    load_stock_data=None, add_indicators=None, score_signal=None,
    calc_stop_levels=None, make_chart=None,
):
    """관심종목 (워치리스트) 표시 + 클릭 상세 분석."""
    st.markdown(f"### 💾 관심종목 ({len(watchlist)}개)")
    if not watchlist:
        st.caption("발굴된 종목 옆 '➕ 추가' 버튼으로 관심종목을 모을 수 있어요.")
        return

    # ----- 리스트 (포트폴리오 관리와 동일하게 종목명 클릭으로 상세) -----
    COL_RATIOS = [3, 2, 2, 1]
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

        try:
            added = datetime.fromisoformat(w.get("added_at", "")).strftime("%m/%d %H:%M")
        except Exception:
            added = "-"
        cols[1].write(added)
        cols[2].write(f"{w.get('score_at_add', '-')}/100" if w.get("score_at_add") else "-")
        if cols[3].button("🗑️", key=f"wl_del_{i}"):
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

    st.markdown("---")
    head_cols = st.columns([6, 1])
    head_cols[0].markdown(f"### 📊 {selected_name} ({selected_code}) 상세 분석")
    if head_cols[1].button("✕ 닫기", use_container_width=True):
        st.session_state.wl_selected_code = None
        st.rerun()

    with st.spinner("분석 중..."):
        df = load_stock_data(selected_code)
        if df.empty or len(df) < 60:
            st.error(f"{selected_code} 데이터를 가져올 수 없습니다.")
            return
        df = add_indicators(df)
        result = score_signal(df)

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    cur = int(last["Close"])
    day_chg = (last["Close"] - prev["Close"]) / prev["Close"] * 100
    rsi = float(last.get("RSI", 0) or 0)

    # 메트릭 4개 (점수 변화 표시)
    score_now = result["total"]
    score_at_add = selected_w.get("score_at_add")
    score_delta = (score_now - score_at_add) if score_at_add else None

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("현재가", f"{cur:,}원", f"{day_chg:+.2f}%")
    m2.metric(
        "점수",
        f"{score_now}/100",
        f"{score_delta:+}점 (추가 후)" if score_delta is not None else None,
    )
    m3.metric("매매 신호", result["verdict"])
    m4.metric("RSI", f"{rsi:.1f}")

    # 캔들 특이사항 — "왜 오늘 이렇게 움직였나"
    candle_obs = analyze_candle(df)
    if candle_obs:
        st.markdown("**🕯️ 오늘의 캔들 특이사항**")
        for obs in candle_obs:
            st.markdown(f"- {obs}")

    # 점수 상세
    st.markdown("**🔬 분석 상세**")
    for k, v in result["details"].items():
        # v 가 (점수, 메시지) 튜플인 경우와 단순 문자열인 경우 모두 처리
        if isinstance(v, tuple) and len(v) == 2:
            s, msg = v
            st.write(f"- **{k}**: {s}점 — {msg}")
        else:
            st.write(f"- **{k}**: {v}")

    # 지금 매수 시 손절/익절
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

    # 차트
    if make_chart:
        st.markdown("**📈 차트**")
        st.plotly_chart(
            make_chart(df, selected_name), use_container_width=True
        )
