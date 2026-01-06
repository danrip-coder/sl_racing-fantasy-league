from flask import Flask, request, redirect, url_for, render_template_string, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from datetime import datetime
import requests
from bs4 import BeautifulSoup

app = Flask(__name__)
app.secret_key = 'change_this_to_a_long_random_string_right_now!'  # <-- CHANGE THIS ON RENDER TOO!

# 2026 Supercross Schedule (first 9 rounds confirmed)
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
]

# Updated rider lists for 2026 (key riders expected in early rounds)
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

def init_db():
    conn = sqlite3.connect('fantasy.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users 
                 (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password TEXT, email TEXT UNIQUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS picks 
                 (id INTEGER PRIMARY KEY, user_id INTEGER, round_num INTEGER, class TEXT, rider TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS results 
                 (id INTEGER PRIMARY KEY, round_num INTEGER, class TEXT, rider TEXT, position INTEGER)''')
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
        conn = sqlite3.connect('fantasy.db')
        c = conn.cursor()
        try:
            c.execute('INSERT INTO users (username, password, email) VALUES (?, ?, ?)', (username, password, email))
            conn.commit()
            flash('Registered! You can now login.')
            conn.close()
            return redirect(url_for('login'))
        except sqlite3.IntegrityError:
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
    {% if username == 'admin' %}
    <hr>
    <h3>Admin Tools</h3>
    <p><a href="/fetch_results/{{ current_round }}">Auto-Fetch Results for Round {{ current_round }}</a></p>
    <p><a href="/admin/{{ current_round }}">Manual Results Entry (Fallback)</a></p>
    <p><a href="/admin/users">Manage Users / Reset Passwords</a></p>
    {% endif %}
    <br><br>
    <a href="/logout">Logout</a>
    ''', username=session['username'], current_round=current_round)

@app.route('/pick/<int:round_num>', methods=['GET', 'POST'])
def pick(round_num):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    sched = next((s for s in SCHEDULE if s['round'] == round_num), None)
    if not sched or datetime.now() > datetime.strptime(sched['deadline'], '%Y-%m-%d %H:%M:%S'):
        flash('Picks are locked for this round')
        return redirect(url_for('dashboard'))

    conn = sqlite3.connect('fantasy.db')
    c = conn.cursor()
    if request.method == 'POST':
        rider_450 = request.form.get('rider_450')
        rider_250 = request.form.get('rider_250')
        if not rider_450 or not rider_250:
            flash('Must select one rider from each class')
        elif rider_450 not in RIDERS_450 or rider_250 not in RIDERS_250:
            flash('Invalid rider')
        else:
            # 3-round repeat rule check
            c.execute('SELECT rider FROM picks WHERE user_id = ? AND class = "450" AND round_num IN (?, ?)',
                      (session['user_id'], round_num-1, round_num-2))
            if rider_450 in [r[0] for r in c.fetchall()]:
                flash('Cannot pick the same 450 rider within 3 rounds')
            else:
                c.execute('SELECT rider FROM picks WHERE user_id = ? AND class = "250" AND round_num IN (?, ?)',
                          (session['user_id'], round_num-1, round_num-2))
                if rider_250 in [r[0] for r in c.fetchall()]:
                    flash('Cannot pick the same 250 rider within 3 rounds')
                else:
                    c.execute('DELETE FROM picks WHERE user_id = ? AND round_num = ?', (session['user_id'], round_num))
                    c.execute('INSERT INTO picks (user_id, round_num, class, rider) VALUES (?, ?, "450", ?)',
                              (session['user_id'], round_num, rider_450))
                    c.execute('INSERT INTO picks (user_id, round_num, class, rider) VALUES (?, ?, "250", ?)',
                              (session['user_id'], round_num, rider_250))
                    conn.commit()
                    flash('Picks saved successfully!')
                    return redirect(url_for('dashboard'))
    conn.close()
    return render_template_string('''
    <h2>Picks - Round {{ round_num }}</h2>
    <form method="post">
        <strong>450 Class:</strong><br>
        <select name="rider_450" style="width:300px;font-size:18px;">
            {% for r in riders_450 %}<option>{{ r }}</option>{% endfor %}
        </select><br><br>
        <strong>250 Class:</strong><br>
        <select name="rider_250" style="width:300px;font-size:18px;">
            {% for r in riders_250 %}<option>{{ r }}</option>{% endfor %}
        </select><br><br>
        <input type="submit" value="Save Picks" style="font-size:18px;padding:10px;">
    </form>
    <br><a href="/dashboard">← Back to Dashboard</a>
    ''', round_num=round_num, riders_450=RIDERS_450, riders_250=RIDERS_250)

@app.route('/fetch_results/<int:round_num>')
def fetch_results(round_num):
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    event_id = get_event_id(round_num)
    if not event_id:
        flash('Event not found or results not available yet')
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
    flash('Results successfully auto-fetched!')
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
    conn = sqlite3.connect('fantasy.db')
    c = conn.cursor()
    if request.method == 'POST':
        user_id = request.form['user_id']
        new_pass = generate_password_hash(request.form['new_password'])
        c.execute('UPDATE users SET password = ? WHERE id = ?', (new_pass, user_id))
        conn.commit()
        flash('Password reset successfully!')
    c.execute('SELECT id, username, email FROM users ORDER BY username')
    users = c.fetchall()
    conn.close()
    return render_template_string('''
    <h2>Manage Users (Admin Only)</h2>
    <table border="1" style="border-collapse:collapse; width:90%; text-align:left;">
        <tr style="background:#f0f0f0;">
            <th>ID</th><th>Username</th><th>Email</th><th>Reset Password</th>
        </tr>
        {% for uid, uname, email in users %}
        <tr>
            <td>{{ uid }}</td>
            <td>{{ uname }}</td>
            <td>{{ email or 'No email' }}</td>
            <td>
                <form method="post" style="display:inline;">
                    <input type="hidden" name="user_id" value="{{ uid }}">
                    <input type="password" name="new_password" placeholder="New password" required style="width:150px;">
                    <input type="submit" value="Reset">
                </form>
            </td>
        </tr>
        {% endfor %}
    </table>
    <br><a href="/dashboard">← Back to Dashboard</a>
    ''', users=users)

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
        <li><strong>{{ username }}</strong>: {{ score }} points</li>
    {% endfor %}
    </ol>
    <br><a href="/dashboard">← Back to Dashboard</a>
    ''', sorted_scores=sorted_scores)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=True)
