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
app.secret_key = 'change_this_to_a_long_random_string_right_now!'  # CHANGE THIS ON RENDER!

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
    'Christian Craig', 'Justin Cooper', 'Austin Forkner'  
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
        {% endif %}
        
        {% if deadline_passed %}
            <div class="card">
                <h3 style="margin-top: 0;">Your Picks (Locked)</h3>
                <p><strong>450 Class:</strong> {{ existing_picks['450'][0] if '450' in existing_picks else 'None' }}
                    {% if '450' in existing_picks and existing_picks['450'][1] %} 
                        <span class="random-pick">(Random)</span>
                    {% endif %}
                </p>
                <p><strong>250 Class:</strong> {{ existing_picks['250'][0] if '250' in existing_picks else 'None' }}
                    {% if '250' in existing_picks and existing_picks['250'][1] %} 
                        <span class="random-pick">(Random)</span>
                    {% endif %}
                </p>
            </div>
        {% else %}
            <form method="post">
                <div class="card">
                    <h3 style="margin-top: 0;">{% if existing_picks %}Update Your Picks{% else %}Make Your Picks{% endif %}</h3>
                    <label><strong>450 Class Rider</strong></label>
                    <select name="rider_450">
                        {% for r in riders_450 %}
                        <option {% if '450' in existing_picks and existing_picks['450'][0]==r %}selected{% endif %}>{{ r }}</option>
                        {% endfor %}
                    </select>
                    
                    <label><strong>250 Class Rider</strong></label>
                    <select name="rider_250">
                        {% for r in riders_250 %}
                        <option {% if '250' in existing_picks and existing_picks['250'][0]==r %}selected{% endif %}>{{ r }}</option>
                        {% endfor %}
                    </select>
                    
                    <button type="submit" class="btn">{% if existing_picks %}Update Picks{% else %}Save Picks{% endif %}</button>
                </div>
            </form>
        {% endif %}
        
        {% if all_players_picks %}
        <hr>
        <h3>Current Round Picks</h3>
        <table>
            <thead>
                <tr>
                    <th>Player</th>
                    <th>450 Class</th>
                    <th>250 Class</th>
                </tr>
            </thead>
            <tbody>
                {% for player, picks in all_players_picks.items() %}
                <tr {% if player == session.username %}style="background: #3d3d3d; border-left: 3px solid #c9975b;"{% endif %}>
                    <td style="font-weight: 600;">
                        {{ player }}
                        {% if player == session.username %}<span style="color: #c9975b;"> (You)</span>{% endif %}
                    </td>
                    <td>
                        {% if picks['450'] %}
                            {{ picks['450']['rider'] }}
                            {% if picks['450']['auto_random'] %}
                                <span class="random-pick">(Random)</span>
                            {% endif %}
                        {% else %}
                            <span style="color:#999;">Not picked yet</span>
                        {% endif %}
                    </td>
                    <td>
                        {% if picks['250'] %}
                            {{ picks['250']['rider'] }}
                            {% if picks['250']['auto_random'] %}
                                <span class="random-pick">(Random)</span>
                            {% endif %}
                        {% else %}
                            <span style="color:#999;">Not picked yet</span>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% else %}
        <hr>
        <p style="color: #b0b0b0; font-style: italic;">No picks have been made for this round yet.</p>
        {% endif %}
        
        <div style="margin-top: 30px;">
            <a href="/dashboard" class="link">← Back to Dashboard</a>
        </div>
    </div>
    ''', round_num=round_num, riders_450=RIDERS_450, riders_250=RIDERS_250,
         existing_picks=existing_picks, message=message, deadline_passed=deadline_passed,
         all_players_picks=all_players_picks, location=location, session=session, 
         deadline_iso=deadline_iso)

@app.route('/fetch_results/<int:round_num>')
def fetch_results(round_num):
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    event_id = get_event_id(round_num)
    if not event_id:
        flash('Event not found or results not available yet')
        return redirect(url_for('dashboard'))
    conn = get_db_connection()
    c = conn.cursor()
    for cls, riders in [('450', RIDERS_450), ('250', RIDERS_250)]:
        url = get_overall_url(event_id, cls)
        if url:
            results = parse_results(url, cls)
            for rider, pos in results.items():
                c.execute('INSERT INTO results (round_num, class, rider, position) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING',
                          (round_num, cls, rider, pos))
    conn.commit()
    conn.close()
    flash('Results successfully auto-fetched!')
    return redirect(url_for('dashboard'))

@app.route('/admin/<int:round_num>', methods=['GET', 'POST'])
def admin(round_num):
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    if request.method == 'POST':
        conn = get_db_connection()
        c = conn.cursor()
        for cls, riders in [('450', RIDERS_450), ('250', RIDERS_250)]:
            for rider in riders:
                pos_str = request.form.get(f'{cls}_{rider.replace(" ", "_")}')
                if pos_str and pos_str.isdigit():
                    c.execute('INSERT INTO results (round_num, class, rider, position) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING',
                              (round_num, cls, rider, int(pos_str)))
        conn.commit()
        conn.close()
        flash('Manual results saved')
    
    location = get_round_location(round_num)
    
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>🔧 Manual Results Entry - Round {{ round_num }} {{ location }}</h1>
        
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="flash">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        <form method="post">
            {% for cls, riders in [('450', RIDERS_450), ('250', RIDERS_250)] %}
            <div class="card">
                <h3 style="margin-top: 0;">{{ cls }} Class Results</h3>
                <div style="display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 15px;">
                    {% for r in riders %}
                    <div>
                        <label style="font-weight: 600;">{{ r }}</label>
                        <input name="{{ cls }}_{{ r.replace(' ', '_') }}" type="number" min="1" placeholder="Position">
                    </div>
                    {% endfor %}
                </div>
            </div>
            {% endfor %}
            
            <button type="submit" class="btn">Save Results</button>
        </form>
        
        <div style="margin-top: 30px;">
            <a href="/dashboard" class="link">← Back to Dashboard</a>
        </div>
    </div>
    ''', round_num=round_num, RIDERS_450=RIDERS_450, RIDERS_250=RIDERS_250, location=location)

@app.route('/admin/users', methods=['GET', 'POST'])
def admin_users():
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    conn = get_db_connection()
    c = conn.cursor()
    if request.method == 'POST':
        action = request.form.get('action')
        user_id = request.form['user_id']
        
        if action == 'reset_password':
            new_pass = generate_password_hash(request.form['new_password'])
            c.execute('UPDATE users SET password = %s WHERE id = %s', (new_pass, user_id))
            conn.commit()
            flash('Password reset successfully!')
        elif action == 'delete_user':
            # Prevent admin from deleting themselves
            if int(user_id) == session.get('user_id'):
                flash('Cannot delete your own admin account!')
            else:
                # Delete user's picks first (foreign key constraint)
                c.execute('DELETE FROM picks WHERE user_id = %s', (user_id,))
                # Delete the user
                c.execute('DELETE FROM users WHERE id = %s', (user_id,))
                conn.commit()
                flash('User deleted successfully!')
    
    c.execute('SELECT id, username, email FROM users ORDER BY username')
    users = c.fetchall()
    conn.close()
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>🔧 Manage Users</h1>
        
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="flash">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Username</th>
                    <th>Email</th>
                    <th>Reset Password</th>
                    <th>Delete</th>
                </tr>
            </thead>
            <tbody>
                {% for user in users %}
                <tr>
                    <td>{{ user['id'] }}</td>
                    <td style="font-weight: 600;">{{ user['username'] }}</td>
                    <td>{{ user['email'] or 'No email' }}</td>
                    <td>
                        <form method="post" style="display: flex; gap: 10px; align-items: center;">
                            <input type="hidden" name="action" value="reset_password">
                            <input type="hidden" name="user_id" value="{{ user['id'] }}">
                            <input type="password" name="new_password" placeholder="New password" required 
                                   style="width: 150px; margin: 0;">
                            <button type="submit" class="btn btn-small">Reset</button>
                        </form>
                    </td>
                    <td>
                        <form method="post" style="display:inline;" 
                              onsubmit="return confirm('Are you sure you want to delete {{ user['username'] }}? This will also delete all their picks.');">
                            <input type="hidden" name="action" value="delete_user">
                            <input type="hidden" name="user_id" value="{{ user['id'] }}">
                            <button type="submit" class="btn btn-small btn-danger">Delete</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        
        <div style="margin-top: 30px;">
            <a href="/dashboard" class="link">← Back to Dashboard</a>
        </div>
    </div>
    ''', users=users)

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
            # Write headers
            csv_writer.writerow(rows[0].keys() if rows else [])
            # Write data
            for row in rows:
                csv_writer.writerow(row.values())
            zip_file.writestr(f'{table}.csv', csv_buffer.getvalue())
    
    conn.close()
    
    zip_buffer.seek(0)
    return send_file(zip_buffer, as_attachment=True, download_name='fantasy_league_export.zip', mimetype='application/zip')

@app.route('/rules')
def rules():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>📋 Tipping Comp Rules</h1>
        
        <div class="card">
            <h3 style="margin-top: 0;">🏍️ How to Play</h3>
            <ul style="line-height: 1.8; margin-left: 20px;">
                <li>Pick <strong>ONE rider from 450</strong> and <strong>ONE from 250</strong> each round</li>
                <li>You must pick riders from both classes</li>
                <li>Picks lock at <strong>midnight the night before each race</strong></li>
            </ul>
        </div>
        
        <div class="card">
            <h3 style="margin-top: 0;">⏰ Missed Picks?</h3>
            <ul style="line-height: 1.8; margin-left: 20px;">
                <li>If you forget to pick, <strong>random riders will be auto-assigned</strong></li>
                <li>Random picks are shown in <span class="random-pick">red</span> on the leaderboard</li>
            </ul>
        </div>
        
        <div class="card">
            <h3 style="margin-top: 0;">🔄 Repeat Rule</h3>
            <ul style="line-height: 1.8; margin-left: 20px;">
                <li>You cannot pick the same rider (in the same class) within any 3-round window</li>
                <li>This keeps strategy interesting throughout the season!</li>
            </ul>
        </div>
        
        <div class="card">
            <h3 style="margin-top: 0;">🏆 Scoring</h3>
            <table style="margin: 15px 0; box-shadow: none; border: none;">
                <tr><td style="border: none;"><strong>1st Place:</strong></td><td style="border: none;">25 points</td></tr>
                <tr><td style="border: none;"><strong>2nd Place:</strong></td><td style="border: none;">22 points</td></tr>
                <tr><td style="border: none;"><strong>3rd Place:</strong></td><td style="border: none;">20 points</td></tr>
                <tr><td style="border: none;"><strong>4th Place:</strong></td><td style="border: none;">18 points</td></tr>
                <tr><td style="border: none;"><strong>5th Place:</strong></td><td style="border: none;">16 points</td></tr>
                <tr><td style="border: none;"><strong>6th-22nd:</strong></td><td style="border: none;">15 down to 1 point</td></tr>
                <tr><td style="border: none;"><strong>23rd+:</strong></td><td style="border: none;">0 points</td></tr>
            </table>
            <p style="margin-top: 15px;"><strong>Round Score</strong> = 450 pick points + 250 pick points</p>
            <p><strong>Season Winner</strong> = Player with highest total points!</p>
        </div>
        
        <div style="margin-top: 30px;">
            <a href="/dashboard" class="link">← Back to Dashboard</a>
        </div>
    </div>
    ''')

@app.route('/leaderboard')
def leaderboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT id, username FROM users ORDER BY username')
    users = c.fetchall()
    
    completed_rounds = []
    for r in range(1, len(SCHEDULE)+1):
        c.execute('SELECT COUNT(*) as count FROM results WHERE round_num = %s', (r,))
        if c.fetchone()['count'] > 0:
            completed_rounds.append(r)
    
    player_data = []
    for user in users:
        user_id = user['id']
        username = user['username']
        total = 0
        round_picks = {}
        
        for rnd in completed_rounds:
            picks = {'450': ('—', False), '250': ('—', False)}
            for cls in ['450', '250']:
                c.execute('SELECT rider, auto_random FROM picks WHERE user_id = %s AND round_num = %s AND class = %s',
                          (user_id, rnd, cls))
                row = c.fetchone()
                if row:
                    initials = get_initials(row['rider'])
                    picks[cls] = (initials, bool(row['auto_random']))
                    c.execute('SELECT position FROM results WHERE round_num = %s AND class = %s AND rider = %s', 
                              (rnd, cls, row['rider']))
                    pos = c.fetchone()
                    if pos:
                        total += get_points(pos['position'])
            round_picks[rnd] = picks
        
        player_data.append({
            'username': username,
            'total': total,
            'round_picks': round_picks
        })
    
    player_data.sort(key=lambda x: x['total'], reverse=True)
    
    conn.close()
    
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>🏆 Season Leaderboard</h1>
        
        <div style="overflow-x: auto;">
            <table>
                <thead>
                    <tr>
                        <th style="text-align: center;">Rank</th>
                        <th>Player</th>
                        <th style="text-align: center;">Total Points</th>
                        {% for rnd in completed_rounds %}
                        <th style="text-align: center;">R{{ rnd }} {{ get_round_location(rnd) }}<br><small>450 | 250</small></th>
                        {% endfor %}
                    </tr>
                </thead>
                <tbody>
                    {% for i in range(player_data|length) %}
                    {% set player = player_data[i] %}
                    <tr {% if player.username == session.username %}style="background: #3d3d3d; font-weight: 600; border-left: 3px solid #c9975b;"{% endif %}>
                        <td style="text-align: center; font-size: 1.3em; font-weight: bold;">
                            {% if i == 0 %}🥇
                            {% elif i == 1 %}🥈
                            {% elif i == 2 %}🥉
                            {% else %}{{ i+1 }}
                            {% endif %}
                        </td>
                        <td style="font-weight: 600;">{{ player.username }}</td>
                        <td style="text-align: center; font-size: 1.4em; font-weight: bold; color: #c9975b;">
                            {{ player.total }}
                        </td>
                        {% for rnd in completed_rounds %}
                        <td style="text-align: center; font-size: 0.9em;">
                            <span {% if player.round_picks[rnd]['450'][1] %}class="random-pick"{% endif %}>
                                {{ player.round_picks[rnd]['450'][0] }}
                            </span>
                            |
                            <span {% if player.round_picks[rnd]['250'][1] %}class="random-pick"{% endif %}>
                                {{ player.round_picks[rnd]['250'][0] }}
                            </span>
                        </td>
                        {% endfor %}
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        
        <p style="margin-top: 20px; color: #b0b0b0; font-size: 0.9em;">
            <span class="random-pick">Red text</span> = random auto-pick (missed deadline)
        </p>
        
        <div style="margin-top: 30px;">
            <a href="/dashboard" class="link">← Back to Dashboard</a>
        </div>
    </div>
    ''', player_data=player_data, completed_rounds=completed_rounds, session=session, get_round_location=get_round_location)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
