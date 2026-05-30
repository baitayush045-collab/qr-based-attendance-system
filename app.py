from flask import Flask, render_template, request, redirect, url_for, session, flash, send_file
from datetime import datetime, timedelta
from werkzeug.middleware.proxy_fix import ProxyFix
import csv
import io
import os
import uuid

from database import (
    init_db,
    get_teacher_by_login,
    get_student_by_login,
    get_all_attendance,
    get_today_attendance,
    get_student_count,
    has_attendance_today,
    mark_attendance_today,
    save_qr_token,
    is_qr_token_valid,
    get_attendance_sheet_for_date,
)
app = Flask(__name__)

secret_key = os.environ.get("SECRET_KEY")
if not secret_key:
    if os.environ.get("FLASK_ENV") == "production":
        raise RuntimeError("SECRET_KEY environment variable is required in production.")
    secret_key = "dev-only-change-me"

app.secret_key = secret_key
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") == "production"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)

# Required on Render so sessions/redirects work behind HTTPS proxy
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

init_db()


@app.before_request
def make_session_permanent():
    session.permanent = True


def mark_attendance_for_session():
    return mark_attendance_today(
        session["student_id"],
        session["student_name"],
        session["student_roll"],
        session["student"],
    )


@app.route("/health")
def health():
    return "OK", 200


@app.route("/")
def login():
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("login"))


@app.route("/student_login", methods=["POST"])
def student_login():
    email = request.form["email"]
    password = request.form["password"]
    pending_qr = session.get("qr_token")

    student = get_student_by_login(email, password)

    if student:
        session.clear()
        session["student_id"] = student["id"]
        session["student_name"] = student["name"]
        session["student_roll"] = student["roll"]
        session["student"] = student["email"]

        if pending_qr:
            return redirect(url_for("scan_qr", token=pending_qr))

        return redirect(url_for("student_dashboard"))

    flash("Invalid student email or password.", "error")
    return redirect(url_for("login"))


@app.route("/teacher_login", methods=["POST"])
def teacher_login():
    email = request.form["email"]
    password = request.form["password"]

    teacher = get_teacher_by_login(email, password)

    if teacher:
        session.clear()
        session["teacher"] = teacher["email"]
        session["teacher_name"] = teacher["name"]
        return redirect(url_for("teacher_dashboard"))

    flash("Invalid teacher email or password.", "error")
    return redirect(url_for("login"))


@app.route("/student-dashboard", strict_slashes=False)
def student_dashboard():
    if "student" not in session:
        return redirect(url_for("login"))

    today_prefix = datetime.now().strftime("%d-%m-%Y")
    marked_today = has_attendance_today(session["student"], today_prefix)

    return render_template(
        "student-dashboard.html",
        student_name=session.get("student_name"),
        marked_today=marked_today,
    )


@app.route("/scan-page", strict_slashes=False)
def scan_page():
    if "student" not in session:
        return redirect(url_for("login"))

    return render_template("scan.html")


@app.route("/teacher-dashboard", strict_slashes=False)
def teacher_dashboard():
    if "teacher" not in session:
        return redirect(url_for("login"))

    today_prefix = datetime.now().strftime("%d-%m-%Y")
    today_records = get_today_attendance(today_prefix)
    total_registered = get_student_count()
    today_count = len(today_records)

    percentage = round((today_count / total_registered) * 100) if total_registered else 0

    records = [
        {
            "Name": r["name"],
            "Roll": r["roll"],
            "Email": r["email"],
            "Time": r["time"],
            "Status": r["status"],
        }
        for r in get_all_attendance()
    ]

    return render_template(
        "teacher-dashboard.html",
        records=records,
        total_students=total_registered,
        today_attendance=today_count,
        attendance_percentage=percentage,
        qr_link=session.get("qr_link"),
        teacher_name=session.get("teacher_name"),
        today_date=datetime.now().strftime("%Y-%m-%d"),
    )


@app.route("/download-attendance")
def download_attendance():
    if "teacher" not in session:
        return redirect(url_for("login"))

    date_input = request.args.get("date")
    if not date_input:
        flash("Please select a date to download.", "error")
        return redirect(url_for("teacher_dashboard"))

    try:
        parsed = datetime.strptime(date_input, "%Y-%m-%d")
        date_str = parsed.strftime("%d-%m-%Y")
    except ValueError:
        flash("Invalid date selected.", "error")
        return redirect(url_for("teacher_dashboard"))

    sheet = get_attendance_sheet_for_date(date_str)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date", date_str])
    writer.writerow([])
    writer.writerow(["Roll", "Name", "Email", "Time", "Status"])
    for row in sheet:
        writer.writerow(
            [row["roll"], row["name"], row["email"], row["time"], row["status"]]
        )

    present_count = sum(1 for row in sheet if row["status"] == "Present")
    writer.writerow([])
    writer.writerow(["Total Students", len(sheet)])
    writer.writerow(["Present", present_count])
    writer.writerow(["Absent", len(sheet) - present_count])

    file_data = io.BytesIO(output.getvalue().encode("utf-8-sig"))
    file_data.seek(0)
    filename = f"attendance_{date_input}.csv"

    return send_file(
        file_data,
        mimetype="text/csv",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/generate_qr", methods=["POST"])
def generate_qr():
    if "teacher" not in session:
        return redirect(url_for("login"))

    qr_token = str(uuid.uuid4())
    save_qr_token(qr_token)

    qr_link = request.host_url.rstrip("/") + f"/scan/{qr_token}"
    session["qr_link"] = qr_link

    return redirect(url_for("teacher_dashboard"))


@app.route("/scan/<token>")
def scan_qr(token):
    if not is_qr_token_valid(token):
        flash("Invalid or expired QR code. Ask teacher to generate a new one.", "error")
        return redirect(url_for("login"))

    if "student" not in session:
        session["qr_token"] = token
        return redirect(url_for("login"))

    result = mark_attendance_for_session()

    if result == "success":
        flash("Attendance marked successfully!", "success")
    else:
        flash("You already marked attendance today.", "warning")

    return redirect(url_for("student_dashboard"))


if __name__ == "__main__":
    debug = os.environ.get("FLASK_ENV") != "production"
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=debug, host="0.0.0.0", port=port)
