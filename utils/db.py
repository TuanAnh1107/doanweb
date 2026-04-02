import sqlite3
from datetime import date, datetime, time
from pathlib import Path

from flask import current_app
from werkzeug.security import generate_password_hash

try:
    import pymysql
except ImportError:  # pragma: no cover - optional dependency when using SQLite
    pymysql = None

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency when using PostgreSQL
    psycopg = None
    dict_row = None


PASSWORD_HASH_PREFIXES = ("scrypt:", "pbkdf2:")


class SQLiteCursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self._cursor.close()

    def execute(self, sql, params=None):
        normalized_sql = sql.replace("%s", "?")
        self._cursor.execute(normalized_sql, _normalize_sqlite_params(params))
        return self

    def fetchone(self):
        row = self._cursor.fetchone()
        return dict(row) if row else None

    def fetchall(self):
        return [dict(row) for row in self._cursor.fetchall()]


class SQLiteConnectionWrapper:
    def __init__(self, connection):
        self._connection = connection

    def cursor(self):
        return SQLiteCursorWrapper(self._connection.cursor())

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def hash_password(password):
    return generate_password_hash(password)


def is_password_hashed(password):
    return isinstance(password, str) and password.startswith(PASSWORD_HASH_PREFIXES)


def _normalize_sqlite_value(value):
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat(timespec="seconds")
    return value


def _normalize_sqlite_params(params):
    if params is None:
        return []
    if isinstance(params, (list, tuple)):
        return [_normalize_sqlite_value(value) for value in params]
    return [_normalize_sqlite_value(params)]


def _ensure_parent_dir(db_path):
    db_path.parent.mkdir(parents=True, exist_ok=True)


def _create_sqlite_connection(db_path):
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    _ensure_sqlite_schema(connection)
    return SQLiteConnectionWrapper(connection)


def _ensure_sqlite_schema(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS teachers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            phone TEXT,
            department TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_code TEXT NOT NULL UNIQUE,
            full_name TEXT NOT NULL,
            email TEXT UNIQUE,
            password TEXT NOT NULL,
            phone TEXT,
            class_name TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS classes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_code TEXT NOT NULL UNIQUE,
            class_name TEXT NOT NULL,
            subject_name TEXT NOT NULL,
            teacher_id INTEGER NOT NULL,
            semester TEXT,
            school_year TEXT,
            room TEXT,
            schedule_info TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (teacher_id) REFERENCES teachers(id) ON DELETE CASCADE ON UPDATE CASCADE
        );

        CREATE TABLE IF NOT EXISTS class_students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (class_id, student_id),
            FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE ON UPDATE CASCADE,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE ON UPDATE CASCADE
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL,
            session_title TEXT,
            session_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            qr_token TEXT NOT NULL UNIQUE,
            qr_expired_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE ON UPDATE CASCADE
        );

        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            checkin_time TEXT,
            attendance_status TEXT NOT NULL DEFAULT 'absent',
            note TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (session_id, student_id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE ON UPDATE CASCADE,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE ON UPDATE CASCADE
        );

        CREATE TABLE IF NOT EXISTS assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            class_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            due_date TEXT NOT NULL,
            attachment_path TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE ON UPDATE CASCADE
        );

        CREATE TABLE IF NOT EXISTS submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignment_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            file_path TEXT,
            submission_text TEXT,
            submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'submitted',
            teacher_score REAL,
            teacher_comment TEXT,
            UNIQUE (assignment_id, student_id),
            FOREIGN KEY (assignment_id) REFERENCES assignments(id) ON DELETE CASCADE ON UPDATE CASCADE,
            FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE ON UPDATE CASCADE
        );

        CREATE TABLE IF NOT EXISTS ai_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL UNIQUE,
            completion_level INTEGER,
            suggested_comment TEXT,
            missing_content TEXT,
            similarity_warning TEXT,
            ai_score REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (submission_id) REFERENCES submissions(id) ON DELETE CASCADE ON UPDATE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_classes_teacher_id ON classes (teacher_id);
        CREATE INDEX IF NOT EXISTS idx_class_students_class_id ON class_students (class_id);
        CREATE INDEX IF NOT EXISTS idx_class_students_student_id ON class_students (student_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_class_id_date ON sessions (class_id, session_date);
        CREATE INDEX IF NOT EXISTS idx_attendance_session_id ON attendance (session_id);
        CREATE INDEX IF NOT EXISTS idx_attendance_student_id ON attendance (student_id);
        CREATE INDEX IF NOT EXISTS idx_assignments_class_id_due_date ON assignments (class_id, due_date);
        CREATE INDEX IF NOT EXISTS idx_submissions_assignment_id ON submissions (assignment_id);
        CREATE INDEX IF NOT EXISTS idx_submissions_student_id_status ON submissions (student_id, status);
        CREATE INDEX IF NOT EXISTS idx_ai_feedback_submission_id ON ai_feedback (submission_id);
        """
    )
    _seed_sqlite_data(connection)
    _normalize_sqlite_sample_data(connection)


def _seed_sqlite_data(connection):
    teacher_exists = connection.execute("SELECT 1 FROM teachers LIMIT 1").fetchone()
    if teacher_exists:
        return

    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    now_ts = now.strftime("%Y-%m-%d %H:%M:%S")
    due_ts = now.replace(hour=23, minute=59, second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")
    qr_expired_at = now.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S")

    connection.execute(
        """
        INSERT INTO teachers (id, full_name, email, password, phone, department)
        VALUES (?, ?, ?, ?, ?, ?)
        """
        ,
        (1, "Nguyễn Việt Tùng", "tungnv@hust.edu.vn", hash_password("tungdzaimica"), "0123456789", "MICA"),
    )

    connection.executemany(
        """
        INSERT INTO students (id, student_code, full_name, email, password, phone, class_name)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, "20231556", "Nguyễn Tuấn Anh", "anh.nt231556@sis.hust.edu.vn", hash_password("1"), "0911111111", "K68-ED2"),
            (2, "20235555", "Nguyễn Thành Luân", "luannt@sis.hust.edu.vn", hash_password("1"), "0922222222", "K68-ED2"),
            (3, "20237777", "Lê Anh Đức", "ducla@sis.hust.edu.vn", hash_password("1"), "0944444444", "K68-ED2"),
        ],
    )

    connection.execute(
        """
        INSERT INTO classes (id, class_code, class_name, subject_name, teacher_id, semester, school_year, room, schedule_info)
        VALUES (1, 'ED-K68', 'ED2-K68', 'Thiết kế và lập trình web', 1, 'Kì 20252', '2025-2026', 'D9-303', 'Thứ 3 - Tiết 1-3')
        """
    )

    connection.executemany(
        "INSERT INTO class_students (class_id, student_id) VALUES (?, ?)",
        [(1, 1), (1, 2), (1, 3)],
    )

    connection.execute(
        """
        INSERT INTO sessions (id, class_id, session_title, session_date, start_time, end_time, qr_token, qr_expired_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (1, 1, "Buổi học 1", today, "06:45:00", "09:10:00", "QR_TOKEN_SAMPLE_001", qr_expired_at, "open"),
    )

    connection.executemany(
        """
        INSERT INTO attendance (session_id, student_id, checkin_time, attendance_status, note)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (1, 1, now_ts, "present", "Đúng giờ"),
            (1, 2, now_ts, "late", "Trễ 10 phút"),
            (1, 3, None, "absent", "Vắng"),
        ],
    )

    connection.execute(
        """
        INSERT INTO assignments (id, class_id, title, description, due_date, attachment_path)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (1, 1, "Bài tập 1", "Thiết kế giao diện đăng nhập bằng HTML/CSS", due_ts, None),
    )

    connection.executemany(
        """
        INSERT INTO submissions (assignment_id, student_id, file_path, submission_text, submitted_at, status, teacher_score, teacher_comment)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (1, 1, "static/uploads/baitap1_tuananh.pdf", "Bài làm của Nguyễn Tuấn Anh", now_ts, "submitted", 9.5, "Đạt yêu cầu"),
            (1, 2, "static/uploads/baitap1_duc.pdf", "Bài làm của Lê Anh Đức", now_ts, "submitted", None, None),
            (1, 3, None, None, now_ts, "missing", None, None),
        ],
    )

    connection.execute(
        """
        INSERT INTO ai_feedback (submission_id, completion_level, suggested_comment, missing_content, similarity_warning, ai_score)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (1, 85, "Bài làm ổn, giao diện rõ ràng.", "Cần bổ sung validation form.", "Không phát hiện trùng lặp cao.", 8.0),
    )
    connection.commit()


def _normalize_sqlite_sample_data(connection):
    connection.execute(
        """
        UPDATE teachers
        SET full_name = ?, department = ?
        WHERE email = ?
        """,
        ("Nguyễn Việt Tùng", "MICA", "tungnv@hust.edu.vn"),
    )

    connection.executemany(
        """
        UPDATE students
        SET full_name = ?, class_name = ?
        WHERE id = ?
        """,
        [
            ("Nguyễn Tuấn Anh", "K68-ED2", 1),
            ("Nguyễn Thành Luân", "K68-ED2", 2),
            ("Lê Anh Đức", "K68-ED2", 3),
        ],
    )

    _upgrade_sqlite_password_hashes(connection, "teachers")
    _upgrade_sqlite_password_hashes(connection, "students")

    connection.execute(
        """
        UPDATE classes
        SET subject_name = ?, semester = ?, schedule_info = ?
        WHERE id = 1
        """,
        ("Thiết kế và lập trình web", "Kì 20252", "Thứ 3 - Tiết 1-3"),
    )

    connection.execute(
        """
        UPDATE sessions
        SET session_title = ?
        WHERE id = 1
        """,
        ("Buổi học 1",),
    )

    connection.executemany(
        """
        UPDATE attendance
        SET note = ?
        WHERE session_id = ? AND student_id = ?
        """,
        [
            ("Đúng giờ", 1, 1),
            ("Trễ 10 phút", 1, 2),
            ("Vắng", 1, 3),
        ],
    )

    connection.execute(
        """
        UPDATE assignments
        SET title = ?, description = ?
        WHERE id = 1
        """,
        ("Bài tập 1", "Thiết kế giao diện đăng nhập bằng HTML/CSS"),
    )

    connection.executemany(
        """
        UPDATE submissions
        SET submission_text = ?, teacher_comment = ?
        WHERE assignment_id = ? AND student_id = ?
        """,
        [
            ("Bài làm của Nguyễn Tuấn Anh", "Đạt yêu cầu", 1, 1),
            ("Bài làm của Lê Anh Đức", None, 1, 2),
        ],
    )

    connection.execute(
        """
        UPDATE ai_feedback
        SET suggested_comment = ?, missing_content = ?, similarity_warning = ?
        WHERE submission_id = 1
        """,
        ("Bài làm ổn, giao diện rõ ràng.", "Cần bổ sung validation form.", "Không phát hiện trùng lặp cao."),
    )
    connection.commit()


def _upgrade_sqlite_password_hashes(connection, table_name):
    rows = connection.execute(f"SELECT id, password FROM {table_name}").fetchall()
    for row in rows:
        stored_password = row["password"]
        if stored_password and not is_password_hashed(stored_password):
            connection.execute(
                f"UPDATE {table_name} SET password = ? WHERE id = ?",
                (hash_password(stored_password), row["id"]),
            )


def get_db_connection():
    db_engine = current_app.config.get("DB_ENGINE", "sqlite").lower()

    if db_engine == "sqlite":
        db_path = Path(current_app.config["SQLITE_PATH"])
        _ensure_parent_dir(db_path)
        return _create_sqlite_connection(db_path)

    if db_engine in {"postgres", "postgresql"}:
        if psycopg is None:
            raise RuntimeError("psycopg is not installed, but DB_ENGINE is set to postgres.")

        database_url = current_app.config.get("DATABASE_URL")
        if not database_url:
            raise RuntimeError("DATABASE_URL is not set, but DB_ENGINE is set to postgres.")

        return psycopg.connect(
            database_url,
            autocommit=True,
            row_factory=dict_row,
        )

    if db_engine != "mysql":
        raise RuntimeError(f"Unsupported DB_ENGINE: {db_engine}")

    if pymysql is None:
        raise RuntimeError("PyMySQL is not installed, but DB_ENGINE is set to mysql.")

    return pymysql.connect(
        host=current_app.config["DB_HOST"],
        port=current_app.config["DB_PORT"],
        user=current_app.config["DB_USER"],
        password=current_app.config["DB_PASSWORD"],
        database=current_app.config["DB_NAME"],
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )
