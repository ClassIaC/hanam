# 하남돼지 근무관리 MVP (웹)

하남돼지 매장용 근무관리 웹 서비스입니다.

## 반영된 핵심 정책
- 직원은 `이름 + 아이디 + 비밀번호`로 직접 가입 신청
- 관리자가 승인해야 로그인 가능
- 관리자는 직원 계정을 직접 생성할 수도 있음
- 근무시간은 `총 분` 대신 `총 시간`으로 표시 (30분 단위 반올림, 예: `2.5시간`)
- 관리자 페이지에서 직원 계정 삭제(비활성 처리) 가능

## 기능
- 관리자
  - 직원 가입 승인/반려
  - 직원 계정 직접 생성
  - 직원 계정 삭제 처리
  - 스케줄 등록
  - 근무기록 승인/반려
  - 직원별 총 승인 시간 확인
- 직원
  - 회원가입 신청
  - 내 스케줄 조회
  - 근무기록 등록
  - 이번 달 승인 근무시간 조회

## 실행 방법
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

브라우저: `http://127.0.0.1:5000`

초기 관리자 계정:
- ID: `admin`
- PW: `admin1234`

## 무료 외부 호스팅 권장
- 앱 서버: Render Free Web Service
- DB: Supabase Free Postgres

배포 절차는 `DEPLOYMENT.md`를 참고하세요.
# 하남돼지 근무관리 MVP (웹)

관리자/알바 계정을 분리해 스케줄과 근무기록을 관리하는 Flask 기반 반응형 웹입니다.

## 기능
- 관리자
  - 알바 계정 생성 (초기 비번 발급)
  - 근무 스케줄 등록
  - 알바 근무기록 승인
- 알바
  - 내 스케줄 조회
  - 근무기록 등록
  - 이번 달 승인 근무시간 확인

## 실행 방법
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

브라우저: `http://127.0.0.1:5000`

초기 관리자 계정:
- ID: `admin`
- PW: `admin1234`

첫 로그인 후 비밀번호 변경이 강제됩니다.

## 무료 배포 추천
- 웹: Render Web Service (Free) 또는 Railway
- DB: Supabase Postgres (무료)

현재 버전은 SQLite 로컬 DB(`hanam.db`)를 사용하며, 다음 단계에서 Postgres로 전환 가능합니다.
