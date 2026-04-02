import sqlite3
from datetime import datetime
from io import BytesIO

import pymysql
from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, session, url_for

from services.excel_service import build_assignment_scores_export
from utils.auth import teacher_required
from utils.db import get_db_connection

try:
    import psycopg
except ImportError:  # pragma: no cover - optional dependency when using SQLite/MySQL
    psycopg = None


assignments_bp = Blueprint("assignments", __name__, url_prefix="/assignments")
DB_ERRORS = [pymysql.MySQLError, sqlite3.Error]
if psycopg is not None:
    DB_ERRORS.append(psycopg.Error)
DB_ERRORS = tuple(DB_ERRORS)


def _commit_if_possible(conn):
    if hasattr(conn, "commit"):
        conn.commit()


def _parse_due_date(due_date_raw):
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(due_date_raw, fmt)
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
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


def _load_teacher_assignment(cursor, teacher_id, assignment_id):
    cursor.execute(
        """
        SELECT
            a.id,
            a.class_id,
            a.title,
            a.description,
            a.due_date,
            a.attachment_path,
            c.class_code,
            c.class_name
        FROM assignments a
        JOIN classes c ON c.id = a.class_id
        WHERE a.id = %s AND c.teacher_id = %s
        """,
        (assignment_id, teacher_id),
    )
    return cursor.fetchone()


def _seed_submission_rows(cursor, class_id, assignment_id):
    cursor.execute(
        """
        INSERT INTO submissions (assignment_id, student_id, status)
        SELECT %s, cs.student_id, 'missing'
        FROM class_students cs
        WHERE cs.class_id = %s
          AND NOT EXISTS (
              SELECT 1
              FROM submissions sb
              WHERE sb.assignment_id = %s
                AND sb.student_id = cs.student_id
          )
        """,
        (assignment_id, class_id, assignment_id),
    )


def _resolve_inserted_assignment_id(cursor, class_id):
    insert_id = getattr(cursor, "lastrowid", None)
    if isinstance(insert_id, int) and insert_id > 0:
        return insert_id

    cursor.execute(
        """
        SELECT id
        FROM assignments
        WHERE class_id = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (class_id,),
    )
    assignment_row = cursor.fetchone()
    return assignment_row["id"] if assignment_row else None


def _load_assignment_submissions(cursor, assignment_id, class_id):
    cursor.execute(
        """
        SELECT
            st.id AS student_id,
            st.student_code,
            st.full_name,
            st.email,
            sb.status,
            sb.submitted_at,
            sb.teacher_score,
            sb.teacher_comment,
            sb.submission_text
        FROM class_students cs
        JOIN students st ON st.id = cs.student_id
        LEFT JOIN submissions sb
            ON sb.assignment_id = %s
            AND sb.student_id = st.id
        WHERE cs.class_id = %s
        ORDER BY st.full_name ASC, st.student_code ASC
        """,
        (assignment_id, class_id),
    )
    return cursor.fetchall()


@assignments_bp.route("/", methods=["GET", "POST"])
@teacher_required
def index():
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            if request.method == "POST" and request.form.get("action") == "create_assignment":
                class_id_raw = (request.form.get("class_id") or "").strip()
                title = (request.form.get("title") or "").strip()
                description = (request.form.get("description") or "").strip() or None
                due_date_raw = (request.form.get("due_date") or "").strip()

                if not class_id_raw or not title or not due_date_raw:
                    flash("Vui lòng nhập đủ lớp học, tiêu đề bài tập và hạn nộp.", "warning")
                    return redirect(url_for("assignments.index"))

                try:
                    class_id = int(class_id_raw)
                except ValueError:
                    flash("Lớp học không hợp lệ.", "warning")
                    return redirect(url_for("assignments.index"))

                due_date = _parse_due_date(due_date_raw)
                if not due_date:
                    flash("Hạn nộp không đúng định dạng.", "warning")
                    return redirect(url_for("assignments.index"))

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
                    flash("Không tìm thấy lớp học hoặc bạn không có quyền tạo bài tập cho lớp này.", "warning")
                    return redirect(url_for("assignments.index"))

                cursor.execute(
                    """
                    INSERT INTO assignments (class_id, title, description, due_date)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (class_id, title, description, due_date),
                )
                assignment_id = _resolve_inserted_assignment_id(cursor, class_id)
                if assignment_id:
                    _seed_submission_rows(cursor, class_id, assignment_id)
                _commit_if_possible(conn)

                flash(
                    f"Đã tạo bài tập mới cho lớp {class_item['class_code']} - {class_item['class_name']}.",
                    "success",
                )
                return redirect(url_for("assignments.index"))

            class_items = _teacher_classes(cursor, session["teacher_id"])

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM assignments a
                JOIN classes c ON c.id = a.class_id
                WHERE c.teacher_id = %s
                """,
                (session["teacher_id"],),
            )
            total_assignments = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM submissions sb
                JOIN assignments a ON a.id = sb.assignment_id
                JOIN classes c ON c.id = a.class_id
                WHERE c.teacher_id = %s AND sb.status = 'submitted' AND sb.teacher_score IS NULL
                """,
                (session["teacher_id"],),
            )
            pending_grading = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM submissions sb
                JOIN assignments a ON a.id = sb.assignment_id
                JOIN classes c ON c.id = a.class_id
                WHERE c.teacher_id = %s AND sb.status = 'missing'
                """,
                (session["teacher_id"],),
            )
            missing_submissions = cursor.fetchone()["total"]

            cursor.execute(
                """
                SELECT
                    a.id,
                    c.id AS class_id,
                    c.class_code,
                    c.class_name,
                    a.title,
                    a.description,
                    a.due_date,
                    COUNT(sb.id) AS total_submissions,
                    COALESCE(SUM(CASE WHEN sb.status = 'submitted' THEN 1 ELSE 0 END), 0) AS submitted_count,
                    COALESCE(SUM(CASE WHEN sb.status = 'late' THEN 1 ELSE 0 END), 0) AS late_count,
                    COALESCE(SUM(CASE WHEN sb.status = 'missing' THEN 1 ELSE 0 END), 0) AS missing_count,
                    COALESCE(SUM(CASE WHEN sb.status IN ('submitted', 'late') AND sb.teacher_score IS NULL THEN 1 ELSE 0 END), 0) AS pending_count
                FROM assignments a
                JOIN classes c ON c.id = a.class_id
                LEFT JOIN submissions sb ON sb.assignment_id = a.id
                WHERE c.teacher_id = %s
                GROUP BY
                    a.id,
                    c.id,
                    c.class_code,
                    c.class_name,
                    a.title,
                    a.description,
                    a.due_date
                ORDER BY a.due_date DESC, a.id DESC
                """,
                (session["teacher_id"],),
            )
            assignment_items = cursor.fetchall()
    except DB_ERRORS:
        current_app.logger.exception("Database error while loading assignments page")
        flash("Không thể tải dữ liệu bài tập.", "danger")
        return redirect(url_for("auth.dashboard"))
    finally:
        if "conn" in locals():
            conn.close()

    return render_template(
        "assignments/index.html",
        user_name=session.get("user_name"),
        active_page="assignments",
        page_title="Bài tập",
        page_subtitle="Tạo bài, theo dõi hạn nộp và chấm theo từng sinh viên.",
        stats={
            "total_assignments": total_assignments,
            "pending_grading": pending_grading,
            "missing_submissions": missing_submissions,
        },
        class_items=class_items,
        assignment_items=assignment_items,
    )


@assignments_bp.route("/<int:assignment_id>")
@teacher_required
def detail(assignment_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            assignment_item = _load_teacher_assignment(cursor, session["teacher_id"], assignment_id)
            if not assignment_item:
                flash("Không tìm thấy bài tập hoặc bạn không có quyền truy cập.", "warning")
                return redirect(url_for("assignments.index"))

            _seed_submission_rows(cursor, assignment_item["class_id"], assignment_id)
            _commit_if_possible(conn)

            submission_items = _load_assignment_submissions(cursor, assignment_id, assignment_item["class_id"])

            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN status = 'submitted' THEN 1 ELSE 0 END), 0) AS submitted_count,
                    COALESCE(SUM(CASE WHEN status = 'late' THEN 1 ELSE 0 END), 0) AS late_count,
                    COALESCE(SUM(CASE WHEN status = 'missing' THEN 1 ELSE 0 END), 0) AS missing_count,
                    AVG(teacher_score) AS average_score
                FROM submissions
                WHERE assignment_id = %s
                """,
                (assignment_id,),
            )
            stats = cursor.fetchone()
    except DB_ERRORS:
        current_app.logger.exception("Database error while loading assignment detail page")
        flash("Không thể tải chi tiết bài tập.", "danger")
        return redirect(url_for("assignments.index"))
    finally:
        if "conn" in locals():
            conn.close()

    return render_template(
        "assignments/detail.html",
        user_name=session.get("user_name"),
        active_page="assignments",
        page_title=assignment_item["title"],
        page_subtitle=f"Lớp {assignment_item['class_code']} - {assignment_item['class_name']}",
        assignment_item=assignment_item,
        submission_items=submission_items,
        stats=stats,
    )


@assignments_bp.route("/<int:assignment_id>/export")
@teacher_required
def export_assignment_scores(assignment_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            assignment_item = _load_teacher_assignment(cursor, session["teacher_id"], assignment_id)
            if not assignment_item:
                flash("Không tìm thấy bài tập hoặc bạn không có quyền truy cập.", "warning")
                return redirect(url_for("assignments.index"))

            submission_items = _load_assignment_submissions(cursor, assignment_id, assignment_item["class_id"])
            workbook_bytes = build_assignment_scores_export(assignment_item, submission_items)
    except DB_ERRORS:
        current_app.logger.exception("Database error while exporting assignment scores")
        flash("Không thể xuất bảng điểm của bài tập này.", "danger")
        return redirect(url_for("assignments.detail", assignment_id=assignment_id))
    finally:
        if "conn" in locals():
            conn.close()

    file_name = f"bang_diem_bai_tap_{assignment_id}.xlsx"
    return send_file(
        BytesIO(workbook_bytes),
        as_attachment=True,
        download_name=file_name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@assignments_bp.route("/<int:assignment_id>/students/<int:student_id>/grade", methods=["POST"])
@teacher_required
def grade_student_submission(assignment_id, student_id):
    status = (request.form.get("status") or "").strip().lower()
    teacher_score_raw = (request.form.get("teacher_score") or "").strip()
    teacher_comment = (request.form.get("teacher_comment") or "").strip() or None

    if status not in {"submitted", "late", "missing"}:
        flash("Trạng thái bài nộp không hợp lệ.", "warning")
        return redirect(url_for("assignments.detail", assignment_id=assignment_id))

    teacher_score = None
    if teacher_score_raw:
        try:
            teacher_score = round(float(teacher_score_raw), 2)
        except ValueError:
            flash("Điểm số không hợp lệ.", "warning")
            return redirect(url_for("assignments.detail", assignment_id=assignment_id))
        if teacher_score < 0 or teacher_score > 10:
            flash("Điểm phải nằm trong khoảng 0 đến 10.", "warning")
            return redirect(url_for("assignments.detail", assignment_id=assignment_id))

    if status == "missing":
        teacher_score = None

    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            assignment_item = _load_teacher_assignment(cursor, session["teacher_id"], assignment_id)
            if not assignment_item:
                flash("Không tìm thấy bài tập hoặc bạn không có quyền chấm bài.", "warning")
                return redirect(url_for("assignments.index"))

            cursor.execute(
                """
                SELECT 1
                FROM class_students
                WHERE class_id = %s AND student_id = %s
                """,
                (assignment_item["class_id"], student_id),
            )
            enrolled = cursor.fetchone()
            if not enrolled:
                flash("Sinh viên không thuộc lớp của bài tập này.", "warning")
                return redirect(url_for("assignments.detail", assignment_id=assignment_id))

            cursor.execute(
                """
                SELECT id, submitted_at
                FROM submissions
                WHERE assignment_id = %s
                  AND student_id = %s
                """,
                (assignment_id, student_id),
            )
            submission_row = cursor.fetchone()

            if status in {"submitted", "late"}:
                existing_submitted_at = submission_row["submitted_at"] if submission_row else None
                submitted_at = existing_submitted_at or datetime.now()
            else:
                submitted_at = None

            if submission_row:
                cursor.execute(
                    """
                    UPDATE submissions
                    SET status = %s,
                        teacher_score = %s,
                        teacher_comment = %s,
                        submitted_at = %s
                    WHERE assignment_id = %s
                      AND student_id = %s
                    """,
                    (status, teacher_score, teacher_comment, submitted_at, assignment_id, student_id),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO submissions (
                        assignment_id, student_id, status, teacher_score, teacher_comment, submitted_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (assignment_id, student_id, status, teacher_score, teacher_comment, submitted_at),
                )
            _commit_if_possible(conn)
            flash("Đã cập nhật điểm và nhận xét.", "success")
    except DB_ERRORS:
        current_app.logger.exception("Database error while grading submission")
        flash("Không thể cập nhật bài nộp.", "danger")
    finally:
        if "conn" in locals():
            conn.close()

    return redirect(url_for("assignments.detail", assignment_id=assignment_id))


@assignments_bp.route("/<int:assignment_id>/delete", methods=["POST"])
@teacher_required
def delete_assignment(assignment_id):
    try:
        conn = get_db_connection()
        with conn.cursor() as cursor:
            assignment_item = _load_teacher_assignment(cursor, session["teacher_id"], assignment_id)
            if not assignment_item:
                flash("Không tìm thấy bài tập hoặc bạn không có quyền xóa.", "warning")
                return redirect(url_for("assignments.index"))

            cursor.execute("DELETE FROM assignments WHERE id = %s", (assignment_id,))
            _commit_if_possible(conn)
            flash("Đã xóa bài tập.", "success")
    except DB_ERRORS:
        current_app.logger.exception("Database error while deleting assignment")
        flash("Không thể xóa bài tập.", "danger")
    finally:
        if "conn" in locals():
            conn.close()

    return redirect(url_for("assignments.index"))
