# Streamlit Community Cloud 기본 진입점(streamlit_app.py) 호환용.
# 실제 앱은 app.py에 있고, 이 파일은 그걸 실행만 한다.
# (배포 '메인 파일 경로'가 streamlit_app.py로 설정돼 있어도 정상 구동되도록)
import app  # noqa: F401  — import 시점에 app.py의 모든 st.* 렌더가 실행됨
