import os
import sqlite3
from datetime import datetime, timedelta

from werkzeug.security import check_password_hash, generate_password_hash

DATA_DIR = os.environ.get("DATA_DIR")
if DATA_DIR:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except OSError:
        DATA_DIR = None
if not DATA_DIR:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.path.join(DATA_DIR, "attendance.db")

# Valid through a full 2-minute rush plus late arrivals
QR_VALID_MINUTES = 10
TARGET_STUDENT_COUNT = 100
DEFAULT_STUDENT_PASSWORD = "1234"

TEACHERS = [
    ("Mr. Sharma", "teacher@gmail.com", "1233"),
    ("Ms. Patel", "teacher2@gmail.com", "5678"),
]

STUDENTS = [
    ("Aayush", "101", "student@gmail.com", "1234"),
    ("Priya", "102", "priya@gmail.com", "1234"),
    ("Rahul", "103", "rahul@gmail.com", "1234"),
    ("Sneha", "104", "sneha@gmail.com", "1234"),
    ("Vikram", "105", "vikram@gmail.com", "1234"),
]


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _configure_connection(conn)
    return conn


def _configure_connection(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-64000")


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS teachers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            roll TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER,
            name TEXT,
            roll TEXT,
            email TEXT,
            time TEXT,
            status TEXT,
            attend_date TEXT,
            FOREIGN KEY (student_id) REFERENCES students(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS qr_tokens (
            token TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
    """)

    _migrate_attendance_date(conn)

    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_unique_day
        ON attendance(email, attend_date)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_qr_created_at
        ON qr_tokens(created_at)
    """)
    c.execute("""
        CREATE INDEX IF NOT EXISTS idx_students_email
        ON students(email)
    """)

    conn.commit()
    cleanup_expired_qr_tokens(conn)

    for name, email, password in TEACHERS:
        c.execute("SELECT id FROM teachers WHERE email = ?", (email,))
        if not c.fetchone():
            c.execute(
                "INSERT INTO teachers (name, email, password) VALUES (?, ?, ?)",
                (name, email, password),
            )

    for name, roll, email, password in STUDENTS:
        c.execute("SELECT id FROM students WHERE email = ?", (email,))
        if not c.fetchone():
            c.execute(
                "INSERT INTO students (name, roll, email, password) VALUES (?, ?, ?, ?)",
                (name, roll, email, password),
            )

    conn.commit()
    _hash_plain_passwords(conn)
    _seed_students_until(conn, TARGET_STUDENT_COUNT)
    conn.close()


def _migrate_attendance_date(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(attendance)")}
    if "attend_date" not in columns:
        conn.execute("ALTER TABLE attendance ADD COLUMN attend_date TEXT")
        conn.execute("""
            UPDATE attendance
            SET attend_date = substr(time, 1, 10)
            WHERE attend_date IS NULL AND time IS NOT NULL
        """)
        conn.commit()


def _seed_students_until(conn, target):
    current = conn.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    if current >= target:
        return

    password_hash = generate_password_hash(DEFAULT_STUDENT_PASSWORD)
    for i in range(current + 1, target + 1):
        roll = str(100 + i)
        email = f"student{i}@college.com"
        name = f"Student {i}"
        try:
            conn.execute(
                """
                INSERT INTO students (name, roll, email, password)
                VALUES (?, ?, ?, ?)
                """,
                (name, roll, email, password_hash),
            )
        except sqlite3.IntegrityError:
            pass
    conn.commit()


def _verify_password(stored_password, provided_password):
    if stored_password.startswith(("pbkdf2:", "scrypt:")):
        return check_password_hash(stored_password, provided_password)
    return stored_password == provided_password


def _hash_plain_passwords(conn):
    for table in ("teachers", "students"):
        rows = conn.execute(f"SELECT id, password FROM {table}").fetchall()
        for row in rows:
            if not row["password"].startswith(("pbkdf2:", "scrypt:")):
                conn.execute(
                    f"UPDATE {table} SET password = ? WHERE id = ?",
                    (generate_password_hash(row["password"]), row["id"]),
                )
    conn.commit()


def get_teacher_by_login(email, password):
    conn = get_db()
    teacher = conn.execute(
        "SELECT id, name, email, password FROM teachers WHERE email = ?",
        (email,),
    ).fetchone()
    conn.close()
    if teacher and _verify_password(teacher["password"], password):
        return teacher
    return None


def get_student_by_login(email, password):
    conn = get_db()
    student = conn.execute(
        "SELECT id, name, roll, email, password FROM students WHERE email = ?",
        (email,),
    ).fetchone()
    conn.close()
    if student and _verify_password(student["password"], password):
        return student
    return None


def save_qr_token(token):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO qr_tokens (token, created_at) VALUES (?, ?)",
        (token, datetime.now().isoformat()),
    )
    conn.commit()
    cleanup_expired_qr_tokens(conn)
    conn.close()


def cleanup_expired_qr_tokens(conn=None):
    close_conn = conn is None
    if conn is None:
        conn = get_db()

    cutoff = (datetime.now() - timedelta(minutes=QR_VALID_MINUTES)).isoformat()
    conn.execute("DELETE FROM qr_tokens WHERE created_at < ?", (cutoff,))
    conn.commit()

    if close_conn:
        conn.close()


def is_qr_token_valid(token):
    conn = get_db()
    row = conn.execute(
        "SELECT created_at FROM qr_tokens WHERE token = ?",
        (token,),
    ).fetchone()
    conn.close()

    if not row:
        return False

    created_at = datetime.fromisoformat(row["created_at"])
    if datetime.now() > created_at + timedelta(minutes=QR_VALID_MINUTES):
        conn = get_db()
        conn.execute("DELETE FROM qr_tokens WHERE token = ?", (token,))
        conn.commit()
        conn.close()
        return False

    return True


def get_student_count():
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) AS total FROM students").fetchone()["total"]
    conn.close()
    return count


def get_all_attendance():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT name, roll, email, time, status
        FROM attendance
        ORDER BY id DESC
        LIMIT 500
        """
    ).fetchall()
    conn.close()
    return rows


def get_today_attendance(date_prefix):
    conn = get_db()
    rows = conn.execute(
        """
        SELECT name, roll, email, time, status
        FROM attendance
        WHERE attend_date = ? OR time LIKE ?
        ORDER BY id DESC
        """,
        (date_prefix, f"{date_prefix}%"),
    ).fetchall()
    conn.close()
    return rows


def get_all_students():
    conn = get_db()
    rows = conn.execute(
        """
        SELECT id, name, roll, email
        FROM students
        ORDER BY CAST(roll AS INTEGER), roll
        """
    ).fetchall()
    conn.close()
    return rows


def get_attendance_sheet_for_date(date_str):
    """Full class sheet: every student marked Present or Absent for the day."""
    conn = get_db()
    students = conn.execute(
        """
        SELECT name, roll, email
        FROM students
        ORDER BY CAST(roll AS INTEGER), roll
        """
    ).fetchall()
    attendance_rows = conn.execute(
        """
        SELECT email, time, status
        FROM attendance
        WHERE attend_date = ? OR time LIKE ?
        """,
        (date_str, f"{date_str}%"),
    ).fetchall()
    conn.close()

    attendance_by_email = {row["email"]: row for row in attendance_rows}
    sheet = []
    for student in students:
        record = attendance_by_email.get(student["email"])
        sheet.append(
            {
                "roll": student["roll"],
                "name": student["name"],
                "email": student["email"],
                "time": record["time"] if record else "",
                "status": record["status"] if record else "Absent",
            }
        )
    return sheet


def has_attendance_today(email, date_prefix):
    conn = get_db()
    row = conn.execute(
        """
        SELECT 1 FROM attendance
        WHERE email = ? AND (attend_date = ? OR time LIKE ?)
        LIMIT 1
        """,
        (email, date_prefix, f"{date_prefix}%"),
    ).fetchone()
    conn.close()
    return row is not None


def mark_attendance_today(student_id, name, roll, email):
    """Single DB round-trip: check + insert. Safe for concurrent scans."""
    now = datetime.now()
    date_str = now.strftime("%d-%m-%Y")
    time_str = now.strftime("%d-%m-%Y %H:%M:%S")

    conn = get_db()
    try:
        existing = conn.execute(
            """
            SELECT 1 FROM attendance
            WHERE email = ? AND (attend_date = ? OR time LIKE ?)
            LIMIT 1
            """,
            (email, date_str, f"{date_str}%"),
        ).fetchone()
        if existing:
            return "already_marked"

        conn.execute(
            """
            INSERT INTO attendance
            (student_id, name, roll, email, time, status, attend_date)
            VALUES (?, ?, ?, ?, ?, 'Present', ?)
            """,
            (student_id, name, roll, email, time_str, date_str),
        )
        conn.commit()
        return "success"
    except sqlite3.IntegrityError:
        return "already_marked"
    finally:
        conn.close()


def add_student(name, roll, email, password):
    conn = get_db()
    conn.execute(
        """
        INSERT INTO students (name, roll, email, password)
        VALUES (?, ?, ?, ?)
        """,
        (name, roll, email, generate_password_hash(password)),
    )
    conn.commit()
    conn.close()
