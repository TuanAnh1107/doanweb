import sqlite3
from io import BytesIO

import pymysql
from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, session, url_for

from services.excel_service import build_class_students_export
from utils.auth import teacher_required
from utils.db import get_db_connection, hash_password

try:
    import psycopg
except ImportError:  # pragma: no cover - optional dependency when using SQLite/MySQL
    psycopg = None


classes_bp = Blueprint("classes", __name__, url_prefix="/classes")
DB_ERRORS = [pymysql.MySQLError, sqlite3.Error]
if psycopg is not None:
    DB_ERRORS.append(psycopg.Error)
DB_ERRORS = tuple(DB_ERRORS)


def _commit_if_possible(conn):
    if hasattr(conn, "commit"):
        conn.commit()


def _load_teacher_class(cursor, teacher_id, class_id):
    cursor.execute(
        """
        SELECT
            c.id,
            c.class_code,
            c.class_name,
            c.subject_name,
            c.semester,
            c.school_year,
            c.room,
            c.schedule_info,
            t.full_name AS teacher_name,
            COUNT(DISTINCT cs.student_id) AS total_students,
            COUNT(DISTINCT s.id) AS total_sessions,
            COUNT(DISTINCT a.id) AS total_assignments
        FROM classes c
        JOIN teachers t ON t.id = c.teacher_id
        LEFT JOIN class_students cs ON cs.class_id = c.id
        LEFT JOIN sessions s ON s.class_id = c.id
        LEFT JOIN assignments a ON a.class_id = c.id
        WHERE c.teacher_id = %s AND c.id = %s
        GROUP BY
            c.id,
            c.class_code,
            c.class_name,
            c.subject_name,
            c.semester,
            c.school_year,
            c.room,
            c.schedule_info,
            t.full_name
        """,
        (teacher_id, class_id),
    )
    return cursor.fetchone()


def _sync_student_records_for_class(cursor, class_id, student_id):
    # Ensure the student has attendance rows for all sessions in this class.
    cursor.execute(
        """
        INSERT INTO attendance (session_id, student_id, attendance_status)
        SELECT s.id, %s, 'absent'
        FROM sessions s
        WHERE s.class_id = %s
          AND NOT EXISTS (
              SELECT 1
              FROM attendance a
              WHERE a.session_id = s.id AND a.student_id = %s
          )
        """,
        (student_id, class_id, student_id),
    )

    # Ensure the student has submission rows for all assignments in this class.
    cursor.execute(
        """
        INSERT INTO submissions (assignment_id, student_id, status)
        SELECT a.id, %s, 'missing'
        FROM assignments a
        WHERE a.class_id = %s
          AND NOT EXISTS (
              SELECT 1
              FROM submissions sb
              WHERE sb.assignment_id = a.id AND sb.student_id = %s
          )
        """,
        (student_id, class_id, student_id),
    )


def _load_class_students_summary(cursor, class_id):
    cursor.execute(
        """
        SELECT
            st.id,
            st.student_code,
            st.full_name,
            st.email,
            st.phone,
            st.class_name,
            COALESCE(attendance_summary.present_count, 0) AS present_count,
            COALESCE(attendance_summary.late_count, 0) AS late_count,
            COALESCE(attendance_summary.absent_count, 0) AS absent_count,
            COALESCE(submission_summary.submitted_count, 0) AS submitted_count,
            COALESCE(submission_summary.missing_count, 0) AS missing_count
        FROM class_students cs
        JOIN students st ON st.id = cs.student_id
        LEFT JOIN (
            SELECT
                a.student_id,
                SUM(CASE WHEN a.attendance_status = 'present' THEN 1 ELSE 0 END) AS present_count,
                SUM(CASE WHEN a.attendance_status = 'late' THEN 1 ELSE 0 END) AS late_count,
                SUM(CASE WHEN a.attendance_status = 'absent' THEN 1 ELSE 0 END) AS absent_count
            FROM attendance a
            JOIN sessions s ON s.id = a.session_id
            WHERE s.class_id = %s
            GROUP BY a.student_id
        ) attendance_summary ON attendance_summary.student_id = st.id
        LEFT JOIN (
            SELECT
                sb.student_id,
                SUM(CASE WHEN sb.status = 'submitted' THEN 1 ELSE 0 END) AS submitted_count,
                SUM(CASE WHEN sb.status = 'missing' THEN 1 ELSE 0 END) AS missing_count
            FROM submissions sb
            JOIN assignments a ON a.id = sb.assignment_id
            WHERE a.class_id = %s
            GROUP BY sb.student_id
        ) submission_summary ON submission_summary.student_id = st.id
        WHERE cs.class_id = %s
        ORDER BY st.full_name ASC, st.student_code ASC
        """,
        (class_id, class_id, class_id),
    )
    return cursor.fetchall()


def _resolve_inserted_student_id(cursor, student_code):
    insert_id = getattr(cursor, "lastrowid", None)
    if isinstance(insert_id, int) and insert_id > 0:
        return insert_id

    cursor.execute("SELECT id FROM students WHERE student_code = %s", (student_code,))
    student_row = cursor.fetchone()
    return student_row["id"] if student_row else None


@classes_bp.route("/", methods=["GET", "POST"])
@teacher_required
def index():
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            if request.method == "POST" and request.form.get("action") == "create_class":
                class_code = (request.form.get("class_code") or "").strip()
                class_name = (request.form.get("class_name") or "").strip()
                subject_name = (request.form.get("subject_name") or "").strip()
                semester = (request.form.get("semester") or "").strip() or None
                school_year = (request.form.get("school_year") or "").strip() or None
                room = (request.form.get("room") or "").strip() or None
                schedule_info = (request.form.get("schedule_info") or "").strip() or None

                if not class_code or not class_name or not subject_name:
                    flash("Vui lòng nhập đủ mã lớp, tên lớp và môn học.", "warning")
                    return redirect(url_for("classes.index"))

                cursor.execute(
                    """
                    INSERT INTO classes (
                        class_code, class_name, subject_name, teacher_id,
                        semester, school_year, room, schedule_info
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        class_code,
                        class_name,
                        subject_name,
                        session["teacher_id"],
                        semester,
                        school_year,
                        room,
                        schedule_info,
                    ),
                )
                _commit_if_possible(conn)
                flash(f"Đã tạo lớp {class_code} thành công.", "success")
                return redirect(url_for("classes.index"))

            cursor.execute("SELECT COUNT(*) AS total FROM classes WHERE teacher_id = %s", (session["teacher_id"],))
            total_classes = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT COUNT(DISTINCT cs.student_id) AS total
                FROM classes c
                LEFT JOIN class_students cs ON cs.class_id = c.id
                WHERE c.teacher_id = %s
                """,
                (session["teacher_id"],),
            )
            total_students = cursor.fetchone()["total"]

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
                SELECT
                    c.id,
                    c.class_code,
                    c.class_name,
                    c.subject_name,
                    c.semester,
                    c.school_year,
                    c.room,
                    c.schedule_info,
                    COUNT(DISTINCT cs.student_id) AS total_students,
                    COUNT(DISTINCT s.id) AS total_sessions
                FROM classes c
                LEFT JOIN class_students cs ON cs.class_id = c.id
                LEFT JOIN sessions s ON s.class_id = c.id
                WHERE c.teacher_id = %s
                GROUP BY
                    c.id,
                    c.class_code,
                    c.class_name,
                    c.subject_name,
                    c.semester,
                    c.school_year,
                    c.room,
                    c.schedule_info
                ORDER BY c.id DESC
                """,
                (session["teacher_id"],),
            )
            class_items = cursor.fetchall()
    except DB_ERRORS:
        current_app.logger.exception("Database error while loading classes page")
        flash("Không thể tải danh sách lớp học.", "danger")
        return redirect(url_for("auth.dashboard"))
    finally:
        if "conn" in locals():
            conn.close()

    return render_template(
        "classes/index.html",
        user_name=session.get("user_name"),
        active_page="classes",
        page_title="Lớp học",
        page_subtitle="Xem danh sách lớp, sĩ số và vào từng lớp để quản lý sinh viên.",
        stats={
            "total_classes": total_classes,
            "total_students": total_students,
            "total_sessions": total_sessions,
        },
        class_items=class_items,
    )


@classes_bp.route("/<int:class_id>/edit", methods=["GET", "POST"])
@teacher_required
def edit(class_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            class_item = _load_teacher_class(cursor, session["teacher_id"], class_id)
            if not class_item:
                flash("Không tìm thấy lớp học hoặc bạn không có quyền chỉnh sửa lớp này.", "warning")
                return redirect(url_for("classes.index"))

            if request.method == "POST":
                class_code = (request.form.get("class_code") or "").strip()
                class_name = (request.form.get("class_name") or "").strip()
                subject_name = (request.form.get("subject_name") or "").strip()
                semester = (request.form.get("semester") or "").strip() or None
                school_year = (request.form.get("school_year") or "").strip() or None
                room = (request.form.get("room") or "").strip() or None
                schedule_info = (request.form.get("schedule_info") or "").strip() or None

                if not class_code or not class_name or not subject_name:
                    flash("Vui lòng nhập đủ mã lớp, tên lớp và môn học.", "warning")
                    return redirect(url_for("classes.edit", class_id=class_id))

                cursor.execute(
                    """
                    UPDATE classes
                    SET class_code = %s,
                        class_name = %s,
                        subject_name = %s,
                        semester = %s,
                        school_year = %s,
                        room = %s,
                        schedule_info = %s
                    WHERE id = %s AND teacher_id = %s
                    """,
                    (
                        class_code,
                        class_name,
                        subject_name,
                        semester,
                        school_year,
                        room,
                        schedule_info,
                        class_id,
                        session["teacher_id"],
                    ),
                )
                _commit_if_possible(conn)
                flash("Đã cập nhật thông tin lớp học.", "success")
                return redirect(url_for("classes.detail", class_id=class_id))
    except DB_ERRORS:
        current_app.logger.exception("Database error while updating class")
        flash("Không thể cập nhật lớp học.", "danger")
        return redirect(url_for("classes.index"))
    finally:
        if "conn" in locals():
            conn.close()

    return render_template(
        "classes/edit.html",
        user_name=session.get("user_name"),
        active_page="classes",
        page_title=f"Cập nhật lớp {class_item['class_code']}",
        page_subtitle="Sửa thông tin lớp bạn đang phụ trách.",
        class_item=class_item,
    )


@classes_bp.route("/<int:class_id>/delete", methods=["POST"])
@teacher_required
def delete(class_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            class_item = _load_teacher_class(cursor, session["teacher_id"], class_id)
            if not class_item:
                flash("Không tìm thấy lớp học hoặc bạn không có quyền xóa lớp này.", "warning")
                return redirect(url_for("classes.index"))

            cursor.execute("DELETE FROM classes WHERE id = %s AND teacher_id = %s", (class_id, session["teacher_id"]))
            _commit_if_possible(conn)
            flash(f"Đã xóa lớp {class_item['class_code']}.", "success")
    except DB_ERRORS:
        current_app.logger.exception("Database error while deleting class")
        flash("Không thể xóa lớp học.", "danger")
    finally:
        if "conn" in locals():
            conn.close()

    return redirect(url_for("classes.index"))


@classes_bp.route("/<int:class_id>")
@teacher_required
def detail(class_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            class_item = _load_teacher_class(cursor, session["teacher_id"], class_id)
            if not class_item:
                flash("Không tìm thấy lớp học hoặc bạn không có quyền truy cập lớp này.", "warning")
                return redirect(url_for("classes.index"))

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM class_students
                WHERE class_id = %s
                """,
                (class_id,),
            )
            total_students = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM sessions
                WHERE class_id = %s
                """,
                (class_id,),
            )
            total_sessions = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM assignments
                WHERE class_id = %s
                """,
                (class_id,),
            )
            total_assignments = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM class_students cs
                LEFT JOIN (
                    SELECT
                        a.student_id,
                        SUM(CASE WHEN a.attendance_status = 'absent' THEN 1 ELSE 0 END) AS absent_count
                    FROM attendance a
                    JOIN sessions s ON s.id = a.session_id
                    WHERE s.class_id = %s
                    GROUP BY a.student_id
                ) attendance_summary ON attendance_summary.student_id = cs.student_id
                WHERE cs.class_id = %s AND COALESCE(attendance_summary.absent_count, 0) > 0
                """,
                (class_id, class_id),
            )
            warning_students = cursor.fetchone()["total"]

            student_items = _load_class_students_summary(cursor, class_id)
    except DB_ERRORS:
        current_app.logger.exception("Database error while loading class detail page")
        flash("Không thể tải danh sách sinh viên của lớp này.", "danger")
        return redirect(url_for("classes.index"))
    finally:
        if "conn" in locals():
            conn.close()

    return render_template(
        "classes/detail.html",
        user_name=session.get("user_name"),
        active_page="classes",
        page_title=f"Lớp {class_item['class_code']}",
        page_subtitle="Danh sách sinh viên, liên hệ và tình hình học tập của lớp.",
        class_item=class_item,
        stats={
            "total_students": total_students,
            "total_sessions": total_sessions,
            "total_assignments": total_assignments,
            "warning_students": warning_students,
        },
        student_items=student_items,
    )


@classes_bp.route("/<int:class_id>/export")
@teacher_required
def export_class_students(class_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            class_item = _load_teacher_class(cursor, session["teacher_id"], class_id)
            if not class_item:
                flash("Không tìm thấy lớp học hoặc bạn không có quyền truy cập lớp này.", "warning")
                return redirect(url_for("classes.index"))

            student_items = _load_class_students_summary(cursor, class_id)
            workbook_bytes = build_class_students_export(class_item, student_items)
    except DB_ERRORS:
        current_app.logger.exception("Database error while exporting class students")
        flash("Không thể xuất danh sách sinh viên của lớp này.", "danger")
        return redirect(url_for("classes.detail", class_id=class_id))
    finally:
        if "conn" in locals():
            conn.close()

    file_name = f"lop_{class_item['class_code']}_sinh_vien.xlsx"
    return send_file(
        BytesIO(workbook_bytes),
        as_attachment=True,
        download_name=file_name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@classes_bp.route("/<int:class_id>/students/add-existing", methods=["POST"])
@teacher_required
def add_existing_student(class_id):
    identifier = (request.form.get("student_identifier") or "").strip()
    if not identifier:
        flash("Vui lòng nhập mã sinh viên hoặc email để thêm vào lớp.", "warning")
        return redirect(url_for("classes.detail", class_id=class_id))

    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            class_item = _load_teacher_class(cursor, session["teacher_id"], class_id)
            if not class_item:
                flash("Không tìm thấy lớp học hoặc bạn không có quyền cập nhật lớp này.", "warning")
                return redirect(url_for("classes.index"))

            cursor.execute(
                """
                SELECT id, full_name, student_code
                FROM students
                WHERE student_code = %s OR email = %s
                """,
                (identifier, identifier),
            )
            student = cursor.fetchone()
            if not student:
                flash("Không tìm thấy sinh viên với mã/email đã nhập.", "warning")
                return redirect(url_for("classes.detail", class_id=class_id))

            cursor.execute(
                """
                INSERT INTO class_students (class_id, student_id)
                VALUES (%s, %s)
                """,
                (class_id, student["id"]),
            )
            _sync_student_records_for_class(cursor, class_id, student["id"])
            _commit_if_possible(conn)

            flash(
                f"Đã thêm sinh viên {student['full_name']} ({student['student_code']}) vào lớp.",
                "success",
            )
    except DB_ERRORS:
        current_app.logger.exception("Database error while adding existing student to class")
        flash("Không thể thêm sinh viên vào lớp (có thể sinh viên đã thuộc lớp).", "danger")
    finally:
        if "conn" in locals():
            conn.close()

    return redirect(url_for("classes.detail", class_id=class_id))


@classes_bp.route("/<int:class_id>/students/create", methods=["POST"])
@teacher_required
def create_student_and_add(class_id):
    student_code = (request.form.get("student_code") or "").strip()
    full_name = (request.form.get("full_name") or "").strip()
    email = (request.form.get("email") or "").strip() or None
    phone = (request.form.get("phone") or "").strip() or None
    class_name = (request.form.get("class_name") or "").strip() or None
    password = (request.form.get("password") or "").strip() or "1"

    if not student_code or not full_name:
        flash("Vui lòng nhập mã sinh viên và họ tên.", "warning")
        return redirect(url_for("classes.detail", class_id=class_id))

    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            class_item = _load_teacher_class(cursor, session["teacher_id"], class_id)
            if not class_item:
                flash("Không tìm thấy lớp học hoặc bạn không có quyền cập nhật lớp này.", "warning")
                return redirect(url_for("classes.index"))

            cursor.execute(
                """
                INSERT INTO students (student_code, full_name, email, password, phone, class_name)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (student_code, full_name, email, hash_password(password), phone, class_name),
            )

            student_id = _resolve_inserted_student_id(cursor, student_code)
            if not student_id:
                flash("Không thể tạo sinh viên mới.", "danger")
                return redirect(url_for("classes.detail", class_id=class_id))

            cursor.execute(
                """
                INSERT INTO class_students (class_id, student_id)
                VALUES (%s, %s)
                """,
                (class_id, student_id),
            )
            _sync_student_records_for_class(cursor, class_id, student_id)
            _commit_if_possible(conn)

            flash(f"Đã tạo và thêm sinh viên {full_name} vào lớp.", "success")
    except DB_ERRORS:
        current_app.logger.exception("Database error while creating student and adding to class")
        flash("Không thể tạo sinh viên mới (mã sinh viên hoặc email có thể đã tồn tại).", "danger")
    finally:
        if "conn" in locals():
            conn.close()

    return redirect(url_for("classes.detail", class_id=class_id))


@classes_bp.route("/<int:class_id>/students/<int:student_id>/remove", methods=["POST"])
@teacher_required
def remove_student_from_class(class_id, student_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            class_item = _load_teacher_class(cursor, session["teacher_id"], class_id)
            if not class_item:
                flash("Không tìm thấy lớp học hoặc bạn không có quyền cập nhật lớp này.", "warning")
                return redirect(url_for("classes.index"))

            cursor.execute(
                """
                SELECT st.full_name
                FROM class_students cs
                JOIN students st ON st.id = cs.student_id
                WHERE cs.class_id = %s AND cs.student_id = %s
                """,
                (class_id, student_id),
            )
            enrolled = cursor.fetchone()
            if not enrolled:
                flash("Sinh viên không thuộc lớp này.", "warning")
                return redirect(url_for("classes.detail", class_id=class_id))

            cursor.execute(
                """
                DELETE FROM attendance
                WHERE student_id = %s
                  AND session_id IN (SELECT id FROM sessions WHERE class_id = %s)
                """,
                (student_id, class_id),
            )
            cursor.execute(
                """
                DELETE FROM submissions
                WHERE student_id = %s
                  AND assignment_id IN (SELECT id FROM assignments WHERE class_id = %s)
                """,
                (student_id, class_id),
            )
            cursor.execute(
                """
                DELETE FROM class_students
                WHERE class_id = %s AND student_id = %s
                """,
                (class_id, student_id),
            )
            _commit_if_possible(conn)
            flash(f"Đã xóa sinh viên {enrolled['full_name']} khỏi lớp.", "success")
    except DB_ERRORS:
        current_app.logger.exception("Database error while removing student from class")
        flash("Không thể xóa sinh viên khỏi lớp.", "danger")
    finally:
        if "conn" in locals():
            conn.close()

    return redirect(url_for("classes.detail", class_id=class_id))


@classes_bp.route("/<int:class_id>/students/<int:student_id>")
@teacher_required
def student_detail(class_id, student_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            class_item = _load_teacher_class(cursor, session["teacher_id"], class_id)
            if not class_item:
                flash("Không tìm thấy lớp học hoặc bạn không có quyền truy cập lớp này.", "warning")
                return redirect(url_for("classes.index"))

            cursor.execute(
                """
                SELECT
                    st.id,
                    st.student_code,
                    st.full_name,
                    st.email,
                    st.phone,
                    st.class_name,
                    cs.joined_at,
                    COALESCE(attendance_summary.present_count, 0) AS present_count,
                    COALESCE(attendance_summary.late_count, 0) AS late_count,
                    COALESCE(attendance_summary.absent_count, 0) AS absent_count,
                    COALESCE(submission_summary.submitted_count, 0) AS submitted_count,
                    COALESCE(submission_summary.missing_count, 0) AS missing_count,
                    submission_summary.avg_score
                FROM class_students cs
                JOIN students st ON st.id = cs.student_id
                LEFT JOIN (
                    SELECT
                        a.student_id,
                        SUM(CASE WHEN a.attendance_status = 'present' THEN 1 ELSE 0 END) AS present_count,
                        SUM(CASE WHEN a.attendance_status = 'late' THEN 1 ELSE 0 END) AS late_count,
                        SUM(CASE WHEN a.attendance_status = 'absent' THEN 1 ELSE 0 END) AS absent_count
                    FROM attendance a
                    JOIN sessions s ON s.id = a.session_id
                    WHERE s.class_id = %s
                    GROUP BY a.student_id
                ) attendance_summary ON attendance_summary.student_id = st.id
                LEFT JOIN (
                    SELECT
                        sb.student_id,
                        SUM(CASE WHEN sb.status = 'submitted' THEN 1 ELSE 0 END) AS submitted_count,
                        SUM(CASE WHEN sb.status = 'missing' THEN 1 ELSE 0 END) AS missing_count,
                        AVG(sb.teacher_score) AS avg_score
                    FROM submissions sb
                    JOIN assignments a ON a.id = sb.assignment_id
                    WHERE a.class_id = %s
                    GROUP BY sb.student_id
                ) submission_summary ON submission_summary.student_id = st.id
                WHERE cs.class_id = %s AND st.id = %s
                """,
                (class_id, class_id, class_id, student_id),
            )
            student_item = cursor.fetchone()
            if not student_item:
                flash("Không tìm thấy sinh viên trong lớp này.", "warning")
                return redirect(url_for("classes.detail", class_id=class_id))

            cursor.execute(
                """
                SELECT
                    s.id,
                    s.session_title,
                    s.session_date,
                    s.start_time,
                    s.end_time,
                    COALESCE(a.attendance_status, 'unrecorded') AS attendance_status,
                    a.checkin_time,
                    a.note
                FROM sessions s
                LEFT JOIN attendance a
                    ON a.session_id = s.id
                    AND a.student_id = %s
                WHERE s.class_id = %s
                ORDER BY s.session_date DESC, s.start_time DESC, s.id DESC
                """,
                (student_id, class_id),
            )
            attendance_items = cursor.fetchall()

            cursor.execute(
                """
                SELECT
                    a.id,
                    a.title,
                    a.description,
                    a.due_date,
                    COALESCE(sb.status, 'missing') AS submission_status,
                    sb.teacher_score,
                    sb.teacher_comment,
                    sb.submitted_at
                FROM assignments a
                LEFT JOIN submissions sb
                    ON sb.assignment_id = a.id
                    AND sb.student_id = %s
                WHERE a.class_id = %s
                ORDER BY a.due_date ASC, a.id DESC
                """,
                (student_id, class_id),
            )
            assignment_items = cursor.fetchall()
    except DB_ERRORS:
        current_app.logger.exception("Database error while loading class student detail page")
        flash("Không thể tải hồ sơ sinh viên trong lớp này.", "danger")
        return redirect(url_for("classes.detail", class_id=class_id))
    finally:
        if "conn" in locals():
            conn.close()

    stats = {
        "present_count": student_item["present_count"],
        "late_count": student_item["late_count"],
        "absent_count": student_item["absent_count"],
        "submitted_count": student_item["submitted_count"],
        "missing_count": student_item["missing_count"],
    }

    return render_template(
        "classes/student_detail.html",
        user_name=session.get("user_name"),
        active_page="classes",
        page_title=student_item["full_name"],
        page_subtitle=f"Thông tin và kết quả học tập của sinh viên trong lớp {class_item['class_code']}.",
        class_item=class_item,
        student_item=student_item,
        stats=stats,
        attendance_items=attendance_items,
        assignment_items=assignment_items,
    )
