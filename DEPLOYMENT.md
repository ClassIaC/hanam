# 무료 외부 호스팅 배포 가이드

## 1) Supabase(Postgres) 생성
1. Supabase 무료 프로젝트 생성
2. `Project Settings > Database`에서 연결 문자열 확보
3. 앱 환경변수로 `DATABASE_URL` 저장

## 2) Render(Web Service) 생성
1. GitHub에 프로젝트 업로드
2. Render에서 `New + > Web Service` 선택
3. 빌드/실행 설정
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app`
4. 환경변수 설정
   - `SECRET_KEY`: 임의의 긴 문자열
   - `DATABASE_URL`: Supabase Postgres URL (추후 DB 전환 시 사용)

## 3) 현재 코드 DB 관련 참고
- 현재 코드는 기본적으로 SQLite(`hanam.db`)를 사용합니다.
- 외부 호스팅에서는 파일 DB가 재시작 시 유실될 수 있으므로,
  운영 전에는 Postgres 연결 코드로 전환하는 것을 권장합니다.

## 4) 운영 체크리스트
- 관리자 비밀번호 즉시 변경
- 직원 승인 정책 운영
- 주 1회 데이터 백업
- 계정 삭제 요청 시 즉시 비활성 처리
