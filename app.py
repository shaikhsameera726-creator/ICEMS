from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
import os
import random
import sqlite3

from werkzeug.security import generate_password_hash, check_password_hash


app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_super_secret_key_2025")


# In-memory conversation state for the local therapy assistant.
therapy_memory = {}


def get_db_connection():
    conn = sqlite3.connect("database.db")
    conn.row_factory = sqlite3.Row
    return conn


def require_role(*roles):
    if not session.get("user_id"):
        return jsonify({"error": "Login required"}), 401
    if roles and session.get("role") not in roles:
        return jsonify({"error": "Unauthorized"}), 403
    return None


def init_db():
    conn = get_db_connection()
    c = conn.cursor()

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'faculty', 'student'))
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            description TEXT,
            date TEXT,
            organizer TEXT
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT,
            uploader TEXT
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS doubts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            answer TEXT,
            asker TEXT,
            resolver TEXT
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS charities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            amount_raised REAL DEFAULT 0
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS therapy_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_username TEXT,
            student_message TEXT NOT NULL,
            ai_response TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    therapy_columns = {
        row["name"]
        for row in c.execute("PRAGMA table_info(therapy_messages)").fetchall()
    }
    if "student_username" not in therapy_columns:
        c.execute("ALTER TABLE therapy_messages ADD COLUMN student_username TEXT")

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS event_fundraising (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            target_amount REAL DEFAULT 0,
            current_amount REAL DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (event_id) REFERENCES events(id)
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS donations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fundraising_id INTEGER NOT NULL,
            student_id INTEGER NOT NULL,
            student_username TEXT NOT NULL,
            amount REAL NOT NULL,
            donated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (fundraising_id) REFERENCES event_fundraising(id),
            FOREIGN KEY (student_id) REFERENCES users(id)
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            student_username TEXT NOT NULL,
            subject TEXT NOT NULL,
            date TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('present', 'absent', 'late')),
            marked_by TEXT NOT NULL,
            marked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (student_id) REFERENCES users(id)
        )
        """
    )

    c.execute(
        """
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            faculty_id INTEGER,
            FOREIGN KEY (faculty_id) REFERENCES users(id)
        )
        """
    )

    hashed_admin = generate_password_hash("admin123", method="pbkdf2:sha256")
    try:
        c.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, 'admin')",
            ("admin", hashed_admin),
        )
        conn.commit()
        print("Default admin account created.")
    except sqlite3.IntegrityError:
        print("Default admin account already exists.")

    conn.close()
    print("Database tables checked / created.")


def get_ai_therapy_response(username, message):
    """Generate a fully local, context-aware therapy reply."""
    normalized_message = " ".join((message or "").strip().lower().split())
    if not normalized_message:
        return {
            "text": "I'm here with you. Start with one sentence about what's been weighing on you today.",
            "source": "local-ai",
            "reason": None,
        }

    emotion_keywords = {
        "stress": {"stress", "stressed", "pressure", "overwhelmed", "tense", "load"},
        "anxiety": {"anxious", "anxiety", "panic", "nervous", "worried", "fear", "scared"},
        "sad": {"sad", "down", "empty", "cry", "crying", "hurt", "hopeless", "low"},
        "lonely": {"alone", "lonely", "isolated", "ignored", "left out", "nobody"},
        "burnout": {"burnout", "burned out", "exhausted", "drained", "tired", "fatigue"},
        "exams": {"exam", "exams", "test", "tests", "marks", "grades", "assignment", "deadline"},
        "sleep": {"sleep", "insomnia", "awake", "restless", "can't sleep", "tired"},
        "confidence": {"failure", "worthless", "useless", "can't do", "not good enough", "confidence"},
    }

    mood_scores = {}
    for mood, keywords in emotion_keywords.items():
        score = sum(1 for keyword in keywords if keyword in normalized_message)
        if score:
            mood_scores[mood] = score

    if mood_scores:
        detected_mood = max(mood_scores, key=mood_scores.get)
    else:
        detected_mood = "general_support"

    user_memory = therapy_memory.get(
        username,
        {"last_mood": None, "repeat_count": 0, "recent_messages": []},
    )
    repeated_mood = user_memory["last_mood"] == detected_mood
    repeat_count = user_memory["repeat_count"] + 1 if repeated_mood else 1

    empathy_openers = {
        "stress": [
            "That sounds like a lot to carry at once.",
            "It makes sense that your mind feels crowded right now.",
        ],
        "anxiety": [
            "That kind of worry can make everything feel louder than it is.",
            "I can hear how on-edge this is making you feel.",
        ],
        "sad": [
            "I'm sorry you're carrying that heaviness.",
            "That sounds genuinely painful, and I'm glad you said it out loud.",
        ],
        "lonely": [
            "Feeling alone in the middle of college life can hurt a lot.",
            "That kind of loneliness can feel really sharp.",
        ],
        "burnout": [
            "You sound worn down, not weak.",
            "That kind of exhaustion usually means you've been pushing for too long.",
        ],
        "exams": [
            "Exam pressure can make even small tasks feel huge.",
            "That kind of academic pressure can snowball quickly.",
        ],
        "sleep": [
            "Poor sleep can make everything feel heavier the next day.",
            "When rest is off, stress usually feels twice as loud.",
        ],
        "confidence": [
            "It hurts when your inner voice turns against you.",
            "It sounds like your confidence has taken a real hit.",
        ],
        "general_support": [
            "Thanks for being honest about how you're feeling.",
            "I'm glad you reached out instead of holding it in alone.",
        ],
    }

    continuity_openers = {
        "stress": "I remember you've been under pressure for a bit now, so let's make this easier on your system, not harder.",
        "anxiety": "It sounds like this worry is staying with you, so let's focus on something grounding and doable.",
        "sad": "I can see this heaviness has not lifted yet, and that deserves care rather than self-judgment.",
        "lonely": "It sounds like that sense of disconnection is still hanging around, and that can wear you down quietly.",
        "burnout": "This seems to be an ongoing strain, which usually means rest and boundaries matter more than pushing harder.",
        "exams": "This exam stress has been repeating, so let's shift from panic mode to a smaller plan you can actually follow.",
        "sleep": "Your sleep seems to keep getting affected, so calming your body may help more than trying to force sleep.",
        "confidence": "I can hear that self-doubt showing up again, so let's answer it with something steadier and more real.",
        "general_support": "It sounds like this feeling is still with you, and I'm glad you checked in again.",
    }

    actionable_support = {
        "stress": [
            "Pick the next one small task only, and give it 10 focused minutes.",
            "Drop your shoulders, unclench your jaw, and take five slow breaths before starting again.",
        ],
        "anxiety": [
            "Try naming five things you can see and four things you can feel to bring your mind back to the present.",
            "Keep your next step tiny: one page, one email, or one short revision block.",
        ],
        "sad": [
            "Be gentle with your energy today and choose one caring action like water, food, or a short walk.",
            "If you can, message one person you trust instead of sitting with it alone.",
        ],
        "lonely": [
            "A small reach-out counts, even if it's just asking a classmate how they're doing.",
            "Try spending a little time in a shared space so you're not carrying this completely alone.",
        ],
        "burnout": [
            "Your body may need recovery, so take a real break without multitasking for 15 minutes.",
            "Lower today's target to the minimum meaningful version instead of forcing peak performance.",
        ],
        "exams": [
            "Choose one subject, one topic, and one 25-minute session instead of trying to fix everything tonight.",
            "After each study block, write two bullet points of what you understood to build confidence.",
        ],
        "sleep": [
            "Dim the lights, put the screen away for a few minutes, and slow your breathing rather than chasing sleep.",
            "If your thoughts are racing, write them down so your mind does not have to keep holding them.",
        ],
        "confidence": [
            "Talk to yourself like you would talk to a stressed friend, not like an enemy.",
            "Write down one thing you handled well this week, even if it felt small.",
        ],
        "general_support": [
            "Try pausing for a minute and asking yourself what feels hardest right now: the emotion, the workload, or feeling alone.",
            "You do not need to solve everything tonight; one honest next step is enough.",
        ],
    }

    context_reflections = []
    if any(word in normalized_message for word in {"friend", "family", "roommate", "teacher", "faculty"}):
        context_reflections.append("It also sounds like people around you may be part of what is making this heavier.")
    if any(word in normalized_message for word in {"deadline", "assignment", "submission", "project"}):
        context_reflections.append("Deadlines can create a constant background pressure that drains focus.")
    if "can't" in normalized_message or "cannot" in normalized_message:
        context_reflections.append("When your mind says 'I can't,' it usually means you're overloaded, not incapable.")

    opener = random.choice(empathy_openers[detected_mood])
    if repeat_count > 1:
        opener = continuity_openers[detected_mood]

    suggestions = random.sample(actionable_support[detected_mood], k=min(2, len(actionable_support[detected_mood])))
    reflection = f" {random.choice(context_reflections)}" if context_reflections else ""
    response_text = f"{opener}{reflection} Try this next: {suggestions[0]} Also, {suggestions[1]}"

    user_memory["last_mood"] = detected_mood
    user_memory["repeat_count"] = repeat_count
    user_memory["recent_messages"] = (user_memory["recent_messages"] + [message])[-5:]
    therapy_memory[username] = user_memory

    return {
        "text": response_text,
        "source": "local-ai",
        "reason": None,
    }


init_db()


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/login", methods=["POST"])
def login():
    username = request.form.get("username")
    password = request.form.get("password")

    if not username or not password:
        flash("Username and password are required.", "danger")
        return redirect(url_for("home"))

    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()

    if user and check_password_hash(user["password"], password):
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        session["role"] = user["role"]
        flash(f"Welcome, {user['username']}!", "success")
        if user["role"] == "admin":
            return redirect(url_for("admin"))
        if user["role"] == "faculty":
            return redirect(url_for("faculty"))
        return redirect(url_for("student"))

    flash("Invalid username or password.", "danger")
    return redirect(url_for("home"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        role = request.form.get("role")
        username = request.form.get("username")
        password = request.form.get("password")
        confirm = request.form.get("confirm_password")

        if role != "student":
            flash("Only student registration is allowed here.", "warning")
            return redirect(url_for("register"))
        if not username or not password or not confirm:
            flash("All fields are required.", "danger")
            return redirect(url_for("register"))
        if password != confirm:
            flash("Passwords do not match.", "danger")
            return redirect(url_for("register"))

        hashed = generate_password_hash(password)
        conn = get_db_connection()
        try:
            conn.execute(
                "INSERT INTO users (username, password, role) VALUES (?, ?, 'student')",
                (username, hashed),
            )
            conn.commit()
            flash("Registration successful!", "success")
            return redirect(url_for("home"))
        except sqlite3.IntegrityError:
            flash("Username already exists.", "danger")
        finally:
            conn.close()

    return render_template("register.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for("home"))


@app.route("/admin")
def admin():
    if session.get("role") != "admin":
        flash("Admin access only.", "danger")
        return redirect(url_for("home"))
    return render_template("admin.html")


@app.route("/faculty")
def faculty():
    if session.get("role") != "faculty":
        flash("Faculty access only.", "danger")
        return redirect(url_for("home"))
    return render_template("faculty.html")


@app.route("/student")
def student():
    if session.get("role") != "student":
        flash("Student access only.", "danger")
        return redirect(url_for("home"))
    return render_template("student.html")


@app.route("/api/events")
def api_events():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM events ORDER BY date DESC, id DESC").fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route("/api/events", methods=["POST"])
@app.route("/api/admin/events", methods=["POST"])
def admin_create_event():
    auth_error = require_role("admin")
    if auth_error:
        return auth_error

    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    date = data.get("date")
    organizer = (data.get("organizer") or session.get("username") or "Admin").strip()

    if not title:
        return jsonify({"error": "Title required"}), 400

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO events (title, description, date, organizer) VALUES (?, ?, ?, ?)",
            (title, description, date, organizer),
        )
        conn.commit()
        return jsonify({"success": True, "event_id": cursor.lastrowid})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/create-faculty", methods=["POST"])
def create_faculty():
    auth_error = require_role("admin")
    if auth_error:
        return auth_error

    data = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400

    conn = get_db_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, 'faculty')",
            (username, generate_password_hash(password)),
        )
        conn.commit()
        return jsonify({"success": True, "faculty_id": cursor.lastrowid})
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already exists"}), 409
    finally:
        conn.close()


@app.route("/api/notes")
def api_notes():
    auth_error = require_role("student", "faculty", "admin")
    if auth_error:
        return auth_error

    conn = get_db_connection()
    rows = conn.execute(
        "SELECT id, title, content, uploader FROM notes ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route("/api/student/doubts", methods=["GET", "POST"])
def student_doubts():
    auth_error = require_role("student")
    if auth_error:
        return auth_error

    username = session.get("username")
    conn = get_db_connection()

    if request.method == "POST":
        data = request.get_json() or {}
        question = (data.get("question") or "").strip()
        if not question:
            conn.close()
            return jsonify({"error": "Question is required"}), 400

        cursor = conn.execute(
            "INSERT INTO doubts (question, asker) VALUES (?, ?)",
            (question, username),
        )
        conn.commit()
        conn.close()
        return jsonify({"success": True, "doubt_id": cursor.lastrowid})

    rows = conn.execute(
        "SELECT id, question, answer, asker, resolver FROM doubts WHERE asker = ? ORDER BY id DESC",
        (username,),
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route("/api/charities", methods=["GET", "POST"])
def charities():
    auth_error = require_role("admin", "student", "faculty")
    if auth_error:
        return auth_error

    conn = get_db_connection()

    if request.method == "POST":
        if session.get("role") != "admin":
            conn.close()
            return jsonify({"error": "Admin only"}), 403

        data = request.get_json() or {}
        name = (data.get("name") or "").strip()
        description = (data.get("description") or "").strip()

        if not name:
            conn.close()
            return jsonify({"error": "Charity name is required"}), 400

        cursor = conn.execute(
            "INSERT INTO charities (name, description) VALUES (?, ?)",
            (name, description),
        )
        conn.commit()
        charity_id = cursor.lastrowid
        conn.close()
        return jsonify({"success": True, "charity_id": charity_id})

    rows = conn.execute(
        "SELECT id, name, description, amount_raised FROM charities ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route("/api/admin/fundraising/create", methods=["POST"])
def admin_create_fundraising():
    auth_error = require_role("admin")
    if auth_error:
        return auth_error

    data = request.get_json() or {}
    event_id = data.get("event_id")
    target_amount = data.get("target_amount", 0)

    if not event_id:
        return jsonify({"error": "Event ID required"}), 400

    conn = get_db_connection()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if not event:
        conn.close()
        return jsonify({"error": "Event not found"}), 404

    existing = conn.execute(
        "SELECT * FROM event_fundraising WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "Fundraising exists"}), 409

    try:
        cursor = conn.execute(
            "INSERT INTO event_fundraising (event_id, target_amount) VALUES (?, ?)",
            (event_id, target_amount),
        )
        conn.commit()
        return jsonify({"success": True, "fundraising_id": cursor.lastrowid})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    finally:
        conn.close()


@app.route("/api/fundraising/active")
def get_active_fundraising():
    auth_error = require_role("student", "faculty", "admin")
    if auth_error:
        return auth_error

    conn = get_db_connection()
    campaigns = conn.execute(
        """
        SELECT
            ef.id AS fundraising_id,
            ef.event_id,
            ef.target_amount,
            ef.current_amount,
            ef.created_at,
            e.title AS event_title,
            e.description AS event_description,
            e.date AS event_date,
            e.organizer
        FROM event_fundraising ef
        JOIN events e ON ef.event_id = e.id
        WHERE ef.is_active = 1
        ORDER BY ef.created_at DESC
        """
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in campaigns])


@app.route("/api/therapy/send", methods=["POST"])
def therapy_send_message():
    auth_error = require_role("student")
    if auth_error:
        return auth_error

    data = request.get_json() or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "Message cannot be empty"}), 400

    username = session.get("username")
    ai_result = get_ai_therapy_response(username, message)
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO therapy_messages (student_username, student_message, ai_response) VALUES (?, ?, ?)",
        (username, message, ai_result["text"]),
    )
    conn.commit()
    conn.close()

    return jsonify(
        {
            "success": True,
            "ai_response": ai_result["text"],
            "ai_source": "local-ai",
            "ai_reason": None,
        }
    )


@app.route("/therapy")
def therapy():
    if session.get("role") != "student":
        flash("Students only can access therapy chat.", "danger")
        return redirect(url_for("home"))
    return render_template("student.html")


@app.route("/api/therapy/history")
def therapy_history():
    auth_error = require_role("student")
    if auth_error:
        return auth_error

    conn = get_db_connection()
    messages = conn.execute(
        """
        SELECT student_message, ai_response, timestamp
        FROM therapy_messages
        WHERE student_username = ?
        ORDER BY timestamp DESC, id DESC
        """,
        (session.get("username"),),
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in messages])


@app.route("/api/admin/therapy/students")
def admin_therapy_students():
    auth_error = require_role("admin")
    if auth_error:
        return auth_error

    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT
            student_username,
            COUNT(*) AS total_sessions,
            MIN(timestamp) AS first_used,
            MAX(timestamp) AS last_used
        FROM therapy_messages
        GROUP BY student_username
        ORDER BY last_used DESC
        """
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route("/api/faculty/notes", methods=["POST"])
def faculty_upload_note():
    auth_error = require_role("faculty")
    if auth_error:
        return auth_error

    data = request.get_json() or {}
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not title or not content:
        return jsonify({"error": "Title and content required"}), 400

    conn = get_db_connection()
    cursor = conn.execute(
        "INSERT INTO notes (title, content, uploader) VALUES (?, ?, ?)",
        (title, content, session.get("username")),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "note_id": cursor.lastrowid})


@app.route("/api/student/donate", methods=["POST"])
def student_donate():
    auth_error = require_role("student")
    if auth_error:
        return auth_error

    data = request.get_json() or {}
    fundraising_id = data.get("fundraising_id")

    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "Amount must be a valid number"}), 400

    if amount <= 0:
        return jsonify({"error": "Amount must be greater than zero"}), 400

    conn = get_db_connection()
    campaign = conn.execute(
        "SELECT id FROM event_fundraising WHERE id = ? AND is_active = 1",
        (fundraising_id,),
    ).fetchone()
    if not campaign:
        conn.close()
        return jsonify({"error": "Fundraising campaign not found"}), 404

    conn.execute(
        """
        INSERT INTO donations (fundraising_id, student_id, student_username, amount)
        VALUES (?, ?, ?, ?)
        """,
        (fundraising_id, session["user_id"], session["username"], amount),
    )
    conn.execute(
        "UPDATE event_fundraising SET current_amount = current_amount + ? WHERE id = ?",
        (amount, fundraising_id),
    )
    conn.commit()
    conn.close()
    return jsonify({"success": True, "message": f"Donation of Rs. {amount:.2f} recorded successfully"})


@app.route("/api/student/attendance/summary")
def student_attendance_summary():
    auth_error = require_role("student")
    if auth_error:
        return auth_error

    conn = get_db_connection()
    rows = conn.execute(
        """
        SELECT
            subject,
            COUNT(*) AS total_classes,
            SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END) AS present_classes,
            ROUND(
                (SUM(CASE WHEN status = 'present' THEN 1 ELSE 0 END) * 100.0) / COUNT(*),
                2
            ) AS attendance_percentage
        FROM attendance
        WHERE student_id = ?
        GROUP BY subject
        ORDER BY subject
        """,
        (session["user_id"],),
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "").strip() == "1"

    app.run(
        host="0.0.0.0",
        port=port,
        debug=debug_mode,
        use_reloader=False
    )