import sqlite3
from datetime import date, datetime

import pymysql
from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from utils.auth import (
    clear_auth_session,
    redirect_to_role_dashboard,
    set_student_session,
    set_teacher_session,
    student_required,
    teacher_required,
)
from utils.db import get_db_connection, hash_password, is_password_hashed

try:
    import psycopg
except ImportError:  # pragma: no cover - optional dependency when using SQLite/MySQL
    psycopg = None


auth_bp = Blueprint("auth", __name__)
DB_ERRORS = [pymysql.MySQLError, sqlite3.Error]
if psycopg is not None:
    DB_ERRORS.append(psycopg.Error)
DB_ERRORS = tuple(DB_ERRORS)


def _password_matches(stored_password, password):
    if not stored_password or not password:
        return False

    if is_password_hashed(stored_password):
        try:
            return check_password_hash(stored_password, password)
        except ValueError:
            return False

    return stored_password == password


def _upgrade_password_if_needed(conn, table_name, user_id, stored_password, raw_password):
    if is_password_hashed(stored_password) or stored_password != raw_password:
        return

    with conn.cursor() as cursor:
        cursor.execute(
            f"UPDATE {table_name} SET password = %s WHERE id = %s",
            (hash_password(raw_password), user_id),
        )

    if hasattr(conn, "commit"):
        conn.commit()


def _load_teacher_by_identifier(conn, identifier):
    with conn.cursor() as cursor:
        cursor.execute("SELECT * FROM teachers WHERE email = %s", (identifier,))
        return cursor.fetchone()


def _load_student_by_identifier(conn, identifier):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM students
            WHERE email = %s OR student_code = %s
            """,
            (identifier, identifier),
        )
        return cursor.fetchone()


def _resolve_login_target(identifier):
    normalized_identifier = identifier.strip().lower()
    if not normalized_identifier:
        return None

    if normalized_identifier.isdigit():
        return "student"

    if normalized_identifier.endswith("@sis.hust.edu.vn"):
        return "student"

    return "teacher"


@auth_bp.route("/")
def home():
    redirect_response = redirect_to_role_dashboard()
    if redirect_response:
        return redirect_response
    return redirect(url_for("auth.login"))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    redirect_response = redirect_to_role_dashboard()
    if redirect_response:
        return redirect_response

    if request.method == "POST":
        identifier = (request.form.get("identifier") or "").strip()
        password = request.form.get("password")
        target = _resolve_login_target(identifier)

        if not target:
            flash("Vui lòng nhập email hoặc mã sinh viên.", "danger")
            return render_template("auth/login.html")

        try:
            conn = get_db_connection()
            if target == "teacher":
                teacher = _load_teacher_by_identifier(conn, identifier)
                if teacher and _password_matches(teacher["password"], password):
                    _upgrade_password_if_needed(conn, "teachers", teacher["id"], teacher["password"], password)
                    set_teacher_session(teacher)
                    return redirect(url_for("auth.dashboard"))
            else:
                student = _load_student_by_identifier(conn, identifier)
                if student and _password_matches(student["password"], password):
                    _upgrade_password_if_needed(conn, "students", student["id"], student["password"], password)
                    set_student_session(student)
                    return redirect(url_for("auth.student_dashboard"))

            flash("Sai thông tin đăng nhập hoặc mật khẩu!", "danger")
        except DB_ERRORS:
            current_app.logger.exception("Database error while processing login")
            flash(
                "Không thể kết nối cơ sở dữ liệu. Kiểm tra cấu hình trong file .env.",
                "danger",
            )
        finally:
            if "conn" in locals():
                conn.close()

    return render_template("auth/login.html")


@auth_bp.route("/dashboard")
@teacher_required
def dashboard():
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) AS total FROM students")
            total_students = cursor.fetchone()["total"]

            cursor.execute("SELECT COUNT(*) AS total FROM sessions WHERE session_date = %s", (date.today(),))
            today_sessions = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM submissions
                WHERE teacher_score IS NULL AND status = 'submitted'
                """
            )
            pending_assignments = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM (
                    SELECT
                        student_id,
                        SUM(CASE WHEN attendance_status = 'absent' THEN 1 ELSE 0 END) AS absent_count
                    FROM attendance
                    GROUP BY student_id
                    HAVING SUM(CASE WHEN attendance_status = 'absent' THEN 1 ELSE 0 END) >= 1
                ) AS warning_list
                """
            )
            warning_students = cursor.fetchone()["total"]

        stats = {
            "total_students": total_students,
            "today_sessions": today_sessions,
            "pending_assignments": pending_assignments,
            "warning_students": warning_students,
        }
    except DB_ERRORS:
        current_app.logger.exception("Database error while loading dashboard")
        flash("Không thể tải dữ liệu dashboard vì kết nối cơ sở dữ liệu thất bại.", "danger")
        return redirect(url_for("auth.login"))
    finally:
        if "conn" in locals():
            conn.close()

    return render_template(
        "dashboard.html",
        stats=stats,
        user_name=session.get("user_name"),
        active_page="dashboard",
        page_title="Bảng điều khiển giảng viên",
        page_subtitle="Xem nhanh việc trong ngày: lớp học, điểm danh và bài chờ chấm.",
    )


@auth_bp.route("/student/dashboard")
@student_required
def student_dashboard():
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, student_code, full_name, email, phone, class_name
                FROM students
                WHERE id = %s
                """,
                (session["student_id"],),
            )
            student = cursor.fetchone()

            cursor.execute(
                """
                SELECT COUNT(DISTINCT class_id) AS total
                FROM class_students
                WHERE student_id = %s
                """,
                (session["student_id"],),
            )
            total_classes = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM assignments a
                JOIN class_students cs ON cs.class_id = a.class_id
                LEFT JOIN submissions sb
                    ON sb.assignment_id = a.id
                    AND sb.student_id = cs.student_id
                WHERE cs.student_id = %s
                  AND a.due_date >= %s
                  AND (sb.id IS NULL OR sb.status = 'missing')
                """,
                (session["student_id"], datetime.now()),
            )
            pending_assignments = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN attendance_status = 'late' THEN 1 ELSE 0 END), 0) AS late_sessions,
                    COALESCE(SUM(CASE WHEN attendance_status = 'absent' THEN 1 ELSE 0 END), 0) AS absent_sessions
                FROM attendance
                WHERE student_id = %s
                """,
                (session["student_id"],),
            )
            attendance_stats = cursor.fetchone()

            cursor.execute(
                """
                SELECT
                    c.class_code,
                    c.class_name,
                    c.subject_name,
                    c.room,
                    c.schedule_info,
                    t.full_name AS teacher_name
                FROM class_students cs
                JOIN classes c ON c.id = cs.class_id
                JOIN teachers t ON t.id = c.teacher_id
                WHERE cs.student_id = %s
                ORDER BY c.id DESC
                """,
                (session["student_id"],),
            )
            class_items = cursor.fetchall()

            cursor.execute(
                """
                SELECT
                    a.id,
                    c.class_name,
                    a.title,
                    a.description,
                    a.due_date,
                    COALESCE(sb.status, 'missing') AS submission_status,
                    sb.teacher_score,
                    sb.teacher_comment,
                    sb.submitted_at
                FROM assignments a
                JOIN class_students cs ON cs.class_id = a.class_id
                JOIN classes c ON c.id = a.class_id
                LEFT JOIN submissions sb
                    ON sb.assignment_id = a.id
                    AND sb.student_id = cs.student_id
                WHERE cs.student_id = %s
                ORDER BY a.due_date ASC, a.id DESC
                """,
                (session["student_id"],),
            )
            assignment_items = cursor.fetchall()

            cursor.execute(
                """
                SELECT
                    c.class_name,
                    s.session_title,
                    s.session_date,
                    s.start_time,
                    s.end_time,
                    a.attendance_status,
                    a.checkin_time,
                    a.note
                FROM attendance a
                JOIN sessions s ON s.id = a.session_id
                JOIN classes c ON c.id = s.class_id
                WHERE a.student_id = %s
                ORDER BY s.session_date DESC, s.start_time DESC, a.id DESC
                LIMIT 5
                """,
                (session["student_id"],),
            )
            attendance_items = cursor.fetchall()
    except DB_ERRORS:
        current_app.logger.exception("Database error while loading student dashboard")
        flash("Không thể tải dữ liệu dashboard sinh viên.", "danger")
        return redirect(url_for("auth.login"))
    finally:
        if "conn" in locals():
            conn.close()

    if not student:
        clear_auth_session()
        flash("Không tìm thấy tài khoản sinh viên.", "danger")
        return redirect(url_for("auth.login"))

    stats = {
        "total_classes": total_classes,
        "pending_assignments": pending_assignments,
        "late_sessions": attendance_stats["late_sessions"],
        "absent_sessions": attendance_stats["absent_sessions"],
    }

    return render_template(
        "student_dashboard.html",
        student=student,
        stats=stats,
        class_items=class_items,
        assignment_items=assignment_items,
        attendance_items=attendance_items,
        user_name=session.get("user_name"),
        active_page="student_dashboard",
        page_title="Bảng điều khiển sinh viên",
        page_subtitle="Xem lịch học, bài tập và điểm danh của bạn.",
    )


@auth_bp.route("/logout")
def logout():
    clear_auth_session()
    return redirect(url_for("auth.login"))
