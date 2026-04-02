import sqlite3
from datetime import datetime, timedelta
from io import BytesIO
from uuid import uuid4

import pymysql
from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, session, url_for

from services.excel_service import build_attendance_session_export
from utils.auth import teacher_required
from utils.db import get_db_connection

try:
    import psycopg
except ImportError:  # pragma: no cover - optional dependency when using SQLite/MySQL
    psycopg = None


attendance_bp = Blueprint("attendance", __name__, url_prefix="/attendance")
DB_ERRORS = [pymysql.MySQLError, sqlite3.Error]
if psycopg is not None:
    DB_ERRORS.append(psycopg.Error)
DB_ERRORS = tuple(DB_ERRORS)


def _commit_if_possible(conn):
    if hasattr(conn, "commit"):
        conn.commit()


def _parse_date(date_raw):
    try:
        return datetime.strptime(date_raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_time(time_raw):
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(time_raw, fmt).time()
        except ValueError:
            continue
    return None


def _teacher_classes(cursor, teacher_id):
    cursor.execute(
        """
        SELECT id, class_code, class_name
        FROM classes
        WHERE teacher_id = %s
        ORDER BY class_code ASC, id ASC
        """,
        (teacher_id,),
    )
    return cursor.fetchall()


def _load_teacher_session(cursor, teacher_id, session_id):
    cursor.execute(
        """
        SELECT
            s.id,
            s.class_id,
            s.session_title,
            s.session_date,
            s.start_time,
            s.end_time,
            s.qr_token,
            s.qr_expired_at,
            s.status,
            c.class_code,
            c.class_name
        FROM sessions s
        JOIN classes c ON c.id = s.class_id
        WHERE s.id = %s AND c.teacher_id = %s
        """,
        (session_id, teacher_id),
    )
    return cursor.fetchone()


def _seed_attendance_rows(cursor, class_id, session_id):
    cursor.execute(
        """
        INSERT INTO attendance (session_id, student_id, attendance_status)
        SELECT %s, cs.student_id, 'absent'
        FROM class_students cs
        WHERE cs.class_id = %s
          AND NOT EXISTS (
              SELECT 1
              FROM attendance a
              WHERE a.session_id = %s
                AND a.student_id = cs.student_id
          )
        """,
        (session_id, class_id, session_id),
    )


def _load_session_attendance_items(cursor, session_id, class_id):
    cursor.execute(
        """
        SELECT
            st.id AS student_id,
            st.student_code,
            st.full_name,
            st.email,
            a.attendance_status,
            a.checkin_time,
            a.note
        FROM class_students cs
        JOIN students st ON st.id = cs.student_id
        LEFT JOIN attendance a
            ON a.session_id = %s
            AND a.student_id = st.id
        WHERE cs.class_id = %s
        ORDER BY st.full_name ASC, st.student_code ASC
        """,
        (session_id, class_id),
    )
    return cursor.fetchall()


@attendance_bp.route("/", methods=["GET", "POST"])
@teacher_required
def index():
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            if request.method == "POST" and request.form.get("action") == "create_session":
                class_id_raw = (request.form.get("class_id") or "").strip()
                session_title = (request.form.get("session_title") or "").strip() or None
                session_date_raw = (request.form.get("session_date") or "").strip()
                start_time_raw = (request.form.get("start_time") or "").strip()
                end_time_raw = (request.form.get("end_time") or "").strip()

                if not class_id_raw or not session_date_raw or not start_time_raw or not end_time_raw:
                    flash("Vui lòng nhập đủ lớp học, ngày học, giờ bắt đầu và giờ kết thúc.", "warning")
                    return redirect(url_for("attendance.index"))

                try:
                    class_id = int(class_id_raw)
                except ValueError:
                    flash("Lớp học không hợp lệ.", "warning")
                    return redirect(url_for("attendance.index"))

                session_date = _parse_date(session_date_raw)
                start_time = _parse_time(start_time_raw)
                end_time = _parse_time(end_time_raw)
                if not session_date or not start_time or not end_time:
                    flash("Ngày hoặc giờ học không đúng định dạng.", "warning")
                    return redirect(url_for("attendance.index"))
                if end_time <= start_time:
                    flash("Giờ kết thúc phải sau giờ bắt đầu.", "warning")
                    return redirect(url_for("attendance.index"))

                cursor.execute(
                    """
                    SELECT id, class_code, class_name
                    FROM classes
                    WHERE id = %s AND teacher_id = %s
                    """,
                    (class_id, session["teacher_id"]),
                )
                class_item = cursor.fetchone()
                if not class_item:
                    flash("Không tìm thấy lớp học hoặc bạn không có quyền tạo buổi cho lớp này.", "warning")
                    return redirect(url_for("attendance.index"))

                if not session_title:
                    session_title = f"Buổi học {session_date.strftime('%d/%m/%Y')}"

                qr_token = f"SESSION-{uuid4().hex}"
                qr_expired_at = datetime.now() + timedelta(minutes=15)
                cursor.execute(
                    """
                    INSERT INTO sessions (
                        class_id, session_title, session_date, start_time, end_time,
                        qr_token, qr_expired_at, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'open')
                    """,
                    (
                        class_id,
                        session_title,
                        session_date.isoformat(),
                        start_time.strftime("%H:%M:%S"),
                        end_time.strftime("%H:%M:%S"),
                        qr_token,
                        qr_expired_at,
                    ),
                )
                cursor.execute(
                    """
                    SELECT id
                    FROM sessions
                    WHERE qr_token = %s
                    """,
                    (qr_token,),
                )
                created_session = cursor.fetchone()
                if created_session:
                    _seed_attendance_rows(cursor, class_id, created_session["id"])
                _commit_if_possible(conn)

                flash(
                    f"Đã tạo buổi học mới cho lớp {class_item['class_code']} - {class_item['class_name']}.",
                    "success",
                )
                return redirect(url_for("attendance.index"))

            class_items = _teacher_classes(cursor, session["teacher_id"])

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM sessions s
                JOIN classes c ON c.id = s.class_id
                WHERE c.teacher_id = %s
                """,
                (session["teacher_id"],),
            )
            total_sessions = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM sessions s
                JOIN classes c ON c.id = s.class_id
                WHERE c.teacher_id = %s AND s.status = 'open'
                """,
                (session["teacher_id"],),
            )
            open_sessions = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM attendance a
                JOIN sessions s ON s.id = a.session_id
                JOIN classes c ON c.id = s.class_id
                WHERE c.teacher_id = %s AND a.attendance_status IN ('present', 'late')
                """,
                (session["teacher_id"],),
            )
            checked_students = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT
                    s.id,
                    s.class_id,
                    c.class_name,
                    c.class_code,
                    s.session_title,
                    s.session_date,
                    s.start_time,
                    s.end_time,
                    s.status,
                    COALESCE(SUM(CASE WHEN a.attendance_status = 'present' THEN 1 ELSE 0 END), 0) AS present_count,
                    COALESCE(SUM(CASE WHEN a.attendance_status = 'late' THEN 1 ELSE 0 END), 0) AS late_count,
                    COALESCE(SUM(CASE WHEN a.attendance_status = 'absent' THEN 1 ELSE 0 END), 0) AS absent_count
                FROM sessions s
                JOIN classes c ON c.id = s.class_id
                LEFT JOIN attendance a ON a.session_id = s.id
                WHERE c.teacher_id = %s
                GROUP BY
                    s.id,
                    s.class_id,
                    c.class_name,
                    c.class_code,
                    s.session_title,
                    s.session_date,
                    s.start_time,
                    s.end_time,
                    s.status
                ORDER BY s.session_date DESC, s.start_time DESC, s.id DESC
                """,
                (session["teacher_id"],),
            )
            session_items = cursor.fetchall()
    except DB_ERRORS:
        current_app.logger.exception("Database error while loading attendance page")
        flash("Không thể tải dữ liệu điểm danh.", "danger")
        return redirect(url_for("auth.dashboard"))
    finally:
        if "conn" in locals():
            conn.close()

    return render_template(
        "attendance/index.html",
        user_name=session.get("user_name"),
        active_page="attendance",
        page_title="Điểm danh",
        page_subtitle="Tạo buổi học và cập nhật điểm danh theo từng sinh viên.",
        stats={
            "total_sessions": total_sessions,
            "open_sessions": open_sessions,
            "checked_students": checked_students,
        },
        class_items=class_items,
        session_items=session_items,
    )


@attendance_bp.route("/<int:session_id>")
@teacher_required
def detail(session_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            session_item = _load_teacher_session(cursor, session["teacher_id"], session_id)
            if not session_item:
                flash("Không tìm thấy buổi học hoặc bạn không có quyền truy cập.", "warning")
                return redirect(url_for("attendance.index"))

            _seed_attendance_rows(cursor, session_item["class_id"], session_id)
            _commit_if_possible(conn)

            attendance_items = _load_session_attendance_items(cursor, session_id, session_item["class_id"])

            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN attendance_status = 'present' THEN 1 ELSE 0 END), 0) AS present_count,
                    COALESCE(SUM(CASE WHEN attendance_status = 'late' THEN 1 ELSE 0 END), 0) AS late_count,
                    COALESCE(SUM(CASE WHEN attendance_status = 'absent' THEN 1 ELSE 0 END), 0) AS absent_count
                FROM attendance
                WHERE session_id = %s
                """,
                (session_id,),
            )
            stats = cursor.fetchone()
    except DB_ERRORS:
        current_app.logger.exception("Database error while loading attendance detail page")
        flash("Không thể tải chi tiết buổi học.", "danger")
        return redirect(url_for("attendance.index"))
    finally:
        if "conn" in locals():
            conn.close()

    return render_template(
        "attendance/detail.html",
        user_name=session.get("user_name"),
        active_page="attendance",
        page_title=session_item["session_title"] or f"Buổi học #{session_item['id']}",
        page_subtitle=f"Lớp {session_item['class_code']} - {session_item['class_name']}",
        session_item=session_item,
        attendance_items=attendance_items,
        stats=stats,
    )


@attendance_bp.route("/<int:session_id>/export")
@teacher_required
def export_session_attendance(session_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            session_item = _load_teacher_session(cursor, session["teacher_id"], session_id)
            if not session_item:
                flash("Không tìm thấy buổi học hoặc bạn không có quyền truy cập.", "warning")
                return redirect(url_for("attendance.index"))

            attendance_items = _load_session_attendance_items(cursor, session_id, session_item["class_id"])
            workbook_bytes = build_attendance_session_export(session_item, attendance_items)
    except DB_ERRORS:
        current_app.logger.exception("Database error while exporting attendance")
        flash("Không thể xuất danh sách điểm danh của buổi học này.", "danger")
        return redirect(url_for("attendance.detail", session_id=session_id))
    finally:
        if "conn" in locals():
            conn.close()

    file_name = f"diem_danh_buoi_{session_id}.xlsx"
    return send_file(
        BytesIO(workbook_bytes),
        as_attachment=True,
        download_name=file_name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@attendance_bp.route("/<int:session_id>/students/<int:student_id>/update", methods=["POST"])
@teacher_required
def update_student_attendance(session_id, student_id):
    attendance_status = (request.form.get("attendance_status") or "").strip().lower()
    note = (request.form.get("note") or "").strip() or None
    if attendance_status not in {"present", "late", "absent"}:
        flash("Trạng thái điểm danh không hợp lệ.", "warning")
        return redirect(url_for("attendance.detail", session_id=session_id))

    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            session_item = _load_teacher_session(cursor, session["teacher_id"], session_id)
            if not session_item:
                flash("Không tìm thấy buổi học hoặc bạn không có quyền cập nhật.", "warning")
                return redirect(url_for("attendance.index"))
            if session_item["status"] != "open":
                flash("Buổi học đã đóng, không thể chỉnh sửa điểm danh.", "warning")
                return redirect(url_for("attendance.detail", session_id=session_id))

            cursor.execute(
                """
                SELECT 1
                FROM class_students
                WHERE class_id = %s AND student_id = %s
                """,
                (session_item["class_id"], student_id),
            )
            enrolled = cursor.fetchone()
            if not enrolled:
                flash("Sinh viên không thuộc lớp của buổi học này.", "warning")
                return redirect(url_for("attendance.detail", session_id=session_id))

            cursor.execute(
                """
                SELECT id, checkin_time
                FROM attendance
                WHERE session_id = %s
                  AND student_id = %s
                """,
                (session_id, student_id),
            )
            attendance_row = cursor.fetchone()

            if attendance_status in {"present", "late"}:
                existing_checkin = attendance_row["checkin_time"] if attendance_row else None
                checkin_time = existing_checkin or datetime.now()
            else:
                checkin_time = None

            if attendance_row:
                cursor.execute(
                    """
                    UPDATE attendance
                    SET attendance_status = %s,
                        checkin_time = %s,
                        note = %s
                    WHERE session_id = %s
                      AND student_id = %s
                    """,
                    (attendance_status, checkin_time, note, session_id, student_id),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO attendance (session_id, student_id, checkin_time, attendance_status, note)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (session_id, student_id, checkin_time, attendance_status, note),
                )
            _commit_if_possible(conn)

            flash("Đã cập nhật điểm danh cho sinh viên.", "success")
    except DB_ERRORS:
        current_app.logger.exception("Database error while updating student attendance")
        flash("Không thể cập nhật điểm danh.", "danger")
    finally:
        if "conn" in locals():
            conn.close()

    return redirect(url_for("attendance.detail", session_id=session_id))


@attendance_bp.route("/<int:session_id>/status", methods=["POST"])
@teacher_required
def change_session_status(session_id):
    new_status = (request.form.get("status") or "").strip().lower()
    if new_status not in {"open", "closed"}:
        flash("Trạng thái buổi học không hợp lệ.", "warning")
        return redirect(url_for("attendance.detail", session_id=session_id))

    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            session_item = _load_teacher_session(cursor, session["teacher_id"], session_id)
            if not session_item:
                flash("Không tìm thấy buổi học hoặc bạn không có quyền cập nhật.", "warning")
                return redirect(url_for("attendance.index"))

            cursor.execute(
                """
                UPDATE sessions
                SET status = %s
                WHERE id = %s
                """,
                (new_status, session_id),
            )
            _commit_if_possible(conn)
            flash("Đã cập nhật trạng thái buổi học.", "success")
    except DB_ERRORS:
        current_app.logger.exception("Database error while changing session status")
        flash("Không thể cập nhật trạng thái buổi học.", "danger")
    finally:
        if "conn" in locals():
            conn.close()

    return redirect(url_for("attendance.detail", session_id=session_id))


@attendance_bp.route("/<int:session_id>/delete", methods=["POST"])
@teacher_required
def delete_session(session_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            session_item = _load_teacher_session(cursor, session["teacher_id"], session_id)
            if not session_item:
                flash("Không tìm thấy buổi học hoặc bạn không có quyền xóa.", "warning")
                return redirect(url_for("attendance.index"))

            cursor.execute("DELETE FROM sessions WHERE id = %s", (session_id,))
            _commit_if_possible(conn)
            flash("Đã xóa buổi học.", "success")
    except DB_ERRORS:
        current_app.logger.exception("Database error while deleting session")
        flash("Không thể xóa buổi học.", "danger")
    finally:
        if "conn" in locals():
            conn.close()

    return redirect(url_for("attendance.index"))
