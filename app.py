from flask import Flask, request, redirect, url_for, render_template_string, session, flash, send_file
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta
import requests
from bs4 import BeautifulSoup
import random
import os
import csv
from io import StringIO
from zipfile import ZipFile
from io import BytesIO

app = Flask(__name__)
app.secret_key = 'SLRACINGsuper_random_key_2026_smxfantasy!@#123'  # CHANGE THIS!

DATABASE_URL = os.environ.get('DATABASE_URL')

SCHEDULE = [
    {'round': 1, 'date': '2026-01-10', 'location': 'Anaheim, CA'},
    {'round': 2, 'date': '2026-01-17', 'location': 'San Diego, CA'},
    {'round': 3, 'date': '2026-01-24', 'location': 'Anaheim, CA'},
    {'round': 4, 'date': '2026-01-31', 'location': 'Houston, TX'},
    {'round': 5, 'date': '2026-02-07', 'location': 'Glendale, AZ'},
    {'round': 6, 'date': '2026-02-14', 'location': 'Seattle, WA'},
    {'round': 7, 'date': '2026-02-21', 'location': 'Arlington, TX'},
    {'round': 8, 'date': '2026-02-28', 'location': 'Daytona Beach, FL'},
    {'round': 9, 'date': '2026-03-07', 'location': 'Indianapolis, IN'},
]

RIDERS_450 = [
    'Chase Sexton', 'Cooper Webb', 'Eli Tomac', 'Hunter Lawrence', 'Jett Lawrence',
    'Ken Roczen', 'Jason Anderson', 'Aaron Plessinger', 'Malcolm Stewart', 'Dylan Ferrandis',
    'Justin Barcia', 'Jorge Prado', 'RJ Hampshire', 'Garrett Marchbanks', 'Christian Craig'
]

RIDERS_250 = [
    'Haiden Deegan', 'Levi Kitchen', 'Chance Hymas', 'Ryder DiFrancesco', 'Max Anstie',
    'Cameron McAdoo', 'Nate Thrasher', 'Jalek Swoll', 'Casey Cochran', 'Daxton Bennick',
    'Pierce Brown', 'Seth Hammaker', 'Julien Beaumer', 'Tom Vialle'
]

def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (id SERIAL PRIMARY KEY, username TEXT UNIQUE, password TEXT, email TEXT UNIQUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS picks 
                 (id SERIAL PRIMARY KEY, user_id INTEGER, round_num INTEGER, class TEXT, rider TEXT, auto_random INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS results 
                 (id SERIAL PRIMARY KEY, round_num INTEGER, class TEXT, rider TEXT, position INTEGER)''')
    conn.commit()
    conn.close()

init_db()

# (All other functions: get_points, get_initials, get_current_round, get_deadline_for_round, normalize_rider, get_event_id, get_overall_url, parse_results - same as before)

# Routes with get_db_connection() instead of sqlite3
@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE username = %s', (username,))
        user = c.fetchone()
        conn.close()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            session['username'] = username
            return redirect(url_for('dashboard'))
        flash('Invalid credentials')
    return render_template_string('''
    <h2>SuperMotocross Fantasy League</h2>
    <h3>Login</h3>
    <form method="post">
        Username: <input name="username" required><br><br>
        Password: <input type="password" name="password" required><br><br>
        <input type="submit" value="Login">
    </form>
    <br><a href="/register">New? Register here</a>
    ''')

# (Other routes like register, dashboard, pick, fetch_results, admin, admin_users, rules, leaderboard - update conn = get_db_connection(), c.execute with %s placeholders, fetchone/fetchall as dicts)

# New Export Route (Admin Only)
@app.route('/admin/export')
def admin_export():
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    c = conn.cursor()
    
    # Get all tables
    tables = ['users', 'picks', 'results']
    
    # Create ZIP in memory
    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, 'w') as zip_file:
        for table in tables:
            c.execute(f'SELECT * FROM {table}')
            rows = c.fetchall()
            csv_buffer = StringIO()
            csv_writer = csv.writer(csv_buffer)
            csv_writer.writerow(rows[0].keys() if rows else [])
            for row in rows:
                csv_writer.writerow(row.values())
            zip_file.writestr(f'{table}.csv', csv_buffer.getvalue())
    
    conn.close()
    
    zip_buffer.seek(0)
    return send_file(zip_buffer, as_attachment=True, download_name='fantasy_league_export.zip')

# Add link to dashboard admin section:
# <p><a href="/admin/export">Export DB to CSV</a></p>

# Update dashboard template to include it under Admin Tools

# Logout same

if __name__ == '__main__':
    app.run(debug=True)
