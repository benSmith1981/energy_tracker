from flask import Flask, render_template, request, redirect, session, abort, url_for
import sqlite3
from datetime import datetime
import os
import secrets
import requests
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
app = Flask(__name__)
app.secret_key = "SECRET123"  # Change in production


# ---------- DB Helpers ----------
def get_db():
    db = app.config.get("DATABASE", "energy_database.db")
    conn = sqlite3.connect(db, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,       -- we'll store email here
        password TEXT NOT NULL,              -- hashed password
        must_change_password INTEGER NOT NULL DEFAULT 0
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        short_desc TEXT NOT NULL,
        long_desc TEXT NOT NULL,
        benefits TEXT NOT NULL,
        typical_saving_pct REAL DEFAULT 0.0
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS calculations(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        product_slug TEXT NOT NULL,
        email TEXT NOT NULL,
        user_id INTEGER,

        electricity_kwh REAL NOT NULL,
        gas_kwh REAL NOT NULL,
        home_size TEXT NOT NULL,
        occupants INTEGER NOT NULL,
        ev_charging INTEGER NOT NULL,
        smart_home INTEGER NOT NULL,

        kwh_saved REAL NOT NULL,
        cost_saved REAL NOT NULL,
        co2_saved REAL NOT NULL
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS bookings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        calc_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,

        full_name TEXT NOT NULL,
        phone TEXT,
        preferred_date TEXT NOT NULL,
        preferred_time TEXT NOT NULL,
        notes TEXT
    )
    """)

    conn.commit()
    conn.close()


def seed_products():
    conn = get_db()
    c = conn.cursor()

    products = [
        {
            "slug": "solar",
            "name": "Solar Panels",
            "short_desc": "Generate clean electricity from sunlight and reduce grid usage.",
            "long_desc": (
                "Solar panels convert sunlight into electricity for your home. "
                "This reduces the amount of electricity you need to buy from the grid, "
                "which can lower both costs and carbon footprint."
            ),
            "benefits": "• Reduce electricity bills\n• Lower carbon footprint\n• Works well with home batteries",
            "typical_saving_pct": 0.25
        },
        {
            "slug": "ev-charger",
            "name": "EV Charging Station",
            "short_desc": "Charge your electric vehicle at home safely and efficiently.",
            "long_desc": (
                "A dedicated EV charging station provides safer, faster charging than a standard plug. "
                "Smart chargers can schedule charging for off-peak times and track energy used for EV charging."
            ),
            "benefits": "• Faster charging\n• Potential off-peak savings\n• Track EV energy usage",
            "typical_saving_pct": 0.05
        },
        {
            "slug": "smart-home",
            "name": "Smart Home Energy Management",
            "short_desc": "Monitor and reduce energy use using automation and smart controls.",
            "long_desc": (
                "Smart home energy management can reduce wasted energy using automation, scheduling, "
                "and monitoring. Examples include smart thermostats, smart plugs, and device usage reports."
            ),
            "benefits": "• Reduce wasted energy\n• Better control of heating and appliances\n• Usage tracking and insights",
            "typical_saving_pct": 0.08
        }
    ]

    for p in products:
        c.execute("""
            INSERT OR IGNORE INTO products (slug, name, short_desc, long_desc, benefits, typical_saving_pct)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (p["slug"], p["name"], p["short_desc"], p["long_desc"], p["benefits"], p["typical_saving_pct"]))

    conn.commit()
    conn.close()


# ---------- Utils ----------
def parse_float(v, default=0.0):
    try:
        return float(v)
    except:
        return default


def parse_int(v, default=1):
    try:
        return int(v)
    except:
        return default


def calculate_savings(slug: str, electricity_kwh: float, gas_kwh: float, ev_charging: int, smart_home: int):
    """
    Simple, defensible assumptions for a student project.
    Returns monthly savings.
    """
    # prices + CO2 factors (easy defaults)
    elec_price = 0.28   # £/kWh
    gas_price = 0.07    # £/kWh
    elec_co2 = 0.20     # kg CO2 per kWh
    gas_co2 = 0.18      # kg CO2 per kWh

    kwh_saved = 0.0

    if slug == "solar":
        # Solar offsets about 25% of electricity usage
        kwh_saved = electricity_kwh * 0.25

    elif slug == "ev-charger":
        # Only matters if user charges at home. Small efficiency + off-peak behaviour.
        if ev_charging == 1:
            kwh_saved = electricity_kwh * 0.06
        else:
            kwh_saved = 0.0

    elif slug == "smart-home":
        # Smart home reduces both electricity and gas
        elec_red = 0.08 if smart_home == 1 else 0.03
        gas_red = 0.05 if smart_home == 1 else 0.02
        kwh_saved = (electricity_kwh * elec_red) + (gas_kwh * gas_red)

    # Convert kWh saved to money/CO2 saved
    if slug in ("solar", "ev-charger"):
        cost_saved = kwh_saved * elec_price
        co2_saved = kwh_saved * elec_co2
    else:
        total = max(1.0, electricity_kwh + gas_kwh)
        blend_price = (electricity_kwh * elec_price + gas_kwh * gas_price) / total
        blend_co2 = (electricity_kwh * elec_co2 + gas_kwh * gas_co2) / total
        cost_saved = kwh_saved * blend_price
        co2_saved = kwh_saved * blend_co2

    return round(kwh_saved, 2), round(cost_saved, 2), round(co2_saved, 2)


def create_user_if_needed(email: str, temp_password: str):
    conn = get_db()
    c = conn.cursor()

    existing = c.execute("SELECT id FROM users WHERE username = ?", (email,)).fetchone()
    if existing:
        conn.close()
        return existing["id"], False

    if not temp_password:
        temp_password = "TempPass123!"  # demo fallback only

    hashed = generate_password_hash(temp_password)

    c.execute("""
        INSERT INTO users(username, password, must_change_password)
        VALUES (?, ?, 1)
    """, (email, hashed))

    user_id = c.lastrowid
    conn.commit()
    conn.close()
    return user_id, True

# ---------- Routes ----------
@app.route("/")
def index():
    conn = get_db()
    rows = conn.execute("SELECT slug, name, short_desc FROM products ORDER BY id").fetchall()
    conn.close()
    return render_template("index.html", products=rows)



@app.route("/product/<slug>", methods=["GET", "POST"])
def product(slug):
    conn = get_db()
    row = conn.execute(
        "SELECT slug, name, short_desc, long_desc, benefits, typical_saving_pct FROM products WHERE slug = ?",
        (slug,)
    ).fetchone()
    conn.close()

    if row is None:
        abort(404)

    benefits_list = [b.strip("• ").strip() for b in row["benefits"].split("\n") if b.strip()]

    if request.method == "GET":
        return render_template("product.html", product=row, benefits=benefits_list)

    # POST: calculate + require email
    email = (request.form.get("email") or "").strip().lower()
    if "@" not in email or "." not in email:
        return render_template("product.html", product=row, benefits=benefits_list, error="Enter a valid email.")

    # ✅ temp password coming from JS hidden input
    temp_password = (request.form.get("temp_password") or "").strip()

    electricity_kwh = parse_float(request.form.get("electricity_kwh"), 0.0)
    gas_kwh = parse_float(request.form.get("gas_kwh"), 0.0)
    home_size = request.form.get("home_size") or "Small"
    occupants = parse_int(request.form.get("occupants"), 1)
    ev_charging = 1 if request.form.get("ev_charging") == "yes" else 0
    smart_home = 1 if request.form.get("smart_home") == "on" else 0

    kwh_saved, cost_saved, co2_saved = calculate_savings(
        slug, electricity_kwh, gas_kwh, ev_charging, smart_home
    )

    # ✅ Create user (if needed) using the JS temp password
    user_id, created = create_user_if_needed(email, temp_password)
    # ✅ log the user in (so /book works)
    session["user_id"] = user_id
    session["email"] = email
    
    # Store calculation (optionally store user_id too if you added the column)
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO calculations(
            created_at, product_slug, email, user_id,
            electricity_kwh, gas_kwh, home_size, occupants, ev_charging, smart_home,
            kwh_saved, cost_saved, co2_saved
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        slug, email, user_id,
        electricity_kwh, gas_kwh, home_size, occupants, ev_charging, smart_home,
        kwh_saved, cost_saved, co2_saved
    ))
    calc_id = c.lastrowid
    conn.commit()
    conn.close()

    # ❌ Do NOT send email from Flask (403 in your logs)
    # Email is sent in the browser via EmailJS sendForm()

    return redirect(url_for("book", calc_id=calc_id))

@app.route("/book/<int:calc_id>", methods=["GET", "POST"])
def book(calc_id):
    uid = session["user_id"]
    conn = get_db()
    calc = conn.execute("""
        SELECT c.*, p.name AS product_name
        FROM calculations c
        JOIN products p ON p.slug = c.product_slug
        WHERE c.id = ? AND c.user_id = ?
    """, (calc_id,uid)).fetchone()

    if calc is None:
        conn.close()
        abort(404)

    if request.method == "GET":
        conn.close()
        return render_template("booking.html", calc=calc)

    # POST booking
    full_name = (request.form.get("full_name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    preferred_date = request.form.get("preferred_date") or ""
    preferred_time = request.form.get("preferred_time") or ""
    notes = (request.form.get("notes") or "").strip()

    if not full_name or not preferred_date or not preferred_time:
        conn.close()
        return render_template("booking.html", calc=calc, error="Fill in name, date and time.")

    c = conn.cursor()
    c.execute("""
        INSERT INTO bookings(created_at, calc_id, user_id, full_name, phone, preferred_date, preferred_time, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.utcnow().isoformat(),
        calc_id, uid, full_name, phone, preferred_date, preferred_time, notes
    ))

    conn.commit()
    conn.close()
    return render_template("thanks.html", name=full_name)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("register.html")

    email = (request.form.get("email") or "").strip().lower()
    password = (request.form.get("password") or "").strip()

    if "@" not in email or "." not in email:
        return render_template("register.html", error="Enter a valid email.")
    if len(password) < 8:
        return render_template("register.html", error="Password must be at least 8 characters.")

    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users(username, password, must_change_password) VALUES (?, ?, 0)",
            (email, generate_password_hash(password))
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return render_template("register.html", error="That email is already registered.")
    conn.close()

    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", next=request.args.get("next", ""))

    email = (request.form.get("email") or "").strip().lower()
    password = (request.form.get("password") or "").strip()
    next_url = request.form.get("next") or ""

    conn = get_db()
    user = conn.execute("SELECT id, username, password, must_change_password FROM users WHERE username = ?", (email,)).fetchone()
    conn.close()

    if not user or not check_password_hash(user["password"], password):
        return render_template("login.html", error="Invalid email or password.", next=next_url)

    session["user_id"] = user["user_id"]
    session["email"] = user["username"]

    if user["must_change_password"] == 1:
        return redirect(url_for("change_password"))

    return redirect(next_url or url_for("account"))


@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    if request.method == "GET":
        return render_template("change_password.html")

    new_password = (request.form.get("new_password") or "").strip()
    if len(new_password) < 8:
        return render_template("change_password.html", error="Password must be at least 8 characters.")

    conn = get_db()
    conn.execute(
        "UPDATE users SET password = ?, must_change_password = 0 WHERE id = ?",
        (generate_password_hash(new_password), session["user_id"])
    )
    conn.commit()
    conn.close()

    return redirect(url_for("account"))


@app.route("/account")
def account():
    uid = session["user_id"]
    conn = get_db()

    calcs = conn.execute("""
        SELECT c.*, p.name AS product_name
        FROM calculations c
        JOIN products p ON p.slug = c.product_slug
        WHERE c.user_id = ?
        ORDER BY c.id DESC
    """, (uid,)).fetchall()

    bookings = conn.execute("""
        SELECT b.*, p.name AS product_name
        FROM bookings b
        JOIN calculations c ON c.id = b.calc_id
        JOIN products p ON p.slug = c.product_slug
        WHERE b.user_id = ?
        ORDER BY b.id DESC
    """, (uid,)).fetchall()

    conn.close()
    return render_template("account.html", calcs=calcs, bookings=bookings)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


if __name__ == "__main__":
    init_db()
    seed_products()
    app.run(host='0.0.0.0', port=5000, debug=True)
    # app.run(debug=True)
