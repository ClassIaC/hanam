from datetime import datetime
import calendar
import sqlite3
from functools import wraps
import csv
import io
import os
import smtplib
import secrets
from email.message import EmailMessage

from flask import Flask, g, redirect, render_template, request, session, url_for, flash, Response
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from db_schema import PG_INIT_STATEMENTS, SQLITE_CREATE_SCRIPT


app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-this-in-production")
app.config["DATABASE"] = os.getenv("SQLITE_PATH", "hanam.db")
app.config["SMTP_HOST"] = os.getenv("SMTP_HOST", "")
app.config["SMTP_PORT"] = int(os.getenv("SMTP_PORT", "587"))
app.config["SMTP_USER"] = os.getenv("SMTP_USER", "")
app.config["SMTP_PASSWORD"] = os.getenv("SMTP_PASSWORD", "")
app.config["SMTP_FROM"] = os.getenv("SMTP_FROM", "")
app.config["UPLOAD_DIR"] = os.path.join("static", "uploads")
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

SUPABASE_URL = (os.getenv("SUPABASE_URL", "") or "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "")
USE_SUPABASE_STORAGE = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY and SUPABASE_BUCKET)

_CONTENT_TYPE_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
WEEKDAY_OPTIONS = [
    ("0", "월요일"),
    ("1", "화요일"),
    ("2", "수요일"),
    ("3", "목요일"),
    ("4", "금요일"),
    ("5", "토요일"),
    ("6", "일요일"),
]
LOGIN_ATTEMPTS = {}
MAX_LOGIN_ATTEMPTS = 5


def _database_url():
    return (os.getenv("DATABASE_URL") or "").strip()


USE_POSTGRES = bool(_database_url())

if USE_POSTGRES:
    import psycopg2
    from psycopg2 import pool as _pg_pool_mod

    DBIntegrityError = psycopg2.IntegrityError
    _PG_POOL = None

    def _get_pg_pool():
        global _PG_POOL
        if _PG_POOL is None:
            _PG_POOL = _pg_pool_mod.SimpleConnectionPool(
                1,
                int(os.getenv("PG_POOL_MAX", "5")),
                dsn=_normalize_postgres_url(_database_url()),
                connect_timeout=10,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=3,
            )
        return _PG_POOL
else:
    DBIntegrityError = sqlite3.IntegrityError


def _normalize_postgres_url(url):
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql://", 1)
    return url


class _SqliteCursorAdapter:
    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class SqliteConnAdapter:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        if params is None:
            params = ()
        return _SqliteCursorAdapter(self._conn.execute(sql, params))

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def executescript(self, script):
        self._conn.executescript(script)


class _PgCursorAdapter:
    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        try:
            return self._cursor.fetchone()
        finally:
            self._close()

    def fetchall(self):
        try:
            return self._cursor.fetchall()
        finally:
            self._close()

    def _close(self):
        if self._cursor is not None:
            try:
                self._cursor.close()
            except Exception:
                pass
            self._cursor = None

    def __del__(self):
        self._close()


class _PgNoopCursor:
    """INSERT/UPDATE 등 결과 집합이 없는 실행용 (커서 즉시 닫힘)."""

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class PostgresConnAdapter:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        if params is None:
            params = ()
        from psycopg2.extras import RealDictCursor

        adapted = sql.replace("?", "%s")
        cur = self._conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(adapted, params)
        head = adapted.lstrip().split(None, 1)[0].upper() if adapted.strip() else ""
        if head in ("SELECT", "WITH") or "RETURNING" in adapted.upper():
            return _PgCursorAdapter(cur)
        cur.close()
        return _PgNoopCursor()

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def executescript(self, script):
        raise RuntimeError("executescript is SQLite-only")


def get_db():
    if "db" not in g:
        if USE_POSTGRES:
            raw = _get_pg_pool().getconn()
            adapter = PostgresConnAdapter(raw)
            adapter._pooled = True
            g.db = adapter
        else:
            raw = sqlite3.connect(app.config["DATABASE"])
            raw.row_factory = sqlite3.Row
            g.db = SqliteConnAdapter(raw)
    return g.db


@app.teardown_appcontext
def close_db(_exception):
    db = g.pop("db", None)
    if db is None:
        return
    if USE_POSTGRES and getattr(db, "_pooled", False):
        raw = db._conn
        try:
            if _exception is not None:
                raw.rollback()
        except Exception:
            pass
        try:
            _get_pg_pool().putconn(raw)
        except Exception:
            try:
                raw.close()
            except Exception:
                pass
    else:
        db.close()


def ensure_column(db, table_name, column_name, alter_sql):
    if USE_POSTGRES:
        exists = db.execute(
            """
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = ?
              AND column_name = ?
            LIMIT 1
            """,
            (table_name, column_name),
        ).fetchone()
        if not exists:
            db.execute(alter_sql)
    else:
        cols = db.execute(f"PRAGMA table_info({table_name})").fetchall()
        if column_name not in [c["name"] for c in cols]:
            db.execute(alter_sql)


def minutes_to_pay_hours(total_minutes):
    rounded_half_hours = round(total_minutes / 30)
    pay_hours = rounded_half_hours * 0.5
    if pay_hours.is_integer():
        return str(int(pay_hours))
    return f"{pay_hours:.1f}"


def weekday_label(weekday_value):
    mapping = {int(value): label for value, label in WEEKDAY_OPTIONS}
    if weekday_value is None:
        return ""
    return mapping.get(int(weekday_value), "")


def format_request_detail(request_row):
    req_type = request_row["request_type"]
    note = (request_row["note"] or "").strip()
    if req_type == "preferred_weekday":
        detail = f"희망요일: {weekday_label(request_row['weekday'])}"
    else:
        detail = f"휴무희망일: {request_row['request_date'] or '-'}"
    if note:
        detail = f"{detail} ({note})"
    return detail


def is_login_blocked(client_key):
    record = LOGIN_ATTEMPTS.get(client_key)
    if not record:
        return False
    fail_count, last_failed_at = record
    if fail_count < MAX_LOGIN_ATTEMPTS:
        return False
    return (datetime.now() - last_failed_at).total_seconds() < 300


def register_login_fail(client_key):
    fail_count, _last_failed_at = LOGIN_ATTEMPTS.get(client_key, (0, None))
    LOGIN_ATTEMPTS[client_key] = (fail_count + 1, datetime.now())


def reset_login_fail(client_key):
    LOGIN_ATTEMPTS.pop(client_key, None)


def write_audit_log(action, details):
    if "user_id" not in session:
        return
    db = get_db()
    db.execute(
        """
        INSERT INTO audit_logs (actor_id, action, details, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (session["user_id"], action, details, datetime.now().isoformat()),
    )
    db.commit()


def send_email_notification(to_email, subject, body):
    if not to_email:
        return False
    host = app.config["SMTP_HOST"]
    port = app.config["SMTP_PORT"]
    user = app.config["SMTP_USER"]
    password = app.config["SMTP_PASSWORD"]
    sender = app.config["SMTP_FROM"] or user
    if not (host and user and password and sender):
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to_email
    msg.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=10) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        return True
    except Exception:
        return False


def notify_staff_by_id(staff_id, subject, body):
    db = get_db()
    staff = db.execute(
        "SELECT full_name, email FROM users WHERE id = ? AND role = 'staff' AND is_deleted = 0",
        (staff_id,),
    ).fetchone()
    if not staff or not staff["email"]:
        return False
    sent = send_email_notification(staff["email"], subject, body)
    if sent:
        write_audit_log("email.sent", f"to={staff['email']}, subject={subject}")
    return sent


def _supabase_public_prefix():
    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/"


def _supabase_upload(object_name, file_storage, ext):
    import requests

    content_type = _CONTENT_TYPE_BY_EXT.get(ext, "application/octet-stream")
    try:
        file_storage.stream.seek(0)
    except Exception:
        pass
    data = file_storage.stream.read()
    upload_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{object_name}"
    resp = requests.post(
        upload_url,
        headers={
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
            "Content-Type": content_type,
            "x-upsert": "true",
        },
        data=data,
        timeout=30,
    )
    resp.raise_for_status()
    return f"{_supabase_public_prefix()}{object_name}"


def _supabase_delete(public_url):
    import requests

    prefix = _supabase_public_prefix()
    if not public_url.startswith(prefix):
        return
    object_name = public_url[len(prefix):]
    delete_url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{object_name}"
    try:
        requests.delete(
            delete_url,
            headers={"Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"},
            timeout=15,
        )
    except Exception as exc:
        app.logger.error("Supabase delete failed: %s", exc)


def save_uploaded_image(file_storage):
    if not file_storage or not file_storage.filename:
        return ""
    filename = secure_filename(file_storage.filename)
    if "." not in filename:
        return None
    ext = filename.rsplit(".", 1)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return None
    unique_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(4)}.{ext}"

    if USE_SUPABASE_STORAGE:
        try:
            return _supabase_upload(unique_name, file_storage, ext)
        except Exception as exc:
            app.logger.error("Supabase upload failed, falling back to local: %s", exc)

    saved_path = os.path.join(app.config["UPLOAD_DIR"], unique_name)
    try:
        file_storage.stream.seek(0)
    except Exception:
        pass
    file_storage.save(saved_path)
    return f"uploads/{unique_name}"


def delete_uploaded_image(path_or_url):
    if not path_or_url:
        return
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        if USE_SUPABASE_STORAGE:
            _supabase_delete(path_or_url)
        return
    if not path_or_url.startswith("uploads/"):
        return
    abs_path = os.path.join("static", *path_or_url.split("/"))
    if os.path.exists(abs_path):
        try:
            os.remove(abs_path)
        except OSError:
            pass


@app.template_filter("image_url")
def image_url_filter(value):
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return url_for("static", filename=value)


def build_month_calendar(year, month, shifts_by_date, selected_date):
    first_weekday, last_day = calendar.monthrange(year, month)
    weeks = []
    week = [None] * first_weekday
    for day in range(1, last_day + 1):
        day_str = f"{year:04d}-{month:02d}-{day:02d}"
        week.append(
            {
                "date": day_str,
                "day": day,
                "is_today": day_str == datetime.now().strftime("%Y-%m-%d"),
                "is_selected": day_str == selected_date,
                "shifts": shifts_by_date.get(day_str, []),
            }
        )
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        week.extend([None] * (7 - len(week)))
        weeks.append(week)
    return weeks


def build_comment_author_aliases(comments_rows):
    """게시글 내 댓글 작성자별로 익명1, 익명2 … 번호를 안정적으로 부여 (첫 댓글 시각 순)."""
    ordered = sorted(comments_rows, key=lambda r: r["created_at"])
    aliases = {}
    n = 0
    for row in ordered:
        aid = row["author_id"]
        if aid not in aliases:
            n += 1
            aliases[aid] = n
    return aliases


def init_db():
    db = get_db()
    if USE_POSTGRES:
        for stmt in PG_INIT_STATEMENTS:
            db.execute(stmt)
    else:
        db.executescript(SQLITE_CREATE_SCRIPT)

    ensure_column(db, "users", "full_name", "ALTER TABLE users ADD COLUMN full_name TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "users", "email", "ALTER TABLE users ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    ensure_column(
        db,
        "users",
        "approval_status",
        "ALTER TABLE users ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'approved' CHECK (approval_status IN ('pending', 'approved', 'rejected'))",
    )
    ensure_column(db, "users", "approved_by", "ALTER TABLE users ADD COLUMN approved_by INTEGER")
    ensure_column(db, "users", "approved_at", "ALTER TABLE users ADD COLUMN approved_at TEXT")
    ensure_column(db, "users", "is_deleted", "ALTER TABLE users ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")
    ensure_column(db, "board_posts", "image_path", "ALTER TABLE board_posts ADD COLUMN image_path TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "board_comments", "image_path", "ALTER TABLE board_comments ADD COLUMN image_path TEXT NOT NULL DEFAULT ''")
    ensure_column(db, "board_comments", "parent_comment_id", "ALTER TABLE board_comments ADD COLUMN parent_comment_id INTEGER")
    ensure_column(db, "manuals", "image_path", "ALTER TABLE manuals ADD COLUMN image_path TEXT NOT NULL DEFAULT ''")
    db.execute(
        """
        UPDATE users
        SET is_deleted = 1, is_active = 0
        WHERE role = 'staff' AND (full_name LIKE ? OR username LIKE ?)
        """,
        ("%(삭제됨)%", "%_deleted_%"),
    )

    admin = db.execute("SELECT id FROM users WHERE username = ?", ("admin",)).fetchone()
    if admin is None:
        db.execute(
            """
            INSERT INTO users (
                username, full_name, password_hash, role, must_change_password,
                is_active, approval_status, is_deleted, created_at
            )
            VALUES (?, ?, ?, 'admin', 1, 1, 'approved', 0, ?)
            """,
            ("admin", "관리자", generate_password_hash("admin1234"), datetime.now().isoformat()),
        )
    else:
        db.execute(
            """
            UPDATE users
            SET full_name = CASE WHEN full_name = '' THEN '관리자' ELSE full_name END,
                approval_status = 'approved',
                is_active = 1
            WHERE username = 'admin'
            """
        )
    db.commit()
    seed_default_manuals()


DEFAULT_MANUALS = [
    (
        10,
        "오픈 준비 (10:30 ~ 12:00)",
        "1. 매장 전체 조명/에어컨/환풍기 ON\n"
        "2. 테이블, 의자, 바닥 닦기 및 소독\n"
        "3. 수저통/앞접시/집게/가위 셋팅 및 수량 확인\n"
        "4. 반찬 냉장고 상태 체크, 부족분 리필\n"
        "5. 숯불/그릴/가스 점검 및 환기 확인\n"
        "6. POS 시스템/결제기 로그인, 프린터 용지 확인\n"
        "7. 화장실 휴지/비누/청결 점검\n"
        "8. 입간판/메뉴판 세팅, 외부 청소",
    ),
    (
        20,
        "홀 서비스 기본",
        "• 손님 입장 시 '어서오세요, 하남돼지집입니다' 라고 밝게 인사\n"
        "• 인원 확인 후 테이블 안내, 물과 물수건 즉시 제공\n"
        "• 메뉴 주문은 POS에 바로 입력하고 주방에 전달\n"
        "• 고기는 1인분 단위가 아닌 '주문 수량 기준'으로 서빙\n"
        "• 불판 교체 타이밍: 숯이 사그라들거나 기름이 많이 튈 때 선제 교체\n"
        "• 반찬은 비면 바로 리필 (손님이 요청하기 전에)\n"
        "• 계산 시 '맛있게 드셨어요?' 한 마디 + 재방문 인사",
    ),
    (
        30,
        "주방/고기 취급 기본",
        "1. 냉장/냉동 온도 기준: 냉장 0~4℃, 냉동 -18℃ 이하 유지\n"
        "2. 입고 시 포장 상태, 유통기한 확인 후 선입선출(FIFO) 원칙으로 배치\n"
        "3. 고기 해동은 냉장해동만 사용 (상온 해동 금지)\n"
        "4. 도마·칼은 용도별 분리(육류/채소) 사용, 교차오염 방지\n"
        "5. 숙성고 내부 온도 매일 오전/오후 2회 기록\n"
        "6. 조리 중 앞치마·위생모·장갑 착용 필수\n"
        "7. 손 세척: 조리 전, 화장실 이용 후, 다른 식재료 만질 때 매번",
    ),
    (
        40,
        "마감 절차 (23:00 이후)",
        "1. 남은 손님 계산 완료 확인\n"
        "2. 전 테이블·의자 닦기, 바닥 물청소\n"
        "3. 반찬은 소분 밀폐용기로 옮겨 냉장 보관 (재사용 가능 여부 체크)\n"
        "4. 불판/그릴/후드 분해 세척\n"
        "5. 쓰레기/음식물/재활용 분리 배출\n"
        "6. 냉장고/숙성고 온도 최종 확인 및 기록\n"
        "7. 시재 정산, 당일 매출 POS에서 마감 처리\n"
        "8. 가스 밸브 / 전기(일부 제외) / 조명 OFF, 문단속 후 CCTV 확인",
    ),
    (
        50,
        "위생 및 안전 수칙",
        "• 근무자 개인 위생: 매일 위생복/앞치마 교체, 손톱 짧게, 반지·시계 제거\n"
        "• 발열/기침/장염 증상 시 즉시 관리자에게 알리고 출근 금지\n"
        "• 식중독 의심 징후(설사, 구토 등) 발생 시 해당 직원 격리 후 보고\n"
        "• 화재 시 1차: 가스 밸브 차단 → 2차: 소화기 사용 → 3차: 119\n"
        "• 소화기 위치: (예) 카운터 옆, 주방 출입구. 매월 위치/압력 게이지 확인\n"
        "• 응급처치 키트 위치: 카운터 하단 서랍",
    ),
    (
        60,
        "고객 응대 / 클레임 대응",
        "• 불만 접수 시 절대 먼저 반박하지 않고 '불편을 드려 죄송합니다' 부터\n"
        "• 원인 확인 후 가능한 범위에서 즉시 조치 (재조리, 교환, 서비스 제공)\n"
        "• 처리 권한을 넘어가는 사항(환불, 전액 보상 등)은 반드시 매니저 호출\n"
        "• 취객/난동 발생 시 다른 손님 보호 우선, 필요 시 112\n"
        "• 모든 클레임은 당일 감사 로그/인수인계 노트에 기록",
    ),
    (
        70,
        "POS / 결제 시스템 기본",
        "1. 주문 입력 → 테이블 지정 → 주방 프린터 출력 확인\n"
        "2. 메뉴 변경/취소는 반드시 관리자 권한 카드로 승인\n"
        "3. 결제 수단: 카드/현금/간편결제. 현금 결제 시 영수증 필수 발급\n"
        "4. 결제 오류/취소는 결제 승인번호 남기고 재시도\n"
        "5. 일 마감 시 POS 정산 리포트 출력 후 시재와 대조",
    ),
    (
        80,
        "긴급 상황 대응",
        "[화재] 가스 차단 → 손님 대피 유도 → 소화기 → 119\n"
        "[정전] 카운터 비상전등 ON, 조리 중단, 현금 결제 안내\n"
        "[부상] 가벼운 상처: 응급키트 사용. 심한 상처/화상: 119 + 관리자 연락\n"
        "[식중독 의심 민원] 해당 음식/영수증 보관, 본사 보고 후 고객 연락처 확보\n"
        "[무단 침입·도난] 본인 안전 우선, 즉시 112 신고, CCTV 확인",
    ),
]


def seed_default_manuals():
    db = get_db()
    try:
        existing = db.execute("SELECT COUNT(*) AS c FROM manuals").fetchone()
    except Exception:
        return
    if not existing:
        return
    try:
        count = existing["c"]
    except Exception:
        try:
            count = existing[0]
        except Exception:
            count = 0
    if count and count > 0:
        return
    admin = db.execute(
        "SELECT id FROM users WHERE role = 'admin' AND is_deleted = 0 ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if not admin:
        return
    now_iso = datetime.now().isoformat()
    for sort_order, title, content in DEFAULT_MANUALS:
        db.execute(
            """
            INSERT INTO manuals (author_id, title, content, image_path, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, '', ?, ?, ?)
            """,
            (admin["id"], title, content, sort_order, now_iso, now_iso),
        )
    db.commit()


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return wrapper


def role_required(required_role):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if session.get("role") != required_role:
                flash("권한이 없습니다.")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)

        return wrapper

    return decorator


@app.context_processor
def inject_today_date():
    return {
        "today_date": datetime.now().strftime("%Y-%m-%d"),
        "default_start_time": "13:00",
        "default_end_time": "22:00",
        "weekday_options": WEEKDAY_OPTIONS,
    }


@app.before_request
def _start_timer():
    g._req_started_at = datetime.now()


@app.after_request
def _log_request_time(response):
    try:
        started = getattr(g, "_req_started_at", None)
        if started is not None:
            ms = int((datetime.now() - started).total_seconds() * 1000)
            app.logger.info(
                "[timing] %s %s %s %dms",
                request.method,
                request.path,
                response.status_code,
                ms,
            )
    except Exception:
        pass
    return response


@app.route("/")
def index():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        full_name = request.form["full_name"].strip()
        email = request.form.get("email", "").strip()
        password = request.form["password"]
        if len(username) < 3 or len(full_name) < 2 or len(password) < 8:
            flash("아이디 3자 이상, 이름 2자 이상, 비밀번호 8자 이상으로 입력해 주세요.")
            return redirect(url_for("register"))
        if email and "@" not in email:
            flash("이메일 형식이 올바르지 않습니다.")
            return redirect(url_for("register"))

        db = get_db()
        try:
            db.execute(
                """
                INSERT INTO users (
                    username, full_name, email, password_hash, role, must_change_password,
                    is_active, approval_status, is_deleted, created_at
                )
                VALUES (?, ?, ?, ?, 'staff', 0, 0, 'pending', 0, ?)
                """,
                (username, full_name, email, generate_password_hash(password), datetime.now().isoformat()),
            )
            db.commit()
            flash("회원가입이 완료되었습니다. 관리자 승인 후 로그인할 수 있습니다.")
            return redirect(url_for("login"))
        except DBIntegrityError:
            flash("이미 존재하는 아이디입니다.")
            return redirect(url_for("register"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]
        client_key = f"{request.remote_addr}:{username}"
        if is_login_blocked(client_key):
            flash("로그인 시도 횟수가 많습니다. 5분 후 다시 시도해 주세요.")
            return render_template("login.html")
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ? AND is_deleted = 0",
            (username,),
        ).fetchone()

        if not user or not check_password_hash(user["password_hash"], password):
            register_login_fail(client_key)
            flash("아이디 또는 비밀번호가 올바르지 않습니다.")
            return render_template("login.html")
        if user["is_active"] != 1:
            if user["approval_status"] == "pending":
                flash("관리자 승인 대기 중입니다.")
            else:
                flash("비활성 계정입니다. 관리자에게 문의하세요.")
            return render_template("login.html")

        reset_login_fail(client_key)
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["full_name"] = user["full_name"]
        session["role"] = user["role"]
        session["must_change_password"] = user["must_change_password"]
        return redirect(url_for("dashboard"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        new_password = request.form["new_password"]
        if len(new_password) < 8:
            flash("비밀번호는 8자 이상이어야 합니다.")
            return redirect(url_for("change_password"))

        db = get_db()
        db.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (generate_password_hash(new_password), session["user_id"]),
        )
        db.commit()
        session["must_change_password"] = 0
        flash("비밀번호가 변경되었습니다.")
        return redirect(url_for("dashboard"))

    return render_template("change_password.html")


@app.route("/profile")
@login_required
def profile():
    db = get_db()
    user = db.execute(
        """
        SELECT id, username, full_name, email, role
        FROM users
        WHERE id = ?
        """,
        (session["user_id"],),
    ).fetchone()
    return render_template("profile.html", user=user)


@app.route("/profile/email", methods=["POST"])
@login_required
def update_profile_email():
    email = request.form.get("email", "").strip()
    if email and "@" not in email:
        flash("이메일 형식이 올바르지 않습니다.")
        return redirect(url_for("profile"))

    db = get_db()
    db.execute(
        "UPDATE users SET email = ? WHERE id = ?",
        (email, session["user_id"]),
    )
    db.commit()
    write_audit_log("profile.email.update", f"user_id={session['user_id']}, email={email or '-'}")
    flash("이메일이 변경되었습니다.")
    return redirect(url_for("profile"))


@app.route("/profile/password", methods=["POST"])
@login_required
def update_profile_password():
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    if len(new_password) < 8:
        flash("새 비밀번호는 8자 이상이어야 합니다.")
        return redirect(url_for("profile"))

    db = get_db()
    user = db.execute(
        "SELECT password_hash FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()
    if not user or not check_password_hash(user["password_hash"], current_password):
        flash("현재 비밀번호가 일치하지 않습니다.")
        return redirect(url_for("profile"))

    db.execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
        (generate_password_hash(new_password), session["user_id"]),
    )
    db.commit()
    session["must_change_password"] = 0
    write_audit_log("profile.password.update", f"user_id={session['user_id']} 비밀번호 변경")
    flash("비밀번호가 변경되었습니다.")
    return redirect(url_for("profile"))


@app.route("/dashboard")
@login_required
def dashboard():
    if session.get("must_change_password") == 1:
        return redirect(url_for("change_password"))

    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("staff_dashboard"))


@app.route("/admin")
@login_required
@role_required("admin")
def admin_dashboard():
    db = get_db()
    staff_list = db.execute(
        """
        SELECT id, username, full_name, email, role, is_active, approval_status
        FROM users
        WHERE role = 'staff' AND is_deleted = 0
        ORDER BY full_name, username
        """
    ).fetchall()
    pending_staff = [s for s in staff_list if s["approval_status"] == "pending"]

    shifts = db.execute(
        """
        SELECT s.id, s.work_date, s.start_time, s.end_time, s.note, u.full_name AS staff_name
        FROM shifts s
        JOIN users u ON u.id = s.staff_id
        WHERE u.is_deleted = 0
        ORDER BY s.work_date DESC, s.start_time DESC
        LIMIT 20
        """
    ).fetchall()

    pending_logs = db.execute(
        """
        SELECT wl.id, wl.work_date, wl.clock_in, wl.clock_out, wl.total_minutes, u.full_name AS staff_name
        FROM work_logs wl
        JOIN users u ON u.id = wl.staff_id
        WHERE wl.status = 'pending'
        ORDER BY wl.work_date DESC
        """
    ).fetchall()

    monthly = db.execute(
        """
        SELECT
            u.full_name AS staff_name,
            COALESCE(SUM(CASE WHEN wl.status = 'approved' THEN wl.total_minutes ELSE 0 END), 0) AS approved_minutes
        FROM users u
        LEFT JOIN work_logs wl ON wl.staff_id = u.id
        WHERE u.role = 'staff' AND u.is_active = 1
        GROUP BY u.id, u.full_name
        ORDER BY u.full_name
        """
    ).fetchall()
    monthly_hours = [
        {"staff_name": row["staff_name"], "pay_hours": minutes_to_pay_hours(row["approved_minutes"])}
        for row in monthly
    ]
    approved_staff_count = len([s for s in staff_list if s["is_active"] and s["approval_status"] == "approved"])
    recent_requests = db.execute(
        """
        SELECT ar.id, u.full_name, ar.request_type, ar.weekday, ar.request_date, ar.note, ar.status
        FROM availability_requests ar
        JOIN users u ON u.id = ar.staff_id
        WHERE u.is_deleted = 0
        ORDER BY ar.created_at DESC
        LIMIT 20
        """
    ).fetchall()
    pending_requests = [r for r in recent_requests if r["status"] == "pending"]

    return render_template(
        "admin_dashboard.html",
        staff_list=staff_list,
        pending_staff=pending_staff,
        shifts=shifts,
        pending_logs=pending_logs,
        monthly_hours=monthly_hours,
        recent_requests=recent_requests,
        pending_requests=pending_requests,
        approved_staff_count=approved_staff_count,
        minutes_to_pay_hours=minutes_to_pay_hours,
        format_request_detail=format_request_detail,
    )


@app.route("/admin/calendar")
@login_required
@role_required("admin")
def admin_calendar():
    selected_date = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    month_param = request.args.get("month", selected_date[:7])
    try:
        view_year = int(month_param.split("-")[0])
        view_month = int(month_param.split("-")[1])
        if not (1 <= view_month <= 12):
            raise ValueError
    except (ValueError, IndexError):
        view_year = datetime.now().year
        view_month = datetime.now().month

    month_start = f"{view_year:04d}-{view_month:02d}-01"
    month_end_day = calendar.monthrange(view_year, view_month)[1]
    month_end = f"{view_year:04d}-{view_month:02d}-{month_end_day:02d}"
    db = get_db()
    month_shifts = db.execute(
        """
        SELECT s.work_date, s.start_time, s.end_time, u.full_name AS staff_name
        FROM shifts s
        JOIN users u ON u.id = s.staff_id
        WHERE s.work_date BETWEEN ? AND ? AND u.is_deleted = 0
        ORDER BY s.work_date ASC, s.start_time ASC
        """,
        (month_start, month_end),
    ).fetchall()
    shifts_by_date = {}
    for row in month_shifts:
        shifts_by_date.setdefault(row["work_date"], []).append(row)

    calendar_weeks = build_month_calendar(view_year, view_month, shifts_by_date, selected_date)
    day_shifts = db.execute(
        """
        SELECT s.start_time, s.end_time, s.note, u.full_name AS staff_name
        FROM shifts s
        JOIN users u ON u.id = s.staff_id
        WHERE s.work_date = ? AND u.is_deleted = 0
        ORDER BY s.start_time ASC
        """,
        (selected_date,),
    ).fetchall()
    return render_template(
        "admin_calendar.html",
        selected_date=selected_date,
        day_shifts=day_shifts,
        calendar_weeks=calendar_weeks,
        view_month=f"{view_year:04d}-{view_month:02d}",
    )


@app.route("/admin/staff", methods=["POST"])
@login_required
@role_required("admin")
def create_staff():
    username = request.form["username"].strip()
    full_name = request.form["full_name"].strip()
    email = request.form.get("email", "").strip()
    temp_password = request.form["temp_password"]

    if len(username) < 3 or len(full_name) < 2 or len(temp_password) < 8:
        flash("아이디 3자 이상, 이름 2자 이상, 초기 비밀번호 8자 이상이어야 합니다.")
        return redirect(url_for("admin_dashboard"))
    if email and "@" not in email:
        flash("이메일 형식이 올바르지 않습니다.")
        return redirect(url_for("admin_dashboard"))

    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO users (
                username, full_name, email, password_hash, role, must_change_password,
                is_active, approval_status, approved_by, approved_at, is_deleted, created_at
            )
            VALUES (?, ?, ?, ?, 'staff', 1, 1, 'approved', ?, ?, 0, ?)
            """,
            (
                username,
                full_name,
                email,
                generate_password_hash(temp_password),
                session["user_id"],
                datetime.now().isoformat(),
                datetime.now().isoformat(),
            ),
        )
        db.commit()
        write_audit_log("staff.create", f"{full_name}({username}) 계정 생성")
        if email:
            send_email_notification(
                email,
                "[하남 근무관리] 계정 생성 안내",
                f"{full_name}님 계정이 생성되었습니다.\n아이디: {username}\n임시 비밀번호: {temp_password}\n로그인 후 비밀번호를 변경해 주세요.",
            )
        flash("알바 계정이 생성되었습니다.")
    except DBIntegrityError:
        flash("이미 존재하는 아이디입니다.")

    return redirect(url_for("admin_dashboard"))


@app.route("/admin/staff/<int:staff_id>/approve", methods=["POST"])
@login_required
@role_required("admin")
def approve_staff(staff_id):
    db = get_db()
    db.execute(
        """
        UPDATE users
        SET is_active = 1, approval_status = 'approved', approved_by = ?, approved_at = ?
        WHERE id = ? AND role = 'staff'
        """,
        (session["user_id"], datetime.now().isoformat(), staff_id),
    )
    db.commit()
    write_audit_log("staff.approve", f"staff_id={staff_id} 승인")
    notify_staff_by_id(
        staff_id,
        "[하남 근무관리] 가입 승인 완료",
        "가입이 승인되었습니다. 로그인 후 근무일정과 근무기록을 확인해 주세요.",
    )
    flash("직원 계정이 승인되었습니다.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/staff/<int:staff_id>/reject", methods=["POST"])
@login_required
@role_required("admin")
def reject_staff(staff_id):
    db = get_db()
    db.execute(
        """
        UPDATE users
        SET is_active = 0, approval_status = 'rejected'
        WHERE id = ? AND role = 'staff'
        """,
        (staff_id,),
    )
    db.commit()
    write_audit_log("staff.reject", f"staff_id={staff_id} 반려")
    notify_staff_by_id(
        staff_id,
        "[하남 근무관리] 가입 요청 반려",
        "가입 요청이 반려되었습니다. 관리자에게 문의해 주세요.",
    )
    flash("직원 계정 가입 요청을 반려했습니다.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/staff/<int:staff_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_staff(staff_id):
    db = get_db()
    existing = db.execute(
        "SELECT id, is_deleted FROM users WHERE id = ? AND role = 'staff'",
        (staff_id,),
    ).fetchone()
    if not existing:
        flash("존재하지 않는 직원 계정입니다.")
        return redirect(url_for("admin_dashboard"))
    if existing["is_deleted"] == 1:
        flash("이미 삭제된 직원 계정입니다.")
        return redirect(url_for("admin_dashboard"))

    # 참조 무결성을 위해 소프트 삭제 후 목록/로그인에서 완전 숨김
    db.execute(
        """
        UPDATE users
        SET is_active = 0, approval_status = 'rejected', is_deleted = 1
        WHERE id = ? AND role = 'staff'
        """,
        (staff_id,),
    )
    db.commit()
    write_audit_log("staff.delete", f"staff_id={staff_id} 삭제처리")
    flash("직원 계정이 삭제 처리되었습니다.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/staff/<int:staff_id>/reset-password", methods=["POST"])
@login_required
@role_required("admin")
def reset_staff_password(staff_id):
    temp_password = request.form.get("temp_password", "").strip()
    if len(temp_password) < 8:
        flash("임시 비밀번호는 8자 이상이어야 합니다.")
        return redirect(url_for("admin_dashboard"))

    db = get_db()
    staff = db.execute(
        """
        SELECT id, full_name
        FROM users
        WHERE id = ? AND role = 'staff' AND is_deleted = 0
        """,
        (staff_id,),
    ).fetchone()
    if not staff:
        flash("비밀번호를 초기화할 직원 계정을 찾을 수 없습니다.")
        return redirect(url_for("admin_dashboard"))

    db.execute(
        """
        UPDATE users
        SET password_hash = ?, must_change_password = 1
        WHERE id = ?
        """,
        (generate_password_hash(temp_password), staff_id),
    )
    db.commit()
    write_audit_log("staff.password_reset", f"staff_id={staff_id} 비밀번호 초기화")
    notify_staff_by_id(
        staff_id,
        "[하남 근무관리] 비밀번호 초기화 안내",
        f"비밀번호가 초기화되었습니다.\n임시 비밀번호: {temp_password}\n다음 로그인 시 비밀번호 변경이 필요합니다.",
    )
    flash(f"{staff['full_name']} 계정 비밀번호가 초기화되었습니다. 다음 로그인 시 변경됩니다.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/staff/<int:staff_id>/grant-admin", methods=["POST"])
@login_required
@role_required("admin")
def grant_admin_role(staff_id):
    db = get_db()
    staff = db.execute(
        """
        SELECT id, full_name, role, is_deleted
        FROM users
        WHERE id = ?
        """,
        (staff_id,),
    ).fetchone()
    if not staff or staff["is_deleted"] == 1:
        flash("권한을 부여할 계정을 찾을 수 없습니다.")
        return redirect(url_for("admin_dashboard"))
    if staff["role"] == "admin":
        flash("이미 관리자 권한을 가진 계정입니다.")
        return redirect(url_for("admin_dashboard"))

    db.execute(
        """
        UPDATE users
        SET role = 'admin', approval_status = 'approved', is_active = 1
        WHERE id = ?
        """,
        (staff_id,),
    )
    db.commit()
    write_audit_log("staff.grant_admin", f"user_id={staff_id} 관리자 권한 부여")
    flash(f"{staff['full_name']} 계정에 관리자 권한을 부여했습니다.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/availability-requests/<int:request_id>/approve", methods=["POST"])
@login_required
@role_required("admin")
def approve_availability_request(request_id):
    db = get_db()
    db.execute(
        "UPDATE availability_requests SET status = 'approved' WHERE id = ?",
        (request_id,),
    )
    db.commit()
    write_audit_log("availability.approve", f"request_id={request_id} 승인")
    flash("요청이 승인되었습니다.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/availability-requests/<int:request_id>/reject", methods=["POST"])
@login_required
@role_required("admin")
def reject_availability_request(request_id):
    db = get_db()
    db.execute(
        "UPDATE availability_requests SET status = 'rejected' WHERE id = ?",
        (request_id,),
    )
    db.commit()
    write_audit_log("availability.reject", f"request_id={request_id} 반려")
    flash("요청이 반려되었습니다.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/shifts", methods=["POST"])
@login_required
@role_required("admin")
def create_shift():
    db = get_db()
    staff_id = request.form["staff_id"]
    work_date = request.form["work_date"]
    start_time = request.form["start_time"]
    end_time = request.form["end_time"]
    overlap = db.execute(
        """
        SELECT id FROM shifts
        WHERE staff_id = ? AND work_date = ?
          AND NOT (end_time <= ? OR start_time >= ?)
        """,
        (staff_id, work_date, start_time, end_time),
    ).fetchone()
    if overlap:
        flash("동일 직원의 같은 날짜/시간대 스케줄이 이미 존재합니다.")
        return redirect(url_for("admin_dashboard"))

    db.execute(
        """
        INSERT INTO shifts (staff_id, work_date, start_time, end_time, note, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            staff_id,
            work_date,
            start_time,
            end_time,
            request.form.get("note", "").strip(),
            session["user_id"],
            datetime.now().isoformat(),
        ),
    )
    db.commit()
    write_audit_log("shift.create", f"staff_id={staff_id}, date={work_date}, {start_time}-{end_time}")
    notify_staff_by_id(
        staff_id,
        "[하남 근무관리] 스케줄 등록 안내",
        f"근무 스케줄이 등록되었습니다.\n날짜: {work_date}\n시간: {start_time}~{end_time}",
    )
    flash("근무 스케줄이 등록되었습니다.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/work-logs/<int:log_id>/approve", methods=["POST"])
@login_required
@role_required("admin")
def approve_work_log(log_id):
    db = get_db()
    db.execute(
        """
        UPDATE work_logs
        SET status = 'approved', reviewed_by = ?, reviewed_at = ?
        WHERE id = ?
        """,
        (session["user_id"], datetime.now().isoformat(), log_id),
    )
    db.commit()
    write_audit_log("worklog.approve", f"log_id={log_id} 승인")
    flash("근무기록이 승인되었습니다.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/work-logs/<int:log_id>/reject", methods=["POST"])
@login_required
@role_required("admin")
def reject_work_log(log_id):
    db = get_db()
    db.execute(
        """
        UPDATE work_logs
        SET status = 'rejected', reviewed_by = ?, reviewed_at = ?
        WHERE id = ?
        """,
        (session["user_id"], datetime.now().isoformat(), log_id),
    )
    db.commit()
    write_audit_log("worklog.reject", f"log_id={log_id} 반려")
    flash("근무기록이 반려되었습니다.")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reports/monthly.csv")
@login_required
@role_required("admin")
def monthly_report_csv():
    db = get_db()
    rows = db.execute(
        """
        SELECT
            u.full_name AS staff_name,
            COALESCE(SUM(CASE WHEN wl.status = 'approved' THEN wl.total_minutes ELSE 0 END), 0) AS approved_minutes
        FROM users u
        LEFT JOIN work_logs wl ON wl.staff_id = u.id
        WHERE u.role = 'staff' AND u.is_deleted = 0
        GROUP BY u.id, u.full_name
        ORDER BY u.full_name
        """
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["직원명", "승인근무분", "급여반영시간(30분단위)"])
    for row in rows:
        writer.writerow([row["staff_name"], row["approved_minutes"], minutes_to_pay_hours(row["approved_minutes"])])

    write_audit_log("report.download", "월간 리포트 CSV 다운로드")
    return Response(
        output.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=monthly_report.csv"},
    )


@app.route("/staff")
@login_required
@role_required("staff")
def staff_dashboard():
    db = get_db()
    user_id = session["user_id"]
    now = datetime.now()
    month_prefix = now.strftime("%Y-%m")
    staff_selected_date = request.args.get("date", now.strftime("%Y-%m-%d"))
    staff_month_param = request.args.get("month", staff_selected_date[:7])
    try:
        staff_view_year = int(staff_month_param.split("-")[0])
        staff_view_month_number = int(staff_month_param.split("-")[1])
        if not (1 <= staff_view_month_number <= 12):
            raise ValueError
    except (ValueError, IndexError):
        staff_view_year = now.year
        staff_view_month_number = now.month
    staff_view_month = f"{staff_view_year:04d}-{staff_view_month_number:02d}"
    staff_month_start = f"{staff_view_year:04d}-{staff_view_month_number:02d}-01"
    staff_month_end_day = calendar.monthrange(staff_view_year, staff_view_month_number)[1]
    staff_month_end = f"{staff_view_year:04d}-{staff_view_month_number:02d}-{staff_month_end_day:02d}"

    month_shifts = db.execute(
        """
        SELECT work_date, start_time, end_time, note
        FROM shifts
        WHERE staff_id = ? AND work_date BETWEEN ? AND ?
        ORDER BY work_date ASC, start_time ASC
        """,
        (user_id, staff_month_start, staff_month_end),
    ).fetchall()
    shifts_by_date = {}
    for row in month_shifts:
        shifts_by_date.setdefault(row["work_date"], []).append(row)
    staff_calendar_weeks = build_month_calendar(
        staff_view_year, staff_view_month_number, shifts_by_date, staff_selected_date
    )
    my_day_shifts = db.execute(
        """
        SELECT work_date, start_time, end_time, note
        FROM shifts
        WHERE staff_id = ? AND work_date = ?
        ORDER BY start_time ASC
        """,
        (user_id, staff_selected_date),
    ).fetchall()

    month_minutes_row = db.execute(
        """
        SELECT COALESCE(SUM(total_minutes), 0) AS total_minutes
        FROM work_logs
        WHERE staff_id = ? AND status = 'approved' AND work_date LIKE ?
        """,
        (user_id, f"{month_prefix}%"),
    ).fetchone()

    my_logs = db.execute(
        """
        SELECT id, work_date, clock_in, clock_out, total_minutes, status
        FROM work_logs
        WHERE staff_id = ?
        ORDER BY work_date DESC
        LIMIT 20
        """,
        (user_id,),
    ).fetchall()
    my_requests = db.execute(
        """
        SELECT request_type, weekday, request_date, note, status, created_at
        FROM availability_requests
        WHERE staff_id = ?
        ORDER BY created_at DESC
        LIMIT 20
        """,
        (user_id,),
    ).fetchall()
    pending_log_count = len([log for log in my_logs if log["status"] == "pending"])
    request_pending_count = len([req for req in my_requests if req["status"] == "pending"])
    shift_count = len(month_shifts)
    return render_template(
        "staff_dashboard.html",
        my_shifts=month_shifts,
        month_minutes=month_minutes_row["total_minutes"],
        month_pay_hours=minutes_to_pay_hours(month_minutes_row["total_minutes"]),
        my_logs=my_logs,
        my_requests=my_requests,
        pending_log_count=pending_log_count,
        request_pending_count=request_pending_count,
        shift_count=shift_count,
        staff_view_month=staff_view_month,
        staff_selected_date=staff_selected_date,
        staff_calendar_weeks=staff_calendar_weeks,
        my_day_shifts=my_day_shifts,
        minutes_to_pay_hours=minutes_to_pay_hours,
        format_request_detail=format_request_detail,
    )


@app.route("/staff/work-logs", methods=["POST"])
@login_required
@role_required("staff")
def create_work_log():
    clock_in = datetime.strptime(request.form["clock_in"], "%H:%M")
    clock_out = datetime.strptime(request.form["clock_out"], "%H:%M")
    total_minutes = int((clock_out - clock_in).total_seconds() // 60)
    if total_minutes <= 0:
        flash("퇴근 시간이 출근 시간보다 늦어야 합니다.")
        return redirect(url_for("staff_dashboard"))

    db = get_db()
    db.execute(
        """
        INSERT INTO work_logs (staff_id, work_date, clock_in, clock_out, total_minutes, note, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            session["user_id"],
            request.form["work_date"],
            request.form["clock_in"],
            request.form["clock_out"],
            total_minutes,
            request.form.get("note", "").strip(),
            datetime.now().isoformat(),
        ),
    )
    db.commit()
    write_audit_log("worklog.create", f"date={request.form['work_date']}, {request.form['clock_in']}-{request.form['clock_out']}")
    flash("근무기록이 등록되었습니다. 관리자 승인 후 집계됩니다.")
    return redirect(url_for("staff_dashboard"))


@app.route("/staff/availability-requests", methods=["POST"])
@login_required
@role_required("staff")
def create_availability_request():
    request_type = request.form["request_type"]
    weekday = request.form.get("weekday")
    request_date = request.form.get("request_date")
    note = request.form.get("note", "").strip()

    if request_type == "preferred_weekday" and (weekday is None or weekday == ""):
        flash("희망 근무요일을 선택해 주세요.")
        return redirect(url_for("staff_dashboard"))
    if request_type == "day_off" and not request_date:
        flash("휴무 희망일을 선택해 주세요.")
        return redirect(url_for("staff_dashboard"))
    if request_type == "preferred_weekday":
        request_date = None
    if request_type == "day_off":
        weekday = None

    db = get_db()
    db.execute(
        """
        INSERT INTO availability_requests (staff_id, request_type, weekday, request_date, note, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (
            session["user_id"],
            request_type,
            int(weekday) if weekday not in (None, "") else None,
            request_date if request_date else None,
            note,
            datetime.now().isoformat(),
        ),
    )
    db.commit()
    write_audit_log(
        "availability.create",
        f"type={request_type}, weekday={weekday}, request_date={request_date}",
    )
    flash("희망 근무/휴무 요청이 등록되었습니다.")
    return redirect(url_for("staff_dashboard"))


@app.route("/board")
@login_required
def board_list():
    db = get_db()
    posts = db.execute(
        """
        SELECT p.id, p.author_id, p.title, p.content, p.image_path, p.created_at, COUNT(c.id) AS comment_count
        FROM board_posts p
        LEFT JOIN board_comments c ON c.post_id = p.id
        GROUP BY p.id
        ORDER BY p.created_at DESC
        LIMIT 100
        """
    ).fetchall()
    return render_template("board_list.html", posts=posts)


@app.route("/board/new", methods=["POST"])
@login_required
def create_board_post():
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    image_path = save_uploaded_image(request.files.get("image"))
    if len(title) < 2 or len(content) < 2:
        flash("제목/내용을 2자 이상 입력해 주세요.")
        return redirect(url_for("board_list"))
    if image_path is None:
        flash("이미지는 png/jpg/jpeg/gif/webp 형식만 업로드할 수 있습니다.")
        return redirect(url_for("board_list"))

    db = get_db()
    db.execute(
        """
        INSERT INTO board_posts (author_id, title, content, image_path, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (session["user_id"], title, content, image_path or "", datetime.now().isoformat()),
    )
    db.commit()
    write_audit_log("board.post.create", f"title={title}")
    flash("익명 게시글이 등록되었습니다.")
    return redirect(url_for("board_list"))


@app.route("/board/<int:post_id>")
@login_required
def board_detail(post_id):
    db = get_db()
    post = db.execute(
        """
        SELECT id, author_id, title, content, image_path, created_at
        FROM board_posts
        WHERE id = ?
        """,
        (post_id,),
    ).fetchone()
    if not post:
        flash("게시글을 찾을 수 없습니다.")
        return redirect(url_for("board_list"))

    comments = db.execute(
        """
        SELECT id, author_id, content, image_path, created_at, parent_comment_id
        FROM board_comments
        WHERE post_id = ?
        ORDER BY created_at ASC
        """,
        (post_id,),
    ).fetchall()
    parent_comments = [c for c in comments if c["parent_comment_id"] is None]
    replies_by_parent = {}
    for c in comments:
        if c["parent_comment_id"] is not None:
            replies_by_parent.setdefault(c["parent_comment_id"], []).append(c)
    comment_author_alias = build_comment_author_aliases(comments)
    return render_template(
        "board_detail.html",
        post=post,
        parent_comments=parent_comments,
        replies_by_parent=replies_by_parent,
        comment_author_alias=comment_author_alias,
    )


@app.route("/board/<int:post_id>/comments", methods=["POST"])
@login_required
def create_board_comment(post_id):
    content = request.form.get("content", "").strip()
    parent_comment_id_raw = request.form.get("parent_comment_id", "").strip()
    image_path = save_uploaded_image(request.files.get("image"))
    if len(content) < 1:
        flash("댓글 내용을 입력해 주세요.")
        return redirect(url_for("board_detail", post_id=post_id))
    if image_path is None:
        flash("이미지는 png/jpg/jpeg/gif/webp 형식만 업로드할 수 있습니다.")
        return redirect(url_for("board_detail", post_id=post_id))

    db = get_db()
    exists = db.execute("SELECT id FROM board_posts WHERE id = ?", (post_id,)).fetchone()
    if not exists:
        flash("게시글을 찾을 수 없습니다.")
        return redirect(url_for("board_list"))

    parent_comment_id = None
    if parent_comment_id_raw:
        try:
            parent_comment_id = int(parent_comment_id_raw)
        except ValueError:
            flash("잘못된 대댓글 요청입니다.")
            return redirect(url_for("board_detail", post_id=post_id))
        parent_comment = db.execute(
            "SELECT id FROM board_comments WHERE id = ? AND post_id = ?",
            (parent_comment_id, post_id),
        ).fetchone()
        if not parent_comment:
            flash("원댓글을 찾을 수 없습니다.")
            return redirect(url_for("board_detail", post_id=post_id))

    db.execute(
        """
        INSERT INTO board_comments (post_id, author_id, parent_comment_id, content, image_path, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (post_id, session["user_id"], parent_comment_id, content, image_path or "", datetime.now().isoformat()),
    )
    db.commit()
    write_audit_log("board.comment.create", f"post_id={post_id}")
    flash("익명 댓글이 등록되었습니다.")
    return redirect(url_for("board_detail", post_id=post_id))


@app.route("/notices")
@login_required
def notice_list():
    db = get_db()
    notices = db.execute(
        """
        SELECT n.id, n.title, n.content, n.created_at, u.full_name AS author_name
        FROM notices n
        JOIN users u ON u.id = n.author_id
        ORDER BY n.created_at DESC
        LIMIT 100
        """
    ).fetchall()
    return render_template("notice_list.html", notices=notices)


@app.route("/notices/new", methods=["POST"])
@login_required
@role_required("admin")
def create_notice():
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    if len(title) < 2 or len(content) < 2:
        flash("공지 제목/내용을 2자 이상 입력해 주세요.")
        return redirect(url_for("notice_list"))

    db = get_db()
    db.execute(
        """
        INSERT INTO notices (author_id, title, content, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (session["user_id"], title, content, datetime.now().isoformat()),
    )
    db.commit()
    write_audit_log("notice.create", f"title={title}")
    flash("공지사항이 등록되었습니다.")
    return redirect(url_for("notice_list"))


@app.route("/notices/<int:notice_id>")
@login_required
def notice_detail(notice_id):
    db = get_db()
    notice = db.execute(
        """
        SELECT n.id, n.title, n.content, n.created_at, u.full_name AS author_name
        FROM notices n
        JOIN users u ON u.id = n.author_id
        WHERE n.id = ?
        """,
        (notice_id,),
    ).fetchone()
    if not notice:
        flash("공지사항을 찾을 수 없습니다.")
        return redirect(url_for("notice_list"))
    return render_template("notice_detail.html", notice=notice)


def _collect_manual_images(manual_id):
    """레거시 image_path + manual_images 를 하나의 리스트로 반환."""
    db = get_db()
    manual_row = db.execute(
        "SELECT image_path FROM manuals WHERE id = ?", (manual_id,)
    ).fetchone()
    items = []
    if manual_row and manual_row["image_path"]:
        items.append({"id": None, "image_path": manual_row["image_path"], "legacy": True})
    rows = db.execute(
        """
        SELECT id, image_path FROM manual_images
        WHERE manual_id = ?
        ORDER BY sort_order ASC, id ASC
        """,
        (manual_id,),
    ).fetchall()
    for r in rows:
        items.append({"id": r["id"], "image_path": r["image_path"], "legacy": False})
    return items


def _save_manual_images(manual_id, file_storages):
    """여러 개의 파일을 manual_images에 순차 저장. 문제가 있으면 False 반환."""
    now_iso = datetime.now().isoformat()
    db = get_db()
    existing_count = db.execute(
        "SELECT COUNT(*) AS c FROM manual_images WHERE manual_id = ?",
        (manual_id,),
    ).fetchone()
    base_order = (existing_count["c"] if existing_count else 0)
    order = base_order
    for fs in file_storages:
        if not fs or not fs.filename:
            continue
        saved = save_uploaded_image(fs)
        if saved is None:
            return False
        if not saved:
            continue
        db.execute(
            """
            INSERT INTO manual_images (manual_id, image_path, sort_order, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (manual_id, saved, order, now_iso),
        )
        order += 1
    return True


@app.route("/manual")
@login_required
def manual_list():
    db = get_db()
    manuals = db.execute(
        """
        SELECT m.id, m.title, m.content, m.image_path, m.sort_order,
               m.created_at, m.updated_at,
               u.full_name AS author_name,
               (CASE WHEN m.image_path <> '' THEN 1 ELSE 0 END)
                 + (SELECT COUNT(*) FROM manual_images mi WHERE mi.manual_id = m.id)
                 AS image_count
        FROM manuals m
        JOIN users u ON u.id = m.author_id
        ORDER BY m.sort_order ASC, m.id ASC
        """
    ).fetchall()
    return render_template("manual_list.html", manuals=manuals)


@app.route("/manual/new", methods=["POST"])
@login_required
@role_required("admin")
def create_manual():
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    sort_order_raw = request.form.get("sort_order", "0").strip() or "0"
    try:
        sort_order = int(sort_order_raw)
    except ValueError:
        sort_order = 0
    if len(title) < 2 or len(content) < 2:
        flash("매뉴얼 제목/내용을 2자 이상 입력해 주세요.")
        return redirect(url_for("manual_list"))

    now_iso = datetime.now().isoformat()
    db = get_db()
    cursor = db.execute(
        """
        INSERT INTO manuals (author_id, title, content, image_path, sort_order, created_at, updated_at)
        VALUES (?, ?, ?, '', ?, ?, ?)
        """,
        (session["user_id"], title, content, sort_order, now_iso, now_iso),
    )
    manual_id = getattr(cursor, "lastrowid", None)
    if manual_id is None:
        row = db.execute(
            "SELECT id FROM manuals WHERE author_id = ? ORDER BY id DESC LIMIT 1",
            (session["user_id"],),
        ).fetchone()
        manual_id = row["id"] if row else None

    files = request.files.getlist("images")
    if manual_id and files:
        ok = _save_manual_images(manual_id, files)
        if not ok:
            db.commit()
            flash("허용된 이미지 형식(png, jpg, jpeg, gif, webp)만 업로드할 수 있습니다. 나머지 항목은 저장되었습니다.")
            return redirect(url_for("manual_detail", manual_id=manual_id))

    db.commit()
    write_audit_log("manual.create", f"title={title}")
    flash("매뉴얼 항목이 등록되었습니다.")
    return redirect(url_for("manual_list"))


@app.route("/manual/<int:manual_id>")
@login_required
def manual_detail(manual_id):
    db = get_db()
    manual = db.execute(
        """
        SELECT m.id, m.title, m.content, m.image_path, m.sort_order,
               m.created_at, m.updated_at,
               u.full_name AS author_name
        FROM manuals m
        JOIN users u ON u.id = m.author_id
        WHERE m.id = ?
        """,
        (manual_id,),
    ).fetchone()
    if not manual:
        flash("매뉴얼 항목을 찾을 수 없습니다.")
        return redirect(url_for("manual_list"))
    images = _collect_manual_images(manual_id)
    return render_template("manual_detail.html", manual=manual, images=images)


@app.route("/manual/<int:manual_id>/edit", methods=["POST"])
@login_required
@role_required("admin")
def update_manual(manual_id):
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    sort_order_raw = request.form.get("sort_order", "0").strip() or "0"
    try:
        sort_order = int(sort_order_raw)
    except ValueError:
        sort_order = 0
    if len(title) < 2 or len(content) < 2:
        flash("매뉴얼 제목/내용을 2자 이상 입력해 주세요.")
        return redirect(url_for("manual_detail", manual_id=manual_id))

    db = get_db()
    existing = db.execute(
        "SELECT image_path FROM manuals WHERE id = ?",
        (manual_id,),
    ).fetchone()
    if not existing:
        flash("매뉴얼 항목을 찾을 수 없습니다.")
        return redirect(url_for("manual_list"))

    current_legacy = existing["image_path"] or ""
    if request.form.get("remove_legacy_image") == "1" and current_legacy:
        delete_uploaded_image(current_legacy)
        current_legacy = ""

    db.execute(
        """
        UPDATE manuals
        SET title = ?, content = ?, image_path = ?, sort_order = ?, updated_at = ?
        WHERE id = ?
        """,
        (title, content, current_legacy, sort_order, datetime.now().isoformat(), manual_id),
    )

    files = request.files.getlist("images")
    if files:
        ok = _save_manual_images(manual_id, files)
        if not ok:
            db.commit()
            flash("허용된 이미지 형식(png, jpg, jpeg, gif, webp)만 업로드할 수 있습니다. 다른 변경사항은 저장되었습니다.")
            return redirect(url_for("manual_detail", manual_id=manual_id))

    db.commit()
    write_audit_log("manual.update", f"manual_id={manual_id} title={title}")
    flash("매뉴얼 항목이 수정되었습니다.")
    return redirect(url_for("manual_detail", manual_id=manual_id))


@app.route("/manual/<int:manual_id>/images/<int:image_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_manual_image(manual_id, image_id):
    db = get_db()
    row = db.execute(
        "SELECT image_path FROM manual_images WHERE id = ? AND manual_id = ?",
        (image_id, manual_id),
    ).fetchone()
    if not row:
        flash("이미지를 찾을 수 없습니다.")
        return redirect(url_for("manual_detail", manual_id=manual_id))
    delete_uploaded_image(row["image_path"])
    db.execute(
        "DELETE FROM manual_images WHERE id = ? AND manual_id = ?",
        (image_id, manual_id),
    )
    db.commit()
    write_audit_log("manual.image.delete", f"manual_id={manual_id} image_id={image_id}")
    flash("이미지가 삭제되었습니다.")
    return redirect(url_for("manual_detail", manual_id=manual_id))


@app.route("/manual/<int:manual_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_manual(manual_id):
    db = get_db()
    legacy = db.execute(
        "SELECT image_path FROM manuals WHERE id = ?",
        (manual_id,),
    ).fetchone()
    image_rows = db.execute(
        "SELECT image_path FROM manual_images WHERE manual_id = ?",
        (manual_id,),
    ).fetchall()
    if legacy:
        delete_uploaded_image(legacy["image_path"])
    for r in image_rows:
        delete_uploaded_image(r["image_path"])
    db.execute("DELETE FROM manual_images WHERE manual_id = ?", (manual_id,))
    db.execute("DELETE FROM manuals WHERE id = ?", (manual_id,))
    db.commit()
    write_audit_log("manual.delete", f"manual_id={manual_id}")
    flash("매뉴얼 항목이 삭제되었습니다.")
    return redirect(url_for("manual_list"))


@app.route("/board/<int:post_id>/delete", methods=["POST"])
@login_required
def delete_board_post(post_id):
    db = get_db()
    post = db.execute(
        "SELECT id, author_id, image_path FROM board_posts WHERE id = ?",
        (post_id,),
    ).fetchone()
    if not post:
        flash("게시글을 찾을 수 없습니다.")
        return redirect(url_for("board_list"))
    if post["author_id"] != session["user_id"]:
        flash("삭제 권한이 없습니다.")
        return redirect(url_for("board_detail", post_id=post_id))

    comment_images = db.execute(
        "SELECT image_path FROM board_comments WHERE post_id = ?",
        (post_id,),
    ).fetchall()
    db.execute("DELETE FROM board_comments WHERE post_id = ?", (post_id,))
    db.execute("DELETE FROM board_posts WHERE id = ?", (post_id,))
    db.commit()
    delete_uploaded_image(post["image_path"])
    for c in comment_images:
        delete_uploaded_image(c["image_path"])
    write_audit_log("board.post.delete", f"post_id={post_id}")
    flash("게시글이 삭제되었습니다.")
    return redirect(url_for("board_list"))


@app.route("/board/comments/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_board_comment(comment_id):
    db = get_db()
    comment = db.execute(
        "SELECT id, post_id, author_id, image_path FROM board_comments WHERE id = ?",
        (comment_id,),
    ).fetchone()
    if not comment:
        flash("댓글을 찾을 수 없습니다.")
        return redirect(url_for("board_list"))
    if comment["author_id"] != session["user_id"]:
        flash("삭제 권한이 없습니다.")
        return redirect(url_for("board_detail", post_id=comment["post_id"]))

    reply_images = db.execute(
        "SELECT image_path FROM board_comments WHERE parent_comment_id = ?",
        (comment_id,),
    ).fetchall()
    db.execute("DELETE FROM board_comments WHERE parent_comment_id = ?", (comment_id,))
    db.execute("DELETE FROM board_comments WHERE id = ?", (comment_id,))
    db.commit()
    delete_uploaded_image(comment["image_path"])
    for r in reply_images:
        delete_uploaded_image(r["image_path"])
    write_audit_log("board.comment.delete", f"comment_id={comment_id}")
    flash("댓글이 삭제되었습니다.")
    return redirect(url_for("board_detail", post_id=comment["post_id"]))


_DB_INITIALIZED = False


@app.before_request
def ensure_db():
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    init_db()
    _DB_INITIALIZED = True


if __name__ == "__main__":
    app.run(debug=True)
