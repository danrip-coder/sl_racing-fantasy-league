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
    # Add future rounds here as dates are announced
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
    return render_template_string('''
    <h2>Register</h2>
    <form method="post">
        Username: <input name="username" required><br><br>
        Email: <input type="email" name="email" required><br><br>
        Password: <input type="password" name="password" required><br><br>
        <input type="submit" value="Register">
    </form>
    <br><a href="/">Back to Login</a>
    ''')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    current_round = get_current_round()
    return render_template_string('''
    <h1>Welcome, {{ username }}!</h1>
    <p><strong>Current Round:</strong> {{ current_round }}</p>
    <p><a href="/pick/{{ current_round }}"><button style="font-size:18px;padding:10px 20px;">Make/Edit Picks for Round {{ current_round }}</button></a></p>
    <p><a href="/leaderboard"><button style="font-size:18px;padding:10px 20px;">View Leaderboard</button></a></p>
    <p><a href="/rules"><button style="font-size:18px;padding:10px 20px;">View Rules</button></a></p>
    {% if username == 'admin' %}
    <hr>
    <h3>Admin Tools</h3>
    <p><a href="/fetch_results/{{ current_round }}">Auto-Fetch Results for Round {{ current_round }}</a></p>
    <p><a href="/admin/{{ current_round }}">Manual Results Entry</a></p>
    <p><a href="/admin/users">Manage Users / Reset Passwords</a></p>
    <p><a href="/admin/export">Export DB to CSV</a></p>
    {% endif %}
    <br><br>
    <a href="/logout">Logout</a>
    ''', username=session['username'], current_round=current_round)

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
    
    # Get all other players' picks for this round
    c.execute('''SELECT u.username, p.class, p.rider, p.auto_random 
                 FROM picks p 
                 JOIN users u ON p.user_id = u.id 
                 WHERE p.round_num = %s AND p.user_id != %s 
                 ORDER BY u.username, p.class''', 
              (round_num, session['user_id']))
    other_picks_raw = c.fetchall()
    
    # Organize other players' picks by username
    other_players_picks = {}
    for pick in other_picks_raw:
        username = pick['username']
        if username not in other_players_picks:
            other_players_picks[username] = {'450': None, '250': None}
        other_players_picks[username][pick['class']] = {
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
            message += " <strong style='color:red;'>Random picks applied.</strong>"
    
    return render_template_string('''
    <h2>Picks - Round {{ round_num }}</h2>
    {% if message %}<p style="font-weight:bold;">{{ message | safe }}</p>{% endif %}
    
    {% if deadline_passed %}
        <p><strong>Your picks:</strong></p>
        <ul>
            <li><strong>450:</strong> {{ existing_picks['450'][0] if '450' in existing_picks else 'None' }}
                {% if '450' in existing_picks and existing_picks['450'][1] %} <span style="color:red;">(Random)</span>{% endif %}</li>
            <li><strong>250:</strong> {{ existing_picks['250'][0] if '250' in existing_picks else 'None' }}
                {% if '250' in existing_picks and existing_picks['250'][1] %} <span style="color:red;">(Random)</span>{% endif %}</li>
        </ul>
    {% else %}
        <form method="post">
            <strong>450 Class:</strong><br>
            <select name="rider_450" style="width:300px;font-size:18px;">
                {% for r in riders_450 %}
                <option {% if '450' in existing_picks and existing_picks['450'][0]==r %}selected{% endif %}>{{ r }}</option>
                {% endfor %}
            </select><br><br>
            
            <strong>250 Class:</strong><br>
            <select name="rider_250" style="width:300px;font-size:18px;">
                {% for r in riders_250 %}
                <option {% if '250' in existing_picks and existing_picks['250'][0]==r %}selected{% endif %}>{{ r }}</option>
                {% endfor %}
            </select><br><br>
            
            <input type="submit" value="Save Picks" style="font-size:18px;padding:10px;">
        </form>
    {% endif %}
    
    {% if other_players_picks %}
    <hr style="margin-top:30px;">
    <h3>Other Players' Picks for Round {{ round_num }}</h3>
    <table border="1" style="border-collapse:collapse; width:90%; text-align:left; margin-top:15px;">
        <tr style="background:#f0f0f0;">
            <th style="padding:8px;">Player</th>
            <th style="padding:8px;">450 Class</th>
            <th style="padding:8px;">250 Class</th>
        </tr>
        {% for player, picks in other_players_picks.items() %}
        <tr>
            <td style="padding:8px; font-weight:bold;">{{ player }}</td>
            <td style="padding:8px;">
                {% if picks['450'] %}
                    {{ picks['450']['rider'] }}
                    {% if picks['450']['auto_random'] %}<span style="color:red; font-size:12px;"> (Random)</span>{% endif %}
                {% else %}
                    <span style="color:#999;">Not picked yet</span>
                {% endif %}
            </td>
            <td style="padding:8px;">
                {% if picks['250'] %}
                    {{ picks['250']['rider'] }}
                    {% if picks['250']['auto_random'] %}<span style="color:red; font-size:12px;"> (Random)</span>{% endif %}
                {% else %}
                    <span style="color:#999;">Not picked yet</span>
                {% endif %}
            </td>
        </tr>
        {% endfor %}
    </table>
    {% else %}
    <hr style="margin-top:30px;">
    <p><em>No other players have made picks for this round yet.</em></p>
    {% endif %}
    
    <br><a href="/dashboard">← Back to Dashboard</a>
    ''', round_num=round_num, riders_450=RIDERS_450, riders_250=RIDERS_250,
         existing_picks=existing_picks, message=message, deadline_passed=deadline_passed,
         other_players_picks=other_players_picks)

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
    return render_template_string('''
    <h2>Manual Results Entry - Round {{ round_num }}</h2>
    <form method="post">
        {% for cls, riders in [('450', RIDERS_450), ('250', RIDERS_250)] %}
        <h3>{{ cls }} Class</h3>
        {% for r in riders %}
        {{ r }}: <input name="{{ cls }}_{{ r.replace(' ', '_') }}" type="number" min="1" style="width:60px;"><br>
        {% endfor %}<br>
        {% endfor %}
        <input type="submit" value="Save Results">
    </form>
    <br><a href="/dashboard">← Back</a>
    ''', round_num=round_num, RIDERS_450=RIDERS_450, RIDERS_250=RIDERS_250)

@app.route('/admin/users', methods=['GET', 'POST'])
def admin_users():
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    conn = get_db_connection()
    c = conn.cursor()
    if request.method == 'POST':
        user_id = request.form['user_id']
        new_pass = generate_password_hash(request.form['new_password'])
        c.execute('UPDATE users SET password = %s WHERE id = %s', (new_pass, user_id))
        conn.commit()
        flash('Password reset successfully!')
    c.execute('SELECT id, username, email FROM users ORDER BY username')
    users = c.fetchall()
    conn.close()
    return render_template_string('''
    <h2>Manage Users (Admin Only)</h2>
    <table border="1" style="border-collapse:collapse; width:90%; text-align:left;">
        <tr style="background:#f0f0f0;"><th>ID</th><th>Username</th><th>Email</th><th>Reset</th></tr>
        {% for user in users %}
        <tr>
            <td>{{ user['id'] }}</td><td>{{ user['username'] }}</td><td>{{ user['email'] or 'No email' }}</td>
            <td>
                <form method="post" style="display:inline;">
                    <input type="hidden" name="user_id" value="{{ user['id'] }}">
                    <input type="password" name="new_password" placeholder="New password" required style="width:150px;">
                    <input type="submit" value="Reset">
                </form>
            </td>
        </tr>
        {% endfor %}
    </table>
    <br><a href="/dashboard">← Back</a>
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
    return render_template_string('''
    <h1>Fantasy League Rules</h1>
    <h2>How to Play</h2>
    <ul>
        <li>Pick <strong>ONE rider from 450</strong> and <strong>ONE from 250</strong> each round.</li>
        <li>Must pick both classes.</li>
        <li>Picks lock at <strong>midnight the night before each race</strong>.</li>
    </ul>
    <h2>Missed Picks?</h2>
    <ul>
        <li>If you forget, <strong>random riders will be auto-assigned</strong> (shown in <span style="color:red;">red</span> on leaderboard).</li>
    </ul>
    <h2>Repeat Rule</h2>
    <ul>
        <li>Cannot pick the same rider (same class) within any 3-round window.</li>
    </ul>
    <h2>Scoring</h2>
    <ul>
        <li>1st: 25 | 2nd: 22 | 3rd: 20 | 4th: 18 | 5th: 16 | 6th–22nd: 15 down to 1 | 23+: 0</li>
        <li>Round score = 450 pick points + 250 pick points</li>
        <li>Season winner = highest total points</li>
    </ul>
    <br><a href="/dashboard">← Back</a>
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
    
    return render_template_string('''
    <h2>Season Leaderboard</h2>
    <table style="width:100%; border-collapse:collapse; font-size:14px;">
        <thead>
            <tr style="background:#333; color:white;">
                <th>Rank</th>
                <th>Player</th>
                <th>Total</th>
                {% for rnd in completed_rounds %}
                <th>R{{ rnd }}<br>450 | 250</th>
                {% endfor %}
            </tr>
        </thead>
        <tbody>
            {% for i, player in enumerate(player_data) %}
            <tr style="background:{% if i % 2 == 0 %}#f8f8f8{% else %}#ffffff{% endif %};">
                <td style="text-align:center; font-weight:bold;">{{ i+1 }}</td>
                <td style="font-weight:bold;">{{ player.username }}</td>
                <td style="text-align:center; font-weight:bold; font-size:18px;">{{ player.total }}</td>
                {% for rnd in completed_rounds %}
                <td style="text-align:center;">
                    <span {% if player.round_picks[rnd]['450'][1] %}style="color:red;"{% endif %}>{{ player.round_picks[rnd]['450'][0] }}</span> |
                    <span {% if player.round_picks[rnd]['250'][1] %}style="color:red;"{% endif %}>{{ player.round_picks[rnd]['250'][0] }}</span>
                </td>
                {% endfor %}
            </tr>
            {% endfor %}
        </tbody>
    </table>
    <br><small>Red initials = random auto-pick (missed deadline)</small>
    <br><br><a href="/dashboard">← Back to Dashboard</a>
    ''', player_data=player_data, completed_rounds=completed_rounds, enumerate=enumerate)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
