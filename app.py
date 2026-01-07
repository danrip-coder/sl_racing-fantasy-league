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
from io import StringIO, BytesIO
from zipfile import ZipFile

app = Flask(__name__)
app.secret_key = 'SLRACING_25102024_Finke'  # CHANGE THIS ON RENDER!

# PostgreSQL connection
DATABASE_URL = os.environ.get('DATABASE_URL')

# 2026 Supercross Schedule - only race dates needed (deadline auto-calculated)
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
    {'round': 10, 'date': '2026-03-21', 'location': 'Birmingham, AL'},
    {'round': 11, 'date': '2026-03-28', 'location': 'Detroit, MI'},
    {'round': 12, 'date': '2026-04-04', 'location': 'St.Louis, MO'},
    {'round': 13, 'date': '2026-04-11', 'location': 'Nashville, TN'},
    {'round': 14, 'date': '2026-04-18', 'location': 'Cleveland, OH'},
    {'round': 15, 'date': '2026-04-25', 'location': 'Philadelphia, PA'},
    {'round': 16, 'date': '2026-05-02', 'location': 'Denver, CO'},
    {'round': 17, 'date': '2026-05-09', 'location': 'Salt Lake City, UT'},
    # Add future rounds here as dates are announced
]

RIDERS_450 = [
    'Chase Sexton', 'Cooper Webb', 'Eli Tomac', 'Hunter Lawrence', 'Jett Lawrence',
    'Ken Roczen', 'Jason Anderson', 'Aaron Plessinger', 'Malcolm Stewart', 'Dylan Ferrandis',
    'Justin Barcia', 'Jorge Prado', 'RJ Hampshire', 'Garrett Marchbanks', 'Christian Craig', 'Joey Savatgy',
    'Justin Cooper', 'Austin Forkner'  
]

RIDERS_250 = [
    'Haiden Deegan', 'Levi Kitchen', 'Chance Hymas', 'Ryder DiFrancesco', 'Max Anstie',
    'Cameron McAdoo', 'Nate Thrasher', 'Jalek Swoll', 'Casey Cochran', 'Daxton Bennick',
    'Pierce Brown', 'Seth Hammaker', 'Julien Beaumer', 'Tom Vialle', 'Max Vohland', 'Michael Mosiman', 
    'Parker Ross', 'Carson Mumford' 
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

def get_points(position):
    if position == 1: return 25
    elif position == 2: return 22
    elif position == 3: return 20
    elif position == 4: return 18
    elif position == 5: return 16
    elif position <= 22: return 23 - position
    else: return 0

def get_initials(name):
    parts = name.split()
    return ''.join(p[0].upper() for p in parts if p)

def get_current_round():
    now = datetime.now()
    for i, s in enumerate(SCHEDULE):
        race_date = datetime.strptime(s['date'], '%Y-%m-%d')
        if now < race_date:
            return i + 1
    return len(SCHEDULE) + 1

def get_deadline_for_round(round_num):
    sched = next((s for s in SCHEDULE if s['round'] == round_num), None)
    if not sched:
        return None
    race_date = datetime.strptime(sched['date'], '%Y-%m-%d')
    deadline = race_date - timedelta(days=1)
    deadline = deadline.replace(hour=23, minute=59, second=59)
    return deadline

def normalize_rider(name):
    return ' '.join(word.capitalize() for word in name.strip().lower().split())

def get_event_id(round_num):
    sched = next((s for s in SCHEDULE if s['round'] == round_num), None)
    if not sched: return None
    target_date_str = datetime.strptime(sched['date'], '%Y-%m-%d').strftime('%b %d, %Y')
    url = 'https://results.supermotocross.com/'
    response = requests.get(url)
    if response.status_code != 200: return None
    soup = BeautifulSoup(response.text, 'html.parser')
    table = soup.find('table')
    if not table: return None
    rows = table.find_all('tr')[1:]
    for row in rows:
        cols = row.find_all('td')
        if len(cols) < 2: continue
        link = cols[0].find('a')
        if not link: continue
        event_name = link.text.strip()
        event_date = cols[1].text.strip()
        if target_date_str in event_date and sched['location'].split(',')[0] in event_name:
            return link['href'].split('id=')[-1]
    return None

def get_overall_url(event_id, cls):
    url = f'https://results.supermotocross.com/results/?p=view_event&id={event_id}'
    response = requests.get(url)
    if response.status_code != 200: return None
    soup = BeautifulSoup(response.text, 'html.parser')
    for a in soup.find_all('a'):
        if f'{cls} Overall Results' in a.text:
            return 'https://results.supermotocross.com' + a['href']
    return None

def parse_results(url, cls):
    response = requests.get(url)
    if response.status_code != 200: return {}
    soup = BeautifulSoup(response.text, 'html.parser')
    table = soup.find('table')
    if not table: return {}
    rows = table.find_all('tr')[1:]
    results = {}
    riders = RIDERS_450 if cls == '450' else RIDERS_250
    for row in rows:
        cols = row.find_all('td')
        if len(cols) < 3: continue
        try:
            pos = int(cols[0].text.strip())
            name = normalize_rider(cols[2].text.strip())
            if name in riders:
                results[name] = pos
        except: pass
    return results

def get_base_style():
    return '''
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen', 'Ubuntu', sans-serif;
            background: #1a1a1a;
            min-height: 100vh;
            padding: 20px;
            color: #e8e8e8;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: #2d2d2d;
            border-radius: 12px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.4);
            padding: 40px;
            border: 1px solid #3d3d3d;
        }
        h1, h2, h3 {
            color: #f5f5f5;
            margin-bottom: 20px;
        }
        h1 { font-size: 2.5em; border-bottom: 3px solid #c9975b; padding-bottom: 15px; }
        h2 { font-size: 2em; color: #c9975b; }
        h3 { font-size: 1.5em; color: #d4a574; margin-top: 30px; }
        .btn {
            background: #c9975b;
            color: #1a1a1a;
            padding: 12px 30px;
            border: none;
            border-radius: 6px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            text-decoration: none;
            display: inline-block;
            transition: all 0.2s;
            margin: 5px;
        }
        .btn:hover {
            background: #d4a574;
            transform: translateY(-1px);
            box-shadow: 0 4px 12px rgba(201, 151, 91, 0.3);
        }
        .btn-danger {
            background: #e74c3c;
            color: white;
        }
        .btn-danger:hover {
            background: #c0392b;
        }
        .btn-small {
            padding: 8px 16px;
            font-size: 14px;
        }
        input[type="text"], input[type="password"], input[type="email"], input[type="number"], select {
            width: 100%;
            padding: 12px;
            border: 1px solid #4a4a4a;
            border-radius: 6px;
            font-size: 16px;
            margin: 8px 0;
            background: #3a3a3a;
            color: #e8e8e8;
            transition: border-color 0.3s;
        }
        input:focus, select:focus {
            outline: none;
            border-color: #c9975b;
            background: #404040;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin: 20px 0;
            border-radius: 8px;
            overflow: hidden;
            border: 1px solid #3d3d3d;
        }
        th {
            background: #3d3d3d;
            color: #c9975b;
            padding: 15px;
            text-align: left;
            font-weight: 600;
            border-bottom: 2px solid #c9975b;
        }
        td {
            padding: 12px 15px;
            border-bottom: 1px solid #3d3d3d;
            color: #d0d0d0;
        }
        tr:hover {
            background-color: #353535;
        }
        tbody tr:nth-child(even) {
            background-color: #2a2a2a;
        }
        tbody tr:nth-child(odd) {
            background-color: #2d2d2d;
        }
        .flash {
            padding: 15px 20px;
            margin: 20px 0;
            border-radius: 6px;
            background: #27ae60;
            color: white;
            font-weight: 500;
            border-left: 4px solid #2ecc71;
        }
        .link {
            color: #c9975b;
            text-decoration: none;
            font-weight: 600;
            transition: color 0.3s;
        }
        .link:hover {
            color: #d4a574;
            text-decoration: underline;
        }
        .card {
            background: #363636;
            border-radius: 8px;
            padding: 20px;
            margin: 15px 0;
            border-left: 4px solid #c9975b;
            border: 1px solid #3d3d3d;
        }
        .random-pick {
            color: #e74c3c;
            font-weight: 600;
        }
        .dashboard-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin: 30px 0;
        }
        .dashboard-card {
            background: #363636;
            color: #e8e8e8;
            padding: 30px;
            border-radius: 8px;
            text-align: center;
            transition: all 0.3s;
            cursor: pointer;
            border: 2px solid #4a4a4a;
        }
        .dashboard-card:hover {
            transform: translateY(-3px);
            box-shadow: 0 8px 20px rgba(201, 151, 91, 0.2);
            border-color: #c9975b;
        }
        .dashboard-card h3 {
            color: #c9975b;
            margin: 10px 0 0 0;
        }
        .dashboard-card p {
            color: #b0b0b0;
            margin-top: 5px;
        }
        hr {
            border: none;
            border-top: 1px solid #4a4a4a;
            margin: 30px 0;
        }
        label {
            color: #d0d0d0;
            font-weight: 500;
        }
        .countdown-timer {
            background: #363636;
            border: 2px solid #c9975b;
            border-radius: 8px;
            padding: 20px;
            margin: 20px 0;
            text-align: center;
        }
        .countdown-timer h3 {
            color: #c9975b;
            margin: 0 0 15px 0;
        }
        .countdown-display {
            display: flex;
            justify-content: center;
            gap: 20px;
            flex-wrap: wrap;
        }
        .countdown-unit {
            background: #2d2d2d;
            border-radius: 6px;
            padding: 15px 20px;
            min-width: 80px;
        }
        .countdown-number {
            font-size: 2em;
            font-weight: bold;
            color: #c9975b;
            display: block;
        }
        .countdown-label {
            font-size: 0.9em;
            color: #b0b0b0;
            display: block;
            margin-top: 5px;
        }
        .countdown-expired {
            color: #e74c3c;
            font-size: 1.2em;
            font-weight: 600;
        }
    </style>
    '''

def get_round_location(round_num):
    """Get the location name for a given round"""
    sched = next((s for s in SCHEDULE if s['round'] == round_num), None)
    if sched:
        return sched['location'].split(',')[0]
    return ""

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
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>🏍️ SL Racing SMX Tipping Comp</h1>
        <div class="card">
            <h2>Login</h2>
            {% with messages = get_flashed_messages() %}
                {% if messages %}
                    {% for message in messages %}
                        <div class="flash">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            <form method="post">
                <label><strong>Username</strong></label>
                <input type="text" name="username" required>
                
                <label><strong>Password</strong></label>
                <input type="password" name="password" required>
                
                <button type="submit" class="btn">Login</button>
            </form>
            <p style="margin-top: 20px;">
                <a href="/register" class="link">New here? Register now!</a>
            </p>
        </div>
    </div>
    ''')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        email = request.form['email']
        password = generate_password_hash(request.form['password'])
        conn = get_db_connection()
        c = conn.cursor()
        try:
            c.execute('INSERT INTO users (username, password, email) VALUES (%s, %s, %s)', (username, password, email))
            conn.commit()
            flash('Registered! You can now login.')
            conn.close()
            return redirect(url_for('login'))
        except psycopg2.IntegrityError:
            flash('Username or email already taken')
            conn.close()
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>🏍️ Register for SL Racing SMX Tipping Comp</h1>
        <div class="card">
            {% with messages = get_flashed_messages() %}
                {% if messages %}
                    {% for message in messages %}
                        <div class="flash">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            <form method="post">
                <label><strong>Username</strong></label>
                <input type="text" name="username" required>
                
                <label><strong>Email</strong></label>
                <input type="email" name="email" required>
                
                <label><strong>Password</strong></label>
                <input type="password" name="password" required>
                
                <button type="submit" class="btn">Register</button>
            </form>
            <p style="margin-top: 20px;">
                <a href="/" class="link">← Back to Login</a>
            </p>
        </div>
    </div>
    ''')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    current_round = get_current_round()
    location = get_round_location(current_round)
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>Welcome, {{ username }}! 🏁</h1>
        <p style="font-size: 1.2em; color: #b0b0b0; margin-bottom: 30px;">
            <strong>Current Round:</strong> {{ current_round }} {{ location }}
        </p>
        
        <div class="dashboard-grid">
            <a href="/pick/{{ current_round }}" style="text-decoration: none;">
                <div class="dashboard-card">
                    <h3>🏍️</h3>
                    <h3>Make Picks</h3>
                    <p>Round {{ current_round }} {{ location }}</p>
                </div>
            </a>
            
            <a href="/leaderboard" style="text-decoration: none;">
                <div class="dashboard-card">
                    <h3>🏆</h3>
                    <h3>Leaderboard</h3>
                    <p>View Standings</p>
                </div>
            </a>
            
            <a href="/rules" style="text-decoration: none;">
                <div class="dashboard-card">
                    <h3>📋</h3>
                    <h3>Rules</h3>
                    <p>How to Play</p>
                </div>
            </a>
        </div>
        
        {% if username == 'admin' %}
        <hr>
        <h3>🔧 Admin Tools</h3>
        <div style="margin: 20px 0;">
            <a href="/fetch_results/{{ current_round }}" class="btn btn-small">Auto-Fetch Results (R{{ current_round }})</a>
            <a href="/admin/{{ current_round }}" class="btn btn-small">Manual Results Entry</a>
            <a href="/admin/users" class="btn btn-small">Manage Users</a>
            <a href="/admin/export" class="btn btn-small">Export Database</a>
        </div>
        {% endif %}
        
        <div style="margin-top: 40px;">
            <a href="/logout" class="link">Logout</a>
        </div>
    </div>
    ''', username=session['username'], current_round=current_round, location=location)

@app.route('/pick/<int:round_num>', methods=['GET', 'POST'])
def pick(round_num):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    sched = next((s for s in SCHEDULE if s['round'] == round_num), None)
    if not sched:
        flash('Invalid round')
        return redirect(url_for('dashboard'))
    
    deadline = get_deadline_for_round(round_num)
    deadline_passed = datetime.now() > deadline
    
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute('SELECT class, rider, auto_random FROM picks WHERE user_id = %s AND round_num = %s', 
              (session['user_id'], round_num))
    existing = c.fetchall()
    existing_picks = {row['class']: (row['rider'], row['auto_random']) for row in existing}
    
    # Get ALL players' picks for this round (including current user)
    c.execute('''SELECT u.username, p.class, p.rider, p.auto_random 
                 FROM picks p 
                 JOIN users u ON p.user_id = u.id 
                 WHERE p.round_num = %s 
                 ORDER BY u.username, p.class''', 
              (round_num,))
    all_picks_raw = c.fetchall()
    
    # Organize all players' picks by username
    all_players_picks = {}
    for pick in all_picks_raw:
        username = pick['username']
        if username not in all_players_picks:
            all_players_picks[username] = {'450': None, '250': None}
        all_players_picks[username][pick['class']] = {
            'rider': pick['rider'],
            'auto_random': pick['auto_random']
        }
    
    if request.method == 'POST' and not deadline_passed:
        rider_450 = request.form.get('rider_450')
        rider_250 = request.form.get('rider_250')
        
        if not rider_450 or not rider_250:
            flash('Must select one rider from each class')
        elif rider_450 not in RIDERS_450 or rider_250 not in RIDERS_250:
            flash('Invalid rider')
        else:
            c.execute('SELECT rider FROM picks WHERE user_id = %s AND class = %s AND round_num IN (%s, %s)',
                      (session['user_id'], '450', round_num-1, round_num-2))
            if rider_450 in [r['rider'] for r in c.fetchall()]:
                flash('Cannot pick the same 450 rider within 3 rounds')
            else:
                c.execute('SELECT rider FROM picks WHERE user_id = %s AND class = %s AND round_num IN (%s, %s)',
                          (session['user_id'], '250', round_num-1, round_num-2))
                if rider_250 in [r['rider'] for r in c.fetchall()]:
                    flash('Cannot pick the same 250 rider within 3 rounds')
                else:
                    c.execute('DELETE FROM picks WHERE user_id = %s AND round_num = %s', (session['user_id'], round_num))
                    c.execute('INSERT INTO picks (user_id, round_num, class, rider, auto_random) VALUES (%s, %s, %s, %s, %s)',
                              (session['user_id'], round_num, '450', rider_450, 0))
                    c.execute('INSERT INTO picks (user_id, round_num, class, rider, auto_random) VALUES (%s, %s, %s, %s, %s)',
                              (session['user_id'], round_num, '250', rider_250, 0))
                    conn.commit()
                    flash('Picks saved successfully!')
                    return redirect(url_for('dashboard'))
    
    elif deadline_passed and len(existing_picks) == 0:
        random_450 = random.choice(RIDERS_450)
        random_250 = random.choice(RIDERS_250)
        
        c.execute('INSERT INTO picks (user_id, round_num, class, rider, auto_random) VALUES (%s, %s, %s, %s, %s)',
                  (session['user_id'], round_num, '450', random_450, 1))
        c.execute('INSERT INTO picks (user_id, round_num, class, rider, auto_random) VALUES (%s, %s, %s, %s, %s)',
                  (session['user_id'], round_num, '250', random_250, 1))
        conn.commit()
        
        flash(f'No picks submitted — random riders auto-assigned: {random_450} (450) and {random_250} (250)')
        existing_picks = {'450': (random_450, 1), '250': (random_250, 1)}
    
    conn.close()
    
    message = ""
    if deadline_passed:
        message = f"Picks locked (deadline was midnight { (deadline - timedelta(days=1)).strftime('%B %d') })."
        if any(existing_picks.get(cls, (None, 0))[1] for cls in ['450', '250']):
            message += " <strong style='color:#e74c3c;'>Random picks applied.</strong>"
    
    location = get_round_location(round_num)
    
    # Get deadline in ISO format for JavaScript
    deadline_iso = deadline.isoformat() if deadline else None
    
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h2>🏍️ Round {{ round_num }} {{ location }} - Picks</h2>
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="flash">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        {% if message %}
            <div class="card">
                <p style="font-weight:bold;">{{ message | safe }}</p>
            </div>
        {% endif %}
        
        {% if not deadline_passed and deadline_iso %}
        <div class="countdown-timer">
            <h3>⏰ Picks Lock In:</h3>
            <div class="countdown-display" id="countdown">
                <div class="countdown-unit">
                    <span class="countdown-number" id="days">--</span>
                    <span class="countdown-label">Days</span>
                </div>
                <div class="countdown-unit">
                    <span class="countdown-number" id="hours">--</span>
                    <span class="countdown-label">Hours</span>
                </div>
                <div class="countdown-unit">
                    <span class="countdown-number" id="minutes">--</span>
                    <span class="countdown-label">Minutes</span>
                </div>
                <div class="countdown-unit">
                    <span class="countdown-number" id="seconds">--</span>
                    <span class="countdown-label">Seconds</span>
                </div>
            </div>
        </div>
        <script>
            const deadline = new Date("{{ deadline_iso }}");
            
            function updateCountdown() {
                const now = new Date();
                const diff = deadline - now;
                
                if (diff <= 0) {
                    document.getElementById('countdown').innerHTML = '<p class="countdown-expired">Picks are now locked!</p>';
                    setTimeout(() => location.reload(), 2000);
                    return;
                }
                
                const days = Math.floor(diff / (1000 * 60 * 60 * 24));
                const hours = Math.floor((diff % (1000 * 60 * 60 * 24)) / (1000 * 60 * 60));
                const minutes = Math.floor((diff % (1000 * 60 * 60)) / (1000 * 60));
                const seconds = Math.floor((diff % (1000 * 60)) / 1000);
                
                document.getElementById('days').textContent = days.toString().padStart(2, '0');
                document.getElementById('hours').textContent = hours.toString().padStart(2, '0');
                document.getElementById('minutes').textContent = minutes.toString().padStart(2, '0');
                document.getElementById('seconds').textContent = seconds.toString().padStart(2, '0');
            }
            
            updateCountdown();
            setInterval(updateCountdown, 1000);
        </script>
