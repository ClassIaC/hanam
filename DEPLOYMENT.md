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

## 3) DB (Supabase Postgres)
- Render에 `DATABASE_URL`을 넣으면 **Supabase(Postgres)** 로 연결됩니다. (Supabase 대시보드의 `postgresql://...` 연결 문자열)
- `DATABASE_URL`이 비어 있으면 로컬 개발용으로 **SQLite(`hanam.db`)** 를 사용합니다.
- 연결 문자열이 `postgres://` 로 시작하면 앱에서 자동으로 `postgresql://` 로 바꿉니다.

### 이미지(첨부파일) 운영 권장
- Render 무료 인스턴스는 **디스크가 비영구**일 수 있어, `static/uploads`에만 두면 재배포 시 사라질 수 있습니다.
- 실서비스에서는 아래 중 하나를 권장합니다.
  - **Supabase Storage** 버킷에 업로드하고 URL만 DB에 저장 (무료 티어와 잘 맞음)
  - **Cloudflare R2 / S3** 등 객체 스토리지 + 공개 URL
- 당장은 로컬·SQLite로 테스트하고, 배포 시 스토리지 연동을 추가하는 방식이 안전합니다.

## 4) 운영 체크리스트
- 관리자 비밀번호 즉시 변경
- 직원 승인 정책 운영
- 주 1회 데이터 백업
- 계정 삭제 요청 시 즉시 비활성 처리
