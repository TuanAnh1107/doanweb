from functools import wraps

from flask import flash, redirect, session, url_for


def get_session_role():
    if session.get("user_role"):
        return session["user_role"]
    if session.get("teacher_id"):
        return "teacher"
    if session.get("student_id"):
        return "student"
    return None


def clear_auth_session():
    for key in (
        "user_role",
        "user_name",
        "teacher_id",
        "teacher_name",
        "teacher_email",
        "student_id",
        "student_name",
        "student_email",
        "student_code",
    ):
        session.pop(key, None)


def set_teacher_session(teacher):
    clear_auth_session()
    session["user_role"] = "teacher"
    session["user_name"] = teacher["full_name"]
    session["teacher_id"] = teacher["id"]
    session["teacher_name"] = teacher["full_name"]
    session["teacher_email"] = teacher["email"]


def set_student_session(student):
    clear_auth_session()
    session["user_role"] = "student"
    session["user_name"] = student["full_name"]
    session["student_id"] = student["id"]
    session["student_name"] = student["full_name"]
    session["student_email"] = student.get("email")
    session["student_code"] = student.get("student_code")


def redirect_to_role_dashboard():
    role = get_session_role()
    if role == "teacher" and session.get("teacher_id"):
        return redirect(url_for("auth.dashboard"))
    if role == "student" and session.get("student_id"):
        return redirect(url_for("auth.student_dashboard"))
    return None


def teacher_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        role = get_session_role()
        if role == "teacher" and session.get("teacher_id"):
            return view_func(*args, **kwargs)
        if role == "student" and session.get("student_id"):
            flash("Trang này dành cho giảng viên.", "warning")
            return redirect(url_for("auth.student_dashboard"))
        return redirect(url_for("auth.login"))

    return wrapped


def student_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        role = get_session_role()
        if role == "student" and session.get("student_id"):
            return view_func(*args, **kwargs)
        if role == "teacher" and session.get("teacher_id"):
            flash("Trang này dành cho sinh viên.", "warning")
            return redirect(url_for("auth.dashboard"))
        return redirect(url_for("auth.login"))

    return wrapped
