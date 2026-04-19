from datetime import datetime
import calendar
import sqlite3
from functools import wraps
import csv
import io

from flask import Flask, g, redirect, render_template, request, session, url_for, flash, Response
from werkzeug.security import check_password_hash, generate_password_hash


app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-in-production"
app.config["DATABASE"] = "hanam.db"
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


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exception):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def ensure_column(db, table_name, column_name, alter_sql):
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


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL DEFAULT '',
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'staff')),
            must_change_password INTEGER NOT NULL DEFAULT 1,
            is_active INTEGER NOT NULL DEFAULT 1,
            approval_status TEXT NOT NULL DEFAULT 'approved' CHECK (approval_status IN ('pending', 'approved', 'rejected')),
            approved_by INTEGER,
            approved_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(approved_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS shifts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            note TEXT,
            created_by INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(staff_id) REFERENCES users(id),
            FOREIGN KEY(created_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS work_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            work_date TEXT NOT NULL,
            clock_in TEXT NOT NULL,
            clock_out TEXT NOT NULL,
            total_minutes INTEGER NOT NULL,
            note TEXT,
            status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')) DEFAULT 'pending',
            reviewed_by INTEGER,
            reviewed_at TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(staff_id) REFERENCES users(id),
            FOREIGN KEY(reviewed_by) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS availability_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            staff_id INTEGER NOT NULL,
            request_type TEXT NOT NULL CHECK (request_type IN ('preferred_weekday', 'day_off')),
            weekday INTEGER,
            request_date TEXT,
            note TEXT,
            status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')) DEFAULT 'pending',
            created_at TEXT NOT NULL,
            FOREIGN KEY(staff_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id INTEGER NOT NULL,
            action TEXT NOT NULL,
            details TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(actor_id) REFERENCES users(id)
        );
        """
    )

    ensure_column(db, "users", "full_name", "ALTER TABLE users ADD COLUMN full_name TEXT NOT NULL DEFAULT ''")
    ensure_column(
        db,
        "users",
        "approval_status",
        "ALTER TABLE users ADD COLUMN approval_status TEXT NOT NULL DEFAULT 'approved' CHECK (approval_status IN ('pending', 'approved', 'rejected'))",
    )
    ensure_column(db, "users", "approved_by", "ALTER TABLE users ADD COLUMN approved_by INTEGER")
    ensure_column(db, "users", "approved_at", "ALTER TABLE users ADD COLUMN approved_at TEXT")
    ensure_column(db, "users", "is_deleted", "ALTER TABLE users ADD COLUMN is_deleted INTEGER NOT NULL DEFAULT 0")
    db.execute(
        """
        UPDATE users
        SET is_deleted = 1, is_active = 0
        WHERE role = 'staff' AND (
            full_name LIKE '%(삭제됨)%' OR username LIKE '%_deleted_%'
        )
        """
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
        password = request.form["password"]
        if len(username) < 3 or len(full_name) < 2 or len(password) < 8:
            flash("아이디 3자 이상, 이름 2자 이상, 비밀번호 8자 이상으로 입력해 주세요.")
            return redirect(url_for("register"))

        db = get_db()
        try:
            db.execute(
                """
                INSERT INTO users (
                    username, full_name, password_hash, role, must_change_password,
                    is_active, approval_status, is_deleted, created_at
                )
                VALUES (?, ?, ?, 'staff', 0, 0, 'pending', 0, ?)
                """,
                (username, full_name, generate_password_hash(password), datetime.now().isoformat()),
            )
            db.commit()
            flash("회원가입이 완료되었습니다. 관리자 승인 후 로그인할 수 있습니다.")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
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
        SELECT id, username, full_name, is_active, approval_status
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
    recent_audit_logs = db.execute(
        """
        SELECT a.action, a.details, a.created_at, u.full_name AS actor_name
        FROM audit_logs a
        JOIN users u ON u.id = a.actor_id
        ORDER BY a.created_at DESC
        LIMIT 15
        """
    ).fetchall()

    return render_template(
        "admin_dashboard.html",
        staff_list=staff_list,
        pending_staff=pending_staff,
        shifts=shifts,
        pending_logs=pending_logs,
        monthly_hours=monthly_hours,
        recent_requests=recent_requests,
        pending_requests=pending_requests,
        recent_audit_logs=recent_audit_logs,
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
    temp_password = request.form["temp_password"]

    if len(username) < 3 or len(full_name) < 2 or len(temp_password) < 8:
        flash("아이디 3자 이상, 이름 2자 이상, 초기 비밀번호 8자 이상이어야 합니다.")
        return redirect(url_for("admin_dashboard"))

    db = get_db()
    try:
        db.execute(
            """
            INSERT INTO users (
                username, full_name, password_hash, role, must_change_password,
                is_active, approval_status, approved_by, approved_at, is_deleted, created_at
            )
            VALUES (?, ?, ?, 'staff', 1, 1, 'approved', ?, ?, 0, ?)
            """,
            (
                username,
                full_name,
                generate_password_hash(temp_password),
                session["user_id"],
                datetime.now().isoformat(),
                datetime.now().isoformat(),
            ),
        )
        db.commit()
        write_audit_log("staff.create", f"{full_name}({username}) 계정 생성")
        flash("알바 계정이 생성되었습니다.")
    except sqlite3.IntegrityError:
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
    flash(f"{staff['full_name']} 계정 비밀번호가 초기화되었습니다. 다음 로그인 시 변경됩니다.")
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


@app.before_request
def ensure_db():
    init_db()


if __name__ == "__main__":
    app.run(debug=True)
