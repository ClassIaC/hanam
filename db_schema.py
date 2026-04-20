"""SQLite / Postgres 초기 스키마 문자열 (app.init_db에서 사용)."""

SQLITE_CREATE_SCRIPT = """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            full_name TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '',
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

        CREATE TABLE IF NOT EXISTS board_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            image_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(author_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS board_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            author_id INTEGER NOT NULL,
            parent_comment_id INTEGER,
            content TEXT NOT NULL,
            image_path TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(post_id) REFERENCES board_posts(id),
            FOREIGN KEY(author_id) REFERENCES users(id),
            FOREIGN KEY(parent_comment_id) REFERENCES board_comments(id)
        );

        CREATE TABLE IF NOT EXISTS notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(author_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS manuals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(author_id) REFERENCES users(id)
        );
"""

PG_INIT_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        username TEXT UNIQUE NOT NULL,
        full_name TEXT NOT NULL DEFAULT '',
        email TEXT NOT NULL DEFAULT '',
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('admin', 'staff')),
        must_change_password INTEGER NOT NULL DEFAULT 1,
        is_active INTEGER NOT NULL DEFAULT 1,
        approval_status TEXT NOT NULL DEFAULT 'approved' CHECK (approval_status IN ('pending', 'approved', 'rejected')),
        approved_by INTEGER REFERENCES users(id),
        approved_at TEXT,
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS shifts (
        id SERIAL PRIMARY KEY,
        staff_id INTEGER NOT NULL REFERENCES users(id),
        work_date TEXT NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        note TEXT,
        created_by INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS work_logs (
        id SERIAL PRIMARY KEY,
        staff_id INTEGER NOT NULL REFERENCES users(id),
        work_date TEXT NOT NULL,
        clock_in TEXT NOT NULL,
        clock_out TEXT NOT NULL,
        total_minutes INTEGER NOT NULL,
        note TEXT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
        reviewed_by INTEGER REFERENCES users(id),
        reviewed_at TEXT,
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS availability_requests (
        id SERIAL PRIMARY KEY,
        staff_id INTEGER NOT NULL REFERENCES users(id),
        request_type TEXT NOT NULL CHECK (request_type IN ('preferred_weekday', 'day_off')),
        weekday INTEGER,
        request_date TEXT,
        note TEXT,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_logs (
        id SERIAL PRIMARY KEY,
        actor_id INTEGER NOT NULL REFERENCES users(id),
        action TEXT NOT NULL,
        details TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS board_posts (
        id SERIAL PRIMARY KEY,
        author_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        image_path TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS board_comments (
        id SERIAL PRIMARY KEY,
        post_id INTEGER NOT NULL REFERENCES board_posts(id),
        author_id INTEGER NOT NULL REFERENCES users(id),
        parent_comment_id INTEGER REFERENCES board_comments(id),
        content TEXT NOT NULL,
        image_path TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS notices (
        id SERIAL PRIMARY KEY,
        author_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS manuals (
        id SERIAL PRIMARY KEY,
        author_id INTEGER NOT NULL REFERENCES users(id),
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """,
]
