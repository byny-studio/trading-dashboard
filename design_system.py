"""
Lily 의 기본 디자인 시스템 v2
- 폰트: Pretendard (한국어 친화)
- 라이트/다크 자동 전환 (시스템 설정 기준)
- v2 변경: 다크모드에서 입력칸·버튼·헤더가 안 보이는 문제 수정
- 사용법: app.py 의 st.set_page_config(...) 바로 아래에 두 줄 추가

    from design_system import inject_css
    inject_css()
"""

import streamlit as st


# ===== 색 토큰 (다른 코드에서도 import 해서 쓸 수 있음) =====
COLORS_LIGHT = {
    "bg": "#FAFAFA",
    "surface": "#FFFFFF",
    "border": "#E5E5EA",
    "text": "#18181B",
    "text_muted": "#71717A",
    "accent": "#4F46E5",
    "success": "#16A34A",
    "warning": "#F59E0B",
    "up": "#E11D48",
    "down": "#1D4ED8",
}

COLORS_DARK = {
    "bg": "#0F0F11",
    "surface": "#1F1F23",
    "border": "#2E2E33",
    "text": "#FAFAFA",
    "text_muted": "#A1A1AA",
    "accent": "#818CF8",
    "success": "#22C55E",
    "warning": "#FBBF24",
    "up": "#FB7185",
    "down": "#60A5FA",
}


_CSS = """
<style>
/* ========== Streamlit 기본 상단 UI 정리 ========== */
/* 상단 툴바(Deploy·GitHub·⋮ 메뉴)와 색상 띠 숨김 */
/* Deploy 버튼·설정 메뉴·색상 띠만 숨김 (헤더 자체는 유지) */
[data-testid="stAppDeployButton"] { display: none !important; }
#MainMenu { visibility: hidden !important; }
[data-testid="stDecoration"] { display: none !important; }
/* ⚠️ 사이드바(메뉴) 열기 버튼은 항상 보이게 — 모바일에서 메뉴 접근 */
[data-testid="stSidebarCollapsedControl"],
[data-testid="collapsedControl"] {
    display: flex !important;
    visibility: visible !important;
}
/* 입력칸 포커스 시 뜨는 'Press Enter to submit form' 안내문 숨김 */
[data-testid="InputInstructions"] { display: none !important; }

/* 본문 상단 여백 정리 — 제목 시작선을 사이드바 메뉴와 같은 높이로 */
.block-container,
[data-testid="stMainBlockContainer"] {
    padding-top: 3rem !important;
}
[data-testid="stSidebar"] [data-testid="stSidebarUserContent"] {
    padding-top: 1.5rem !important;
}

/* ========== Pretendard 폰트 ========== */
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css');

/* ========== 라이트 모드 변수 ========== */
:root {
    --ds-bg: #FAFAFA;
    --ds-surface: #FFFFFF;
    --ds-surface-2: #F4F4F5;
    --ds-border: #E5E5EA;
    --ds-text: #18181B;
    --ds-text-muted: #71717A;
    --ds-accent: #4F46E5;
    --ds-success: #16A34A;
    --ds-warning: #F59E0B;
    --ds-up: #E11D48;
    --ds-down: #1D4ED8;
}

/* ========== Pretendard 전체 적용 (아이콘 폰트 제외) ========== */
html, body, .stApp, .stMarkdown, .stMarkdown p,
button, input, textarea, select, p, label,
h1, h2, h3, h4, h5, h6, .stDataFrame,
[data-testid="stMarkdownContainer"],
[data-testid="stMarkdownContainer"] p,
[data-testid="stMarkdownContainer"] li,
[data-testid="stMarkdownContainer"] span:not([class*="material"]):not([class*="icon"]) {
    font-family: 'Pretendard', -apple-system, BlinkMacSystemFont,
                 'Apple SD Gothic Neo', 'Segoe UI', sans-serif !important;
}

/* === 아이콘 폰트는 절대 건드리지 않기 === */
.material-symbols-rounded,
.material-symbols-outlined,
.material-symbols-sharp,
.material-icons,
.material-icons-outlined,
[class*="material-symbols"],
[class*="material-icons"],
[class*="MuiIcon"],
i[class*="icon"],
span[class*="icon"]:not([class*="iconify"]) {
    font-family: 'Material Symbols Rounded', 'Material Icons',
                 'Material Symbols Outlined' !important;
    font-feature-settings: 'liga' !important;
}

h1, h2, h3, h4, h5, h6 {
    font-weight: 500 !important;
    letter-spacing: -0.01em !important;
}
h1 { font-size: 22px !important; }
h2 { font-size: 18px !important; }
h3 { font-size: 16px !important; }

/* ========== 공통 컴포넌트 스타일 ========== */
/* 입력칸과 버튼이 같은 높이가 되도록 통일 */
:root {
    --ds-control-h: 42px;
}

/* === 버튼 === */
.stButton > button,
.stDownloadButton > button,
[data-testid="baseButton-primary"],
[data-testid="baseButton-secondary"],
[data-testid="baseButton-primaryFormSubmit"],
[data-testid="baseButton-secondaryFormSubmit"] {
    height: var(--ds-control-h) !important;
    min-height: var(--ds-control-h) !important;
    border-radius: 8px !important;
    font-weight: 500 !important;
    font-size: 14px !important;
    padding: 0 16px !important;
    line-height: 1 !important;
    display: inline-flex !important;
    align-items: center !important;
    justify-content: center !important;
    transition: all 0.15s ease !important;
}
.stButton > button[kind="primary"],
[data-testid="baseButton-primary"],
[data-testid="baseButton-primaryFormSubmit"] {
    background: var(--ds-accent) !important;
    color: #FFFFFF !important;
    border: none !important;
}
.stButton > button[kind="primary"]:hover {
    filter: brightness(1.08) !important;
    color: #FFFFFF !important;
}

/* === 입력 필드 — 깔끔한 1px 테두리 한 줄 === */

/* 1) 안쪽 input/textarea 자체의 테두리·아웃라인 완전 제거 */
.stTextInput input,
.stNumberInput input,
.stTextArea textarea {
    height: var(--ds-control-h) !important;
    line-height: var(--ds-control-h) !important;
    border: 0 !important;
    border-radius: 0 !important;
    font-size: 14px !important;
    padding: 0 12px !important;
    box-shadow: none !important;
    outline: none !important;
    background: transparent !important;
    box-sizing: border-box !important;
    -webkit-appearance: none !important;
    color: var(--ds-text) !important;
}
.stTextArea textarea {
    height: auto !important;
    min-height: var(--ds-control-h) !important;
    line-height: 1.5 !important;
    padding: 10px 12px !important;
}

/* 2) baseweb 안쪽 래퍼 테두리 제거 (이중 테두리 방지) */
.stTextInput [data-baseweb="input"],
.stNumberInput [data-baseweb="input"],
.stTextArea [data-baseweb="textarea"] {
    border: 0 !important;
    background: transparent !important;
    box-shadow: none !important;
    flex: 1 !important;
}
.stTextInput [data-baseweb="input"] > div,
.stNumberInput [data-baseweb="input"] > div,
.stTextArea [data-baseweb="textarea"] > div {
    border: 0 !important;
    background: transparent !important;
}

/* 3) 텍스트/텍스트영역 — 가장 바깥 래퍼에 1px 테두리 */
.stTextInput > div > div:first-child,
.stTextArea > div > div:first-child {
    border: 1px solid var(--ds-border) !important;
    border-radius: 8px !important;
    background-color: var(--ds-surface) !important;
    overflow: hidden !important;
    transition: border-color 0.15s ease, box-shadow 0.15s ease !important;
}

/* 4) 숫자 입력 — input과 +/- 버튼을 한 테두리 안에 묶기 */
.stNumberInput > div:not([data-testid="stWidgetLabel"]) {
    border: 1px solid var(--ds-border) !important;
    border-radius: 8px !important;
    background-color: var(--ds-surface) !important;
    overflow: hidden !important;
    display: flex !important;
    align-items: stretch !important;
    transition: border-color 0.15s ease, box-shadow 0.15s ease !important;
}
.stNumberInput > div:not([data-testid="stWidgetLabel"]) > div {
    border: 0 !important;
    background: transparent !important;
}

/* 5) +/- 버튼 — 입력칸과 시각적으로 연결, 왼쪽에 1px 분리선 */
.stNumberInput button,
[data-testid="stNumberInput"] button {
    height: var(--ds-control-h) !important;
    min-height: var(--ds-control-h) !important;
    width: 36px !important;
    border: 0 !important;
    border-left: 1px solid var(--ds-border) !important;
    border-radius: 0 !important;
    background: transparent !important;
    color: var(--ds-text-muted) !important;
    box-shadow: none !important;
    padding: 0 !important;
    flex-shrink: 0 !important;
}
.stNumberInput button:hover {
    background: var(--ds-surface-2, rgba(0,0,0,0.04)) !important;
    color: var(--ds-text) !important;
}

/* 6) 포커스 상태 — 테두리 색만 강조색 (두께 그대로 1px) */
.stTextInput > div > div:first-child:focus-within,
.stTextArea > div > div:first-child:focus-within,
.stNumberInput > div:not([data-testid="stWidgetLabel"]):focus-within {
    border-color: var(--ds-accent) !important;
    box-shadow: 0 0 0 3px rgba(79, 70, 229, 0.12) !important;
}

/* 셀렉트 박스도 통일 */
[data-baseweb="select"] > div {
    min-height: var(--ds-control-h) !important;
    border: 1px solid var(--ds-border) !important;
    border-radius: 8px !important;
}

/* === 가로 컬럼 정렬: 같은 행의 입력칸·버튼이 같은 줄에 정렬되도록 === */
/* 한 컬럼엔 라벨+입력칸, 다른 컬럼엔 버튼만 있을 때
   각 컬럼의 내용을 "아래쪽 기준"으로 맞춰서 입력칸 바닥과 버튼 바닥이 일치 */
[data-testid="stHorizontalBlock"] {
    align-items: flex-end !important;
    gap: 12px !important;
}
[data-testid="stHorizontalBlock"] > [data-testid="column"] {
    display: flex !important;
    flex-direction: column !important;
    justify-content: flex-end !important;
}

/* === 위젯 라벨 통일 === */
[data-testid="stWidgetLabel"],
[data-testid="stWidgetLabel"] > label,
.stTextInput > label,
.stNumberInput > label,
.stSelectbox > label,
.stTextArea > label {
    font-size: 13px !important;
    color: var(--ds-text-muted) !important;
    margin-bottom: 6px !important;
    line-height: 1.4 !important;
    font-weight: 400 !important;
}

[data-testid="stMetric"] {
    border-radius: 10px;
    padding: 16px 20px;
    border: 0.5px solid var(--ds-border);
}
[data-testid="stMetricValue"] {
    font-weight: 500 !important;
    font-size: 24px !important;
}
[data-testid="stMetricLabel"] {
    color: var(--ds-text-muted) !important;
    font-size: 13px !important;
}


/* ========================================== */
/* ========== 다크 모드 (시스템 설정 따라 자동) ========== */
/* ========================================== */
@media (prefers-color-scheme: dark) {
    :root {
        --ds-bg: #0F0F11;
        --ds-surface: #1F1F23;
        --ds-surface-2: #2A2A2F;
        --ds-border: #2E2E33;
        --ds-text: #FAFAFA;
        --ds-text-muted: #A1A1AA;
        --ds-accent: #818CF8;
        --ds-success: #22C55E;
        --ds-warning: #FBBF24;
        --ds-up: #FB7185;
        --ds-down: #60A5FA;
    }

    /* 메인 배경 */
    .stApp,
    [data-testid="stAppViewContainer"],
    [data-testid="stMain"],
    .main {
        background-color: var(--ds-bg) !important;
        color: var(--ds-text) !important;
    }

    /* 상단 헤더 바 (흰 줄 제거) */
    [data-testid="stHeader"],
    header[data-testid="stHeader"] {
        background-color: var(--ds-bg) !important;
    }

    /* 사이드바 */
    [data-testid="stSidebar"],
    [data-testid="stSidebar"] > div,
    section[data-testid="stSidebar"] {
        background-color: var(--ds-surface) !important;
    }
    [data-testid="stSidebar"] * {
        color: var(--ds-text) !important;
    }

    /* 일반 텍스트 */
    body, p, div, span, label,
    .stMarkdown, .stMarkdown p,
    .stRadio label, .stCheckbox label,
    h1, h2, h3, h4, h5, h6 {
        color: var(--ds-text) !important;
    }

    /* === 입력 필드 (텍스트, 숫자, 텍스트영역) === */
    .stTextInput input,
    .stNumberInput input,
    .stTextArea textarea,
    [data-baseweb="input"] input,
    [data-baseweb="input"],
    [data-baseweb="textarea"] textarea,
    [data-baseweb="textarea"],
    [data-baseweb="base-input"] {
        background-color: var(--ds-surface) !important;
        color: var(--ds-text) !important;
        border-color: var(--ds-border) !important;
        -webkit-text-fill-color: var(--ds-text) !important;
    }
    .stTextInput input::placeholder,
    .stNumberInput input::placeholder,
    .stTextArea textarea::placeholder {
        color: var(--ds-text-muted) !important;
        -webkit-text-fill-color: var(--ds-text-muted) !important;
    }

    /* === 숫자 입력 +/- 버튼 === */
    .stNumberInput button,
    [data-testid="stNumberInput"] button {
        background-color: var(--ds-surface) !important;
        color: var(--ds-text) !important;
        border-color: var(--ds-border) !important;
    }

    /* === 셀렉트 박스 === */
    [data-baseweb="select"] > div,
    [data-baseweb="popover"] {
        background-color: var(--ds-surface) !important;
        color: var(--ds-text) !important;
        border-color: var(--ds-border) !important;
    }

    /* === 일반 버튼 (보조 버튼 포함) === */
    .stButton > button,
    [data-testid="baseButton-secondary"],
    [data-testid="baseButton-secondaryFormSubmit"] {
        background-color: var(--ds-surface) !important;
        color: var(--ds-text) !important;
        border: 0.5px solid var(--ds-border) !important;
    }
    .stButton > button:hover {
        background-color: var(--ds-surface-2) !important;
        border-color: var(--ds-accent) !important;
        color: var(--ds-accent) !important;
    }
    .stButton > button[kind="primary"],
    [data-testid="baseButton-primary"],
    [data-testid="baseButton-primaryFormSubmit"] {
        background-color: var(--ds-accent) !important;
        color: #FFFFFF !important;
        border: none !important;
    }

    /* === 폼 컨테이너 === */
    .stForm,
    [data-testid="stForm"] {
        background-color: var(--ds-surface) !important;
        border: 0.5px solid var(--ds-border) !important;
    }

    /* === Metric 카드 === */
    [data-testid="stMetric"] {
        background-color: var(--ds-surface) !important;
        border-color: var(--ds-border) !important;
    }

    /* === 데이터프레임/테이블 === */
    .stDataFrame, [data-testid="stDataFrame"] {
        background-color: var(--ds-surface) !important;
    }

    /* === Expander, Tabs, Alert === */
    .streamlit-expanderHeader,
    [data-testid="stExpander"] {
        background-color: var(--ds-surface) !important;
        color: var(--ds-text) !important;
        border-color: var(--ds-border) !important;
    }
}

/* ========================================== */
/* ========== 모바일 반응형 (≤640px) ========== */
/* ========================================== */
@media (max-width: 640px) {
    /* 본문 좌우 여백 최소화 → 좁은 화면 폭 최대 활용 */
    .block-container,
    [data-testid="stMainBlockContainer"],
    [data-testid="block-container"] {
        padding-left: 0.7rem !important;
        padding-right: 0.7rem !important;
        padding-top: 3rem !important;
        padding-bottom: 2rem !important;
    }

    /* 제목 약간 축소 */
    h1 { font-size: 18px !important; }
    h2 { font-size: 16px !important; }
    h3 { font-size: 15px !important; }

    /* 가로 컬럼이 좁으면 다음 줄로 흐르게 (내용 잘림 방지) */
    [data-testid="stHorizontalBlock"] {
        flex-wrap: wrap !important;
        gap: 8px !important;
        align-items: stretch !important;
    }
    /* 각 컬럼 최소 너비 → 한 줄에 2개씩 유연하게 배치 (폼·메트릭) */
    [data-testid="stHorizontalBlock"] > [data-testid="column"] {
        min-width: 140px !important;
        flex: 1 1 140px !important;
        justify-content: flex-start !important;
    }

    /* 메트릭 카드 컴팩트 */
    [data-testid="stMetric"] {
        padding: 10px 12px !important;
    }
    [data-testid="stMetricValue"] {
        font-size: 18px !important;
    }
    [data-testid="stMetricLabel"] {
        font-size: 12px !important;
    }

    /* 버튼 글자 약간 축소 */
    .stButton > button { font-size: 13px !important; }
}
</style>
"""


def inject_css() -> None:
    """앱 시작 부분에 한 번만 호출하면 디자인 시스템이 적용됩니다."""
    st.markdown(_CSS, unsafe_allow_html=True)


def color(name: str, dark: bool = False) -> str:
    """파이썬 코드 안에서 색을 가져올 때 사용. 예: color('up')."""
    palette = COLORS_DARK if dark else COLORS_LIGHT
    return palette.get(name, "#000000")
