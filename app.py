from flask import Flask, request, redirect, url_for, render_template_string, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from datetime import datetime
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
app.secret_key = 'SLRACINGsuper_random_key_2026_smxfantasy!@#123'  # CHANGE THIS!

# 2026 SuperMotocross Schedule (Supercross rounds only for now - update as Pro Motocross dates are confirmed)
SCHEDULE = [
    {'round': 1, 'date': '2026-01-10', 'location': 'Anaheim, CA', 'deadline': '2026-01-09 23:59:59'},
    {'round': 2, 'date': '2026-01-17', 'location': 'San Diego, CA', 'deadline': '2026-01-16 23:59:59'},
    {'round': 3, 'date': '2026-01-24', 'location': 'Anaheim, CA', 'deadline': '2026-01-23 23:59:59'},
    {'round': 4, 'date': '2026-01-31', 'location': 'Houston, TX', 'deadline': '2026-01-30 23:59:59'},
    {'round': 5, 'date': '2026-02-07', 'location': 'Glendale, AZ', 'deadline': '2026-02-06 23:59:59'},
    {'round': 6, 'date': '2026-02-14', 'location': 'Seattle, WA', 'deadline': '2026-02-13 23:59:59'},
    {'round': 7, 'date': '2026-02-21', 'location': 'Arlington, TX', 'deadline': '2026-02-20 23:59:59'},
    {'round': 8, 'date': '2026-02-28', 'location': 'Daytona Beach, FL', 'deadline': '2026-02-27 23:59:59'},
    {'round': 9, 'date': '2026-03-07', 'location': 'Indianapolis, IN', 'deadline': '2026-03-06 23:59:59'},
    # Add more as confirmed - Pro Motocross starts ~May 30
]

# Current main riders for 2026 (update if needed)
RIDERS_450 = [
    'Chase Sexton', 'Cooper Webb', 'Eli Tomac', 'Hunter Lawrence', 'Jason Anderson',
    'Jett Lawrence', 'Ken Roczen', 'Aaron Plessinger', 'Malcolm Stewart', 'RJ Hampshire',
    'Dylan Ferrandis', 'Jorge Prado', 'Garrett Marchbanks', 'Christian Craig', 'Dean Wilson'
]

RIDERS_250 = [
    'Haiden Deegan', 'Jo Shimoda', 'Chance Hymas', 'Levi Kitchen', 'Julien Beaumer',
    'Ryder DiFrancesco', 'Casey Cochran', 'Daxton Bennick', 'Pierce Brown', 'Max Anstie',
    'Seth Hammaker', 'Jordon Smith', 'Cameron McAdoo', 'Nate Thrasher', 'Tom Vialle'
]

def init_db():
    conn = sqlite3.connect('fantasy.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS picks (id INTEGER PRIMARY KEY, user_id INTEGER, round_num INTEGER, class TEXT, rider TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS results (id INTEGER PRIMARY KEY, round_num INTEGER, class TEXT, rider TEXT, position INTEGER)''')
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

def get_current_round():
    now = datetime.now()
    for i, s in enumerate(SCHEDULE):
        race_date = datetime.strptime(s['date'], '%Y-%m-%d')
        if now < race_date:
            return i + 1
    return len(SCHEDULE) + 1

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

# Routes below (login, register, dashboard, pick, fetch_results, leaderboard, etc.)
# ... (same as previous version - login/register/dashboard/pick/admin manual entry/leaderboard/logout)

@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        conn = sqlite3.connect('fantasy.db')
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE username = ?', (username,))
        user = c.fetchone()
        conn.close()
        if user and check_password_hash(user[2], password):
            session['user_id'] = user[0]
            session['username'] = username
            return redirect(url_for('dashboard'))
        flash('Invalid credentials')
    return render_template_string('''
    <h2>Login</h2>
    <form method="post">
        Username: <input name="username"><br>
        Password: <input type="password" name="password"><br>
        <input type="submit" value="Login">
    </form>
    <a href="/register">Register new user</a>
    ''')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form['username']
        password = generate_password_hash(request.form['password'])
        conn = sqlite3.connect('fantasy.db')
        c = conn.cursor()
        try:
            c.execute('INSERT INTO users (username, password) VALUES (?, ?)', (username, password))
            conn.commit()
            flash('Registered successfully! Now login.')
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
            flash('Username already taken')
        conn.close()
    return render_template_string('''
    <h2>Register</h2>
    <form method="post">
        Username: <input name="username"><br>
        Password: <input type="password" name="password"><br>
        <input type="submit" value="Register">
    </form>
    ''')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    current_round = get_current_round()
    return render_template_string('''
    <h1>Welcome, {{ username }}!</h1>
    Current Round: {{ current_round }}<br><br>
    <a href="/pick/{{ current_round }}">Make/ Edit Picks for Round {{ current_round }}</a><br><br>
    <a href="/leaderboard">View Full Leaderboard</a><br><br>
    {% if username == 'admin' %}
    <a href="/fetch_results/{{ current_round }}">Admin: Auto-Fetch Results for Round {{ current_round }}</a><br><br>
    <a href="/admin/{{ current_round }}">Admin: Manual Entry Fallback</a><br><br>
    {% endif %}
    <a href="/logout">Logout</a>
    ''', username=session['username'], current_round=current_round)

@app.route('/pick/<int:round_num>', methods=['GET', 'POST'])
def pick(round_num):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    sched = next((s for s in SCHEDULE if s['round'] == round_num), None)
    if not sched or datetime.now() > datetime.strptime(sched['deadline'], '%Y-%m-%d %H:%M:%S'):
        flash('Picks locked for this round')
        return redirect(url_for('dashboard'))

    conn = sqlite3.connect('fantasy.db')
    c = conn.cursor()
    if request.method == 'POST':
        rider_450 = request.form.get('rider_450')
        rider_250 = request.form.get('rider_250')
        if not rider_450 or not rider_250:
            flash('Must pick one from each class')
        elif rider_450 not in RIDERS_450 or rider_250 not in RIDERS_250:
            flash('Invalid rider selected')
        else:
            # Check 3-round rule
            c.execute('SELECT rider FROM picks WHERE user_id = ? AND class = "450" AND round_num IN (?, ?)', 
                      (session['user_id'], round_num-1, round_num-2))
            if rider_450 in [r[0] for r in c.fetchall()]:
                flash('Cannot pick same 450 rider within 3 rounds')
            else:
                c.execute('SELECT rider FROM picks WHERE user_id = ? AND class = "250" AND round_num IN (?, ?)', 
                          (session['user_id'], round_num-1, round_num-2))
                if rider_250 in [r[0] for r in c.fetchall()]:
                    flash('Cannot pick same 250 rider within 3 rounds')
                else:
                    c.execute('DELETE FROM picks WHERE user_id = ? AND round_num = ?', (session['user_id'], round_num))
                    c.execute('INSERT INTO picks (user_id, round_num, class, rider) VALUES (?, ?, "450", ?)', 
                              (session['user_id'], round_num, rider_450))
                    c.execute('INSERT INTO picks (user_id, round_num, class, rider) VALUES (?, ?, "250", ?)', 
                              (session['user_id'], round_num, rider_250))
                    conn.commit()
                    flash('Picks saved!')
                    return redirect(url_for('dashboard'))
    conn.close()
    return render_template_string('''
    <h2>Picks for Round {{ round_num }}</h2>
    <form method="post">
        450 Class: <select name="rider_450">
            {% for r in riders_450 %}<option>{{ r }}</option>{% endfor %}
        </select><br><br>
        250 Class: <select name="rider_250">
            {% for r in riders_250 %}<option>{{ r }}</option>{% endfor %}
        </select><br><br>
        <input type="submit" value="Save Picks">
    </form>
    <a href="/dashboard">Back</a>
    ''', round_num=round_num, riders_450=RIDERS_450, riders_250=RIDERS_250)

@app.route('/fetch_results/<int:round_num>')
def fetch_results(round_num):
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    event_id = get_event_id(round_num)
    if not event_id:
        flash('Event not found or results not posted yet')
        return redirect(url_for('dashboard'))
    conn = sqlite3.connect('fantasy.db')
    c = conn.cursor()
    for cls, riders in [('450', RIDERS_450), ('250', RIDERS_250)]:
        url = get_overall_url(event_id, cls)
        if url:
            results = parse_results(url, cls)
            for rider, pos in results.items():
                c.execute('INSERT OR REPLACE INTO results (round_num, class, rider, position) VALUES (?, ?, ?, ?)',
                          (round_num, cls, rider, pos))
    conn.commit()
    conn.close()
    flash('Results auto-fetched successfully!')
    return redirect(url_for('dashboard'))

@app.route('/admin/<int:round_num>', methods=['GET', 'POST'])
def admin(round_num):
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    if request.method == 'POST':
        conn = sqlite3.connect('fantasy.db')
        c = conn.cursor()
        for cls, riders in [('450', RIDERS_450), ('250', RIDERS_250)]:
            for rider in riders:
                pos_str = request.form.get(f'{cls}_{rider.replace(" ", "_")}')
                if pos_str and pos_str.isdigit():
                    c.execute('INSERT OR REPLACE INTO results (round_num, class, rider, position) VALUES (?, ?, ?, ?)',
                              (round_num, cls, rider, int(pos_str)))
        conn.commit()
        conn.close()
        flash('Manual results saved')
    return render_template_string('''
    <h2>Manual Results Entry - Round {{ round_num }}</h2>
    <form method="post">
        {% for cls, riders in [('450', riders_450), ('250', riders_250)] %}
        <h3>{{ cls }} Class</h3>
        {% for r in riders %}
        {{ r }}: <input name="{{ cls }}_{{ r.replace(' ', '_') }}" type="number" min="1"><br>
        {% endfor %}
        {% endfor %}
        <input type="submit" value="Save">
    </form>
    ''', round_num=round_num, riders_450=RIDERS_450, riders_250=RIDERS_250)

@app.route('/leaderboard')
def leaderboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = sqlite3.connect('fantasy.db')
    c = conn.cursor()
    c.execute('SELECT id, username FROM users')
    users = c.fetchall()
    scores = {}
    for user_id, username in users:
        total = 0
        for rnd in range(1, len(SCHEDULE)+1):
            for cls in ['450', '250']:
                c.execute('SELECT rider FROM picks WHERE user_id = ? AND round_num = ? AND class = ?', (user_id, rnd, cls))
                pick = c.fetchone()
                if pick:
                    c.execute('SELECT position FROM results WHERE round_num = ? AND class = ? AND rider = ?', (rnd, cls, pick[0]))
                    pos = c.fetchone()
                    if pos:
                        total += get_points(pos[0])
        scores[username] = total
    conn.close()
    sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return render_template_string('''
    <h2>Season Leaderboard</h2>
    <ol>
    {% for username, score in sorted_scores %}
        <li>{{ username }}: {{ score }} points</li>
    {% endfor %}
    </ol>
    <a href="/dashboard">Back</a>
    ''', sorted_scores=sorted_scores)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
