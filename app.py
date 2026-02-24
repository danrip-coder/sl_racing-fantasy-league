from flask import Flask, request, redirect, url_for, render_template_string, session, flash, send_file
from werkzeug.security import generate_password_hash, check_password_hash
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
import requests
import random
import os
import csv
import re
from io import StringIO, BytesIO
from zipfile import ZipFile

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-key-change-in-production')
DATABASE_URL = os.environ.get('DATABASE_URL')

RACE_TYPES = {
    'supercross': {'name': 'Supercross', 'emoji': '🏟️', 'color': '#e74c3c'},
    'motocross': {'name': 'Motocross', 'emoji': '🏞️', 'color': '#27ae60'},
    'SMX': {'name': 'SuperMotocross', 'emoji': '🏁', 'color': '#f39c12'}
}

def get_location_timezone(location):
    """
    Return a ZoneInfo timezone for the given race location string.
    Uses named timezones so DST is handled automatically.
    """
    if 'AZ' in location:
        return ZoneInfo('America/Phoenix')          # No DST year-round
    elif 'CA' in location or 'Seattle' in location or 'WA' in location:
        return ZoneInfo('America/Los_Angeles')       # PST/PDT
    elif 'CO' in location:
        return ZoneInfo('America/Denver')            # MST/MDT
    elif 'TX' in location:
        return ZoneInfo('America/Chicago')           # CST/CDT
    elif 'IN' in location:
        return ZoneInfo('America/Indiana/Indianapolis')  # EST/EDT
    elif 'FL' in location or 'NC' in location or 'GA' in location or 'SC' in location:
        return ZoneInfo('America/New_York')          # EST/EDT
    else:
        return ZoneInfo('America/Los_Angeles')       # Default Pacific

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
    c.execute('''CREATE TABLE IF NOT EXISTS schedule (
                 id SERIAL PRIMARY KEY, round INTEGER UNIQUE NOT NULL, race_date DATE NOT NULL,
                 location TEXT NOT NULL, race_type TEXT NOT NULL CHECK (race_type IN ('supercross', 'motocross', 'SMX')),
                 class_250 TEXT NOT NULL CHECK (class_250 IN ('West', 'East', 'Combined')))''')
    c.execute('''CREATE TABLE IF NOT EXISTS riders (
                 id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                 class TEXT NOT NULL CHECK (class IN ('450', '250_West', '250_East')),
                 active BOOLEAN DEFAULT TRUE)''')
    
    # ============================================================================
    # LEADERBOARD CACHE TABLES - For fast leaderboard loading
    # ============================================================================
    
    # Cache table for per-user, per-round, per-class data (stores everything needed for display)
    c.execute('''CREATE TABLE IF NOT EXISTS user_round_points (
                 id SERIAL PRIMARY KEY,
                 user_id INTEGER NOT NULL,
                 username TEXT NOT NULL,
                 round_num INTEGER NOT NULL,
                 race_type TEXT,
                 class TEXT NOT NULL,
                 rider TEXT,
                 rider_initials TEXT,
                 position INTEGER,
                 points INTEGER DEFAULT 0,
                 auto_random BOOLEAN DEFAULT FALSE,
                 UNIQUE(user_id, round_num, class))''')
    
    # Cache table for pre-calculated totals per user per view type
    c.execute('''CREATE TABLE IF NOT EXISTS leaderboard_totals (
                 id SERIAL PRIMARY KEY,
                 user_id INTEGER NOT NULL,
                 username TEXT NOT NULL,
                 view_type TEXT NOT NULL,
                 total_points INTEGER DEFAULT 0,
                 rank INTEGER,
                 last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                 UNIQUE(user_id, view_type))''')
    
    # Track when leaderboard was last recalculated
    c.execute('''CREATE TABLE IF NOT EXISTS leaderboard_metadata (
                 id SERIAL PRIMARY KEY,
                 key TEXT UNIQUE NOT NULL,
                 value TEXT,
                 updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    
    # ============================================================================
    # DATABASE INDEXES - For faster query performance
    # ============================================================================
    
    # Index for leaderboard_totals queries (used in leaderboard page)
    c.execute('CREATE INDEX IF NOT EXISTS idx_leaderboard_totals_view ON leaderboard_totals(view_type, rank)')
    
    # Index for user_round_points queries (used in leaderboard page)
    c.execute('CREATE INDEX IF NOT EXISTS idx_user_round_points_round ON user_round_points(round_num)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_user_round_points_race_type ON user_round_points(race_type, round_num)')
    
    # Index for results table (used for results counts)
    c.execute('CREATE INDEX IF NOT EXISTS idx_results_round ON results(round_num)')
    
    # Index for picks table (used in various places)
    c.execute('CREATE INDEX IF NOT EXISTS idx_picks_user_round ON picks(user_id, round_num)')
    
    conn.commit()
    conn.close()

init_db()

# ============================================================================
# LEADERBOARD CACHE FUNCTIONS
# ============================================================================

def recalculate_leaderboard():
    """
    Recalculate the entire leaderboard cache. Called by admin only.
    This populates user_round_points and leaderboard_totals tables.
    OPTIMIZED: Calculates deadlines inline to avoid extra DB connections.
    """
    conn = get_db_connection()
    c = conn.cursor()
    
    # Get all users
    c.execute('SELECT id, username FROM users ORDER BY username')
    users = c.fetchall()
    
    # Get schedule with race types AND location for deadline calculation
    c.execute('SELECT round, race_type, race_date, location FROM schedule ORDER BY round')
    schedule = c.fetchall()
    round_race_types = {s['round']: s['race_type'] for s in schedule}
    
    # Get all rounds that have passed deadline
    now_utc = datetime.now(timezone.utc)

    visible_rounds = []
    for s in schedule:
        tz = get_location_timezone(s['location'])
        deadline = datetime.combine(s['race_date'], datetime.min.time(), tzinfo=tz)
        if now_utc > deadline:
            visible_rounds.append(s['round'])
    
    # Clear existing cache
    c.execute('DELETE FROM user_round_points')
    c.execute('DELETE FROM leaderboard_totals')
    
    # Get all picks in one query
    c.execute('''SELECT user_id, round_num, class, rider, auto_random 
                 FROM picks WHERE round_num = ANY(%s)''', (visible_rounds,))
    all_picks = c.fetchall()
    
    # Index picks by (user_id, round_num, class)
    picks_index = {}
    for pick in all_picks:
        key = (pick['user_id'], pick['round_num'], pick['class'])
        picks_index[key] = pick
    
    # Get all results in one query
    c.execute('SELECT round_num, class, rider, position FROM results')
    all_results = c.fetchall()
    
    # Index results by (round_num, class, rider)
    results_index = {}
    for result in all_results:
        key = (result['round_num'], result['class'], result['rider'])
        results_index[key] = result['position']
    
    # Calculate and cache data for each user
    user_totals = {user['id']: {'overall': 0, 'supercross': 0, 'motocross': 0, 'SMX': 0} 
                   for user in users}
    
    for user in users:
        user_id = user['id']
        username = user['username']
        
        for rnd in visible_rounds:
            race_type = round_race_types.get(rnd, 'supercross')
            
            for cls in ['450', '250']:
                pick_key = (user_id, rnd, cls)
                pick = picks_index.get(pick_key)
                
                rider = None
                rider_initials = '—'
                position = None
                points = 0
                auto_random = False
                
                if pick:
                    rider = pick['rider']
                    rider_initials = get_initials(rider) if rider else '—'
                    auto_random = bool(pick['auto_random'])
                    
                    # Look up result
                    result_key = (rnd, cls, rider)
                    if result_key in results_index:
                        position = results_index[result_key]
                        points = get_points(position)
                        
                        # Add to totals
                        user_totals[user_id]['overall'] += points
                        user_totals[user_id][race_type] += points
                
                # Insert into cache
                c.execute('''INSERT INTO user_round_points 
                             (user_id, username, round_num, race_type, class, rider, rider_initials, position, points, auto_random)
                             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                             ON CONFLICT (user_id, round_num, class) 
                             DO UPDATE SET username = EXCLUDED.username, race_type = EXCLUDED.race_type,
                                           rider = EXCLUDED.rider, rider_initials = EXCLUDED.rider_initials,
                                           position = EXCLUDED.position, points = EXCLUDED.points,
                                           auto_random = EXCLUDED.auto_random''',
                          (user_id, username, rnd, race_type, cls, rider, rider_initials, position, points, auto_random))
    
    # Calculate ranks and insert totals for each view type
    for view_type in ['overall', 'supercross', 'motocross', 'SMX']:
        # Sort users by points for this view
        sorted_users = sorted(users, key=lambda u: user_totals[u['id']][view_type], reverse=True)
        
        for rank, user in enumerate(sorted_users, 1):
            user_id = user['id']
            username = user['username']
            total = user_totals[user_id][view_type]
            
            c.execute('''INSERT INTO leaderboard_totals (user_id, username, view_type, total_points, rank, last_updated)
                         VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                         ON CONFLICT (user_id, view_type) 
                         DO UPDATE SET username = EXCLUDED.username, total_points = EXCLUDED.total_points,
                                       rank = EXCLUDED.rank, last_updated = CURRENT_TIMESTAMP''',
                      (user_id, username, view_type, total, rank))
    
    # Update metadata
    c.execute('''INSERT INTO leaderboard_metadata (key, value, updated_at)
                 VALUES ('last_recalculated', %s, CURRENT_TIMESTAMP)
                 ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP''',
              (now_utc.isoformat(),))
    
    conn.commit()
    conn.close()
    
    return len(users), len(visible_rounds)

def get_leaderboard_last_updated():
    """Get when the leaderboard was last recalculated"""
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT value, updated_at FROM leaderboard_metadata WHERE key = 'last_recalculated'")
    result = c.fetchone()
    conn.close()
    return result

def get_schedule():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM schedule ORDER BY round')
    schedule = c.fetchall()
    conn.close()
    return schedule

def get_riders_by_class(rider_class):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT name FROM riders WHERE class = %s AND active = TRUE ORDER BY name', (rider_class,))
    riders = [r['name'] for r in c.fetchall()]
    conn.close()
    return riders

def get_available_250_riders(round_num):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT class_250 FROM schedule WHERE round = %s', (round_num,))
    result = c.fetchone()
    conn.close()
    if not result:
        return []
    class_250 = result['class_250']
    if class_250 == 'West':
        return get_riders_by_class('250_West')
    elif class_250 == 'East':
        return get_riders_by_class('250_East')
    else:
        return get_riders_by_class('250_West') + get_riders_by_class('250_East')

def get_top_riders_by_points(rider_class, round_num, exclude_riders=None):
    """
    Get top 10 riders by championship points for smart auto-pick.
    Only considers riders who have actually scored points in previous rounds.
    Falls back to random selection from riders with results if top 10 are all excluded.
    OPTIMIZED: Uses single DB connection for all queries.
    """
    conn = get_db_connection()
    c = conn.cursor()

    # Get available riders using the same connection (avoids extra connections)
    if rider_class == '250':
        c.execute('SELECT class_250 FROM schedule WHERE round = %s', (round_num,))
        result = c.fetchone()
        if not result:
            conn.close()
            return []
        class_250 = result['class_250']
        if class_250 == 'West':
            c.execute('SELECT name FROM riders WHERE class = %s AND active = TRUE ORDER BY name', ('250_West',))
        elif class_250 == 'East':
            c.execute('SELECT name FROM riders WHERE class = %s AND active = TRUE ORDER BY name', ('250_East',))
        else:
            c.execute('SELECT name FROM riders WHERE class IN (%s, %s) AND active = TRUE ORDER BY name',
                      ('250_West', '250_East'))
        available_riders = [r['name'] for r in c.fetchall()]
    else:
        c.execute('SELECT name FROM riders WHERE class = %s AND active = TRUE ORDER BY name', ('450',))
        available_riders = [r['name'] for r in c.fetchall()]

    if not available_riders:
        conn.close()
        return []

    # Single query to get points for ALL riders at once
    c.execute('''SELECT rider,
                        SUM(CASE 
                            WHEN position = 1 THEN 25
                            WHEN position = 2 THEN 22
                            WHEN position = 3 THEN 20
                            WHEN position = 4 THEN 18
                            WHEN position >= 5 AND position <= 20 THEN 22 - position
                            ELSE 0
                        END) as total_points,
                        COUNT(*) as race_count
                 FROM results 
                 WHERE rider = ANY(%s) AND round_num < %s
                 GROUP BY rider''', (available_riders, round_num))

    results = c.fetchall()
    conn.close()

    # Build lookup of rider points and race counts
    rider_points = {r['rider']: r['total_points'] or 0 for r in results}
    riders_with_results = [r['rider'] for r in results if r['race_count'] > 0]

    # Sort riders WHO HAVE RESULTS by points (descending), then by name for consistency
    riders_with_points = [(rider, rider_points.get(rider, 0)) for rider in riders_with_results]
    sorted_riders = sorted(riders_with_points, key=lambda x: (-x[1], x[0]))

    # Get top 10 riders who have actually scored points (points > 0)
    top_riders_with_points = [rider for rider, points in sorted_riders if points > 0][:10]

    # Filter out excluded riders (from 3-round rule)
    if exclude_riders:
        top_riders_with_points = [r for r in top_riders_with_points if r not in exclude_riders]

    # If we have riders with points after filtering, use them
    if top_riders_with_points:
        return top_riders_with_points

    # Fallback 1: Use riders who have appeared in results (even with 0 points from DNFs etc)
    fallback_riders = [r for r in riders_with_results if r not in (exclude_riders or [])]
    if fallback_riders:
        fallback_riders = sorted(fallback_riders, key=lambda r: rider_points.get(r, 0), reverse=True)
        return fallback_riders[:10] if len(fallback_riders) > 10 else fallback_riders

    # Fallback 2: If no riders have results yet (very early season), use all available riders
    fallback_all = [r for r in available_riders if r not in (exclude_riders or [])]
    return fallback_all

def get_points(position):
    if position == 1: return 25
    elif position == 2: return 22
    elif position == 3: return 20
    elif position == 4: return 18
    elif position >= 5 and position <= 20: return 22 - position
    else: return 0

def get_initials(name):
    parts = name.split()
    return ''.join(p[0].upper() for p in parts if p)

def get_current_round():
    """Get the current active round (first round whose deadline hasn't passed)"""
    now_utc = datetime.now(timezone.utc)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT round, race_date, location FROM schedule ORDER BY round')
    schedule = c.fetchall()
    conn.close()

    for s in schedule:
        tz = get_location_timezone(s['location'])
        deadline = datetime.combine(s['race_date'], datetime.min.time(), tzinfo=tz)
        if now_utc < deadline:
            return s['round']

    return len(schedule) + 1 if schedule else 1

def get_round_info(round_num):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM schedule WHERE round = %s', (round_num,))
    result = c.fetchone()
    conn.close()
    return result

def get_deadline_for_round(round_num):
    round_info = get_round_info(round_num)
    if not round_info:
        return None
    race_date = round_info['race_date']
    location = round_info['location']
    tz = get_location_timezone(location)
    return datetime.combine(race_date, datetime.min.time(), tzinfo=tz)

def get_round_location(round_num):
    round_info = get_round_info(round_num)
    if round_info:
        return round_info['location'].split(',')[0]
    return ""

def get_race_type_display(race_type):
    return RACE_TYPES.get(race_type, {'name': race_type, 'emoji': '🏍️', 'color': '#c9975b'})

# ============================================================================
# AUTO-FETCH RESULTS FROM SUPERMOTOCROSS.COM
# ============================================================================

def get_event_id(round_num):
    """
    Get event ID from supermotocross.com based on round information.
    The site organizes results by events, so we need to find the matching event.
    """
    round_info = get_round_info(round_num)
    if not round_info:
        return None
    
    location = round_info['location'].lower()
    race_type = round_info['race_type']
    race_date = round_info['race_date']
    
    try:
        # Fetch the main results/schedule page
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Try to fetch the results page based on race type
        if race_type == 'supercross':
            url = 'https://www.supermotocross.com/supercross/results'
        elif race_type == 'motocross':
            url = 'https://www.supermotocross.com/motocross/results'
        else:  # SMX
            url = 'https://www.supermotocross.com/smx/results'
        
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            # Try alternate URL structure
            url = f'https://www.supermotocross.com/results'
            response = requests.get(url, headers=headers, timeout=15)
        
        if response.status_code != 200:
            return None
            
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Look for event links that match location
        # Common patterns: links containing city names, round numbers, or dates
        location_parts = location.replace(',', ' ').split()
        city = location_parts[0].lower() if location_parts else ''
        
        # Search for links containing the location name
        for link in soup.find_all('a', href=True):
            link_text = link.get_text().lower()
            link_href = link['href'].lower()
            
            # Check if location matches
            if city and (city in link_text or city in link_href):
                # Extract event ID from href
                # Common patterns: /event/123, /results/event-name, /round/1
                href = link['href']
                
                # Try to extract numeric ID
                id_match = re.search(r'/(\d+)', href)
                if id_match:
                    return id_match.group(1)
                
                # Return the full path as identifier
                if '/event/' in href or '/round/' in href or '/results/' in href:
                    return href
        
        # Fallback: construct event identifier from round info
        return f"{race_type}-round-{round_num}"
        
    except Exception as e:
        print(f"Error fetching event ID: {e}")
        return None

def get_overall_url(event_id, rider_class):
    """
    Get the URL for overall/combined results for a specific class.
    """
    if not event_id:
        return None
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        # Construct URLs based on common patterns
        base_urls = [
            f'https://www.supermotocross.com{event_id}' if event_id.startswith('/') else f'https://www.supermotocross.com/results/{event_id}',
            f'https://www.supermotocross.com/event/{event_id}/results',
            f'https://www.supermotocross.com/results/{event_id}',
        ]
        
        class_suffix = '450' if rider_class == '450' else '250'
        
        for base_url in base_urls:
            # Try various URL patterns for class-specific results
            urls_to_try = [
                f'{base_url}/{class_suffix}',
                f'{base_url}?class={class_suffix}',
                f'{base_url}/overall/{class_suffix}',
                f'{base_url}#{class_suffix}',
                base_url,  # Sometimes all classes on one page
            ]
            
            for url in urls_to_try:
                try:
                    response = requests.get(url, headers=headers, timeout=10)
                    if response.status_code == 200:
                        # Verify this page has results content
                        if 'position' in response.text.lower() or 'place' in response.text.lower() or 'finish' in response.text.lower():
                            return url
                except:
                    continue
        
        return None
        
    except Exception as e:
        print(f"Error getting overall URL: {e}")
        return None

def parse_results(url, rider_class, round_num):
    """
    Parse results from a given URL and return a dictionary of {rider_name: position}.
    Matches riders against the database to ensure we only get valid riders.
    """
    if not url:
        return {}
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code != 200:
            return {}
        
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Get our registered riders for this class
        if rider_class == '450':
            our_riders = get_riders_by_class('450')
        else:
            our_riders = get_available_250_riders(round_num)
        
        # Create lookup dict for case-insensitive matching
        rider_lookup = {r.lower(): r for r in our_riders}
        
        results = {}
        
        # Method 1: Look for tables with results
        for table in soup.find_all('table'):
            rows = table.find_all('tr')
            for row in rows:
                cells = row.find_all(['td', 'th'])
                if len(cells) >= 2:
                    # Look for position (number) and name
                    for i, cell in enumerate(cells):
                        cell_text = cell.get_text().strip()
                        
                        # Check if this cell contains a position number
                        if cell_text.isdigit():
                            position = int(cell_text)
                            
                            # Look for rider name in adjacent cells
                            for j, other_cell in enumerate(cells):
                                if i != j:
                                    name_text = other_cell.get_text().strip()
                                    # Check if this matches one of our riders
                                    name_lower = name_text.lower()
                                    
                                    # Try exact match
                                    if name_lower in rider_lookup:
                                        rider_name = rider_lookup[name_lower]
                                        if rider_name not in results:
                                            results[rider_name] = position
                                        break
                                    
                                    # Try partial match (first and last name)
                                    for rider_key, rider_name in rider_lookup.items():
                                        if rider_key in name_lower or name_lower in rider_key:
                                            if rider_name not in results:
                                                results[rider_name] = position
                                            break
        
        # Method 2: Look for structured result elements (common in modern sites)
        result_containers = soup.find_all(['div', 'li'], class_=lambda x: x and ('result' in x.lower() or 'position' in x.lower() or 'standing' in x.lower()))
        
        for container in result_containers:
            text = container.get_text()
            
            # Extract position and name using patterns
            # Pattern: "1. Rider Name" or "1 Rider Name" or "Pos: 1 Name: Rider Name"
            patterns = [
                r'(\d+)\s*[\.\)\-]\s*([A-Za-z\s]+)',
                r'pos[ition]*[:\s]+(\d+)[,\s]+([A-Za-z\s]+)',
            ]
            
            for pattern in patterns:
                matches = re.findall(pattern, text, re.IGNORECASE)
                for match in matches:
                    try:
                        position = int(match[0])
                        name_text = match[1].strip().lower()
                        
                        if name_text in rider_lookup:
                            rider_name = rider_lookup[name_text]
                            if rider_name not in results:
                                results[rider_name] = position
                    except:
                        continue
        
        return results
        
    except Exception as e:
        print(f"Error parsing results: {e}")
        return {}

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
            max-width: 1400px;
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
        input[type="text"], input[type="password"], input[type="email"], input[type="number"], input[type="date"], select {
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
        .race-type-badge {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 4px;
            font-size: 0.9em;
            font-weight: 600;
            margin-left: 10px;
        }
        .leaderboard-tabs {
            display: flex;
            gap: 10px;
            margin: 20px 0;
            flex-wrap: wrap;
        }
        .leaderboard-tab {
            padding: 12px 24px;
            background: #363636;
            border: 2px solid #4a4a4a;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.3s;
            font-weight: 600;
        }
        .leaderboard-tab:hover {
            border-color: #c9975b;
        }
        .leaderboard-tab.active {
            background: #c9975b;
            color: #1a1a1a;
            border-color: #c9975b;
        }
        .form-row {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin: 15px 0;
        }
    </style>
    '''

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
                <br>
                <a href="/forgot-password" class="link">Forgot your password?</a>
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

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        if not username or not email or not new_password or not confirm_password:
            flash('All fields are required')
            return redirect(url_for('forgot_password'))
        if new_password != confirm_password:
            flash('Passwords do not match')
            return redirect(url_for('forgot_password'))
        if len(new_password) < 6:
            flash('Password must be at least 6 characters')
            return redirect(url_for('forgot_password'))
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE username = %s AND email = %s', (username, email))
        user = c.fetchone()
        if user:
            hashed = generate_password_hash(new_password)
            c.execute('UPDATE users SET password = %s WHERE id = %s', (hashed, user['id']))
            conn.commit()
            conn.close()
            flash('Password reset successful! You can now login with your new password.')
            return redirect(url_for('login'))
        else:
            conn.close()
            flash('Username and email combination not found. Please contact admin for help.')
            return redirect(url_for('forgot_password'))
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>🔑 Reset Password</h1>
        <div class="card">
            {% with messages = get_flashed_messages() %}
                {% if messages %}
                    {% for message in messages %}
                        <div class="flash">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            <p style="color: #b0b0b0; margin-bottom: 20px;">
                Enter your username and email address to reset your password.
            </p>
            <form method="post">
                <label><strong>Username</strong></label>
                <input type="text" name="username" required>
                <label><strong>Email Address</strong></label>
                <input type="email" name="email" required>
                <label><strong>New Password (min 6 characters)</strong></label>
                <input type="password" name="new_password" required minlength="6">
                <label><strong>Confirm New Password</strong></label>
                <input type="password" name="confirm_password" required minlength="6">
                <button type="submit" class="btn">Reset Password</button>
            </form>
            <p style="margin-top: 20px;">
                <a href="/" class="link">← Back to Login</a>
            </p>
        </div>
    </div>
    ''')

@app.route('/change-password', methods=['GET', 'POST'])
def change_password():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if request.method == 'POST':
        current_password = request.form.get('current_password')
        new_password = request.form.get('new_password')
        confirm_password = request.form.get('confirm_password')
        if not current_password or not new_password or not confirm_password:
            flash('All fields are required')
            return redirect(url_for('change_password'))
        if new_password != confirm_password:
            flash('New passwords do not match')
            return redirect(url_for('change_password'))
        if len(new_password) < 6:
            flash('Password must be at least 6 characters')
            return redirect(url_for('change_password'))
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT * FROM users WHERE id = %s', (session['user_id'],))
        user = c.fetchone()
        if user and check_password_hash(user['password'], current_password):
            hashed = generate_password_hash(new_password)
            c.execute('UPDATE users SET password = %s WHERE id = %s', (hashed, session['user_id']))
            conn.commit()
            conn.close()
            flash('Password changed successfully!')
            return redirect(url_for('dashboard'))
        else:
            conn.close()
            flash('Current password is incorrect')
            return redirect(url_for('change_password'))
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>🔑 Change Your Password</h1>
        <div class="card">
            {% with messages = get_flashed_messages() %}
                {% if messages %}
                    {% for message in messages %}
                        <div class="flash">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            <form method="post">
                <label><strong>Current Password</strong></label>
                <input type="password" name="current_password" required>
                <label><strong>New Password (min 6 characters)</strong></label>
                <input type="password" name="new_password" required minlength="6">
                <label><strong>Confirm New Password</strong></label>
                <input type="password" name="confirm_password" required minlength="6">
                <button type="submit" class="btn">Change Password</button>
            </form>
            <p style="margin-top: 20px;">
                <a href="/dashboard" class="link">← Back to Dashboard</a>
            </p>
        </div>
    </div>
    ''')

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    current_round = get_current_round()
    round_info = get_round_info(current_round)
    location = ""
    race_type_info = {'emoji': '🏍️', 'name': 'Racing', 'color': '#c9975b'}
    if round_info:
        location = round_info['location'].split(',')[0]
        race_type_info = get_race_type_display(round_info['race_type'])
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>Welcome, {{ username }}! 🏁</h1>
        <p style="font-size: 1.2em; color: #b0b0b0; margin-bottom: 30px;">
            <strong>Current Round:</strong> {{ current_round }} {{ location }}
            <span class="race-type-badge" style="background: {{ race_type_info['color'] }}20; color: {{ race_type_info['color'] }}; border: 1px solid {{ race_type_info['color'] }};">
                {{ race_type_info['emoji'] }} {{ race_type_info['name'] }}
            </span>
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
            <a href="/admin/schedule" class="btn btn-small">Manage Schedule</a>
            <a href="/admin/riders" class="btn btn-small">Manage Riders</a>
            <a href="/admin/results-selector" class="btn btn-small">Enter Results</a>
            <a href="/admin/manage-users" class="btn btn-small">Manage Users</a>
            <a href="/admin/export" class="btn btn-small">Export Database</a>
            <a href="/admin/recalculate-leaderboard" class="btn btn-small" style="background: #27ae60;"
               onclick="this.innerHTML='⏳ Calculating...'; this.style.pointerEvents='none';">
               🔄 Recalculate Leaderboard
            </a>
        </div>
        {% endif %}
        <div style="margin-top: 40px;">
            <a href="/change-password" class="link">Change Password</a> | 
            <a href="/logout" class="link">Logout</a>
        </div>
    </div>
    ''', username=session['username'], current_round=current_round, location=location, race_type_info=race_type_info)

@app.route('/pick/<int:round_num>', methods=['GET', 'POST'])
def pick(round_num):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    round_info = get_round_info(round_num)
    if not round_info:
        flash('Invalid round')
        return redirect(url_for('dashboard'))
    deadline = get_deadline_for_round(round_num)
    deadline_passed = False
    if deadline:
        now_utc = datetime.now(timezone.utc)
        deadline_passed = now_utc > deadline
    riders_450 = get_riders_by_class('450')
    riders_250 = get_available_250_riders(round_num)
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT class, rider, auto_random FROM picks WHERE user_id = %s AND round_num = %s', 
              (session['user_id'], round_num))
    existing = c.fetchall()
    existing_picks = {row['class']: (row['rider'], row['auto_random']) for row in existing}
    c.execute('''SELECT u.username, p.class, p.rider, p.auto_random 
                 FROM picks p 
                 JOIN users u ON p.user_id = u.id 
                 WHERE p.round_num = %s 
                 ORDER BY u.username, p.class''', (round_num,))
    all_picks_raw = c.fetchall()
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
        elif rider_450 not in riders_450 or rider_250 not in riders_250:
            flash('Invalid rider')
        else:
            # 3-round rule: check ±2 rounds in both directions (handles out-of-order admin ops)
            nearby_rounds = [r for r in [round_num-2, round_num-1, round_num+1, round_num+2] if r > 0]
            c.execute('''SELECT rider FROM picks WHERE user_id = %s AND class = %s AND round_num = ANY(%s)''',
                      (session['user_id'], '450', nearby_rounds))
            if rider_450 in [r['rider'] for r in c.fetchall()]:
                flash('Cannot pick the same 450 rider within 3 rounds')
            else:
                c.execute('''SELECT rider FROM picks WHERE user_id = %s AND class = %s AND round_num = ANY(%s)''',
                          (session['user_id'], '250', nearby_rounds))
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
                    conn.close()
                    return redirect(url_for('dashboard'))
    elif deadline_passed and len(existing_picks) == 0:
        # Smart auto-pick: use top riders by points, respecting 3-round rule
        # Check rounds within 2 in either direction for exclusions
        nearby_rounds = [round_num-2, round_num-1, round_num+1, round_num+2]
        nearby_rounds = [r for r in nearby_rounds if r > 0]
        
        c.execute('''SELECT rider FROM picks 
                    WHERE user_id = %s AND class = %s AND round_num = ANY(%s)''',
                  (session['user_id'], '450', nearby_rounds))
        excluded_450 = [r['rider'] for r in c.fetchall()]
        
        c.execute('''SELECT rider FROM picks 
                    WHERE user_id = %s AND class = %s AND round_num = ANY(%s)''',
                  (session['user_id'], '250', nearby_rounds))
        excluded_250 = [r['rider'] for r in c.fetchall()]
        
        # Get top riders by points, excluding recently picked
        top_450 = get_top_riders_by_points('450', round_num, excluded_450)
        top_250 = get_top_riders_by_points('250', round_num, excluded_250)
        
        random_450 = random.choice(top_450) if top_450 else None
        random_250 = random.choice(top_250) if top_250 else None
        
        if random_450 and random_250:
            c.execute('INSERT INTO picks (user_id, round_num, class, rider, auto_random) VALUES (%s, %s, %s, %s, %s)',
                      (session['user_id'], round_num, '450', random_450, 1))
            c.execute('INSERT INTO picks (user_id, round_num, class, rider, auto_random) VALUES (%s, %s, %s, %s, %s)',
                      (session['user_id'], round_num, '250', random_250, 1))
            conn.commit()
            flash(f'No picks submitted — smart auto-picks assigned: {random_450} (450) and {random_250} (250)')
            existing_picks = {'450': (random_450, 1), '250': (random_250, 1)}
    conn.close()
    message = ""
    if deadline_passed:
        if deadline:
            deadline_display = deadline.astimezone()
            message = f"Picks locked (deadline was midnight USA race time on {deadline_display.strftime('%B %d')})."
        else:
            message = "Picks locked."
        if any(existing_picks.get(cls, (None, 0))[1] for cls in ['450', '250']):
            message += " <strong style='color:#e74c3c;'>Random picks applied.</strong>"
    location = round_info['location'].split(',')[0]
    race_type_info = get_race_type_display(round_info['race_type'])
    deadline_iso = deadline.isoformat() if deadline else None
    class_250_type = round_info['class_250']
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h2>🏍️ Round {{ round_num }} {{ location }} - Picks
            <span class="race-type-badge" style="background: {{ race_type_info['color'] }}20; color: {{ race_type_info['color'] }}; border: 1px solid {{ race_type_info['color'] }};">
                {{ race_type_info['emoji'] }} {{ race_type_info['name'] }}
            </span>
        </h2>
        {% if class_250_type != 'Combined' %}
        <p style="color: #d4a574; margin-bottom: 20px;">
            <strong>250 Class:</strong> {{ class_250_type }} riders only this round
        </p>
        {% endif %}
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
            <h3>⏰ Picks Lock at Midnight USA Race Time:</h3>
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
            <p style="margin-top: 15px; color: #b0b0b0; font-size: 0.9em;">
                Deadline: <span id="deadline-display"></span>
            </p>
        </div>
        <script>
            const deadlineUTC = new Date("{{ deadline_iso }}");
            document.getElementById('deadline-display').textContent = deadlineUTC.toLocaleString('en-AU', {
                weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
                hour: '2-digit', minute: '2-digit', timeZoneName: 'short'
            });
            function updateCountdown() {
                const now = new Date();
                const diff = deadlineUTC - now;
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
                    {% if '450' in existing_picks and existing_picks['450'][1] %}<span class="random-pick">(Random)</span>{% endif %}
                </p>
                <p><strong>250 Class:</strong> {{ existing_picks['250'][0] if '250' in existing_picks else 'None' }}
                    {% if '250' in existing_picks and existing_picks['250'][1] %}<span class="random-pick">(Random)</span>{% endif %}
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
                            {% if picks['450']['auto_random'] %}<span class="random-pick">(Random)</span>{% endif %}
                        {% else %}
                            <span style="color:#999;">Not picked yet</span>
                        {% endif %}
                    </td>
                    <td>
                        {% if picks['250'] %}
                            {{ picks['250']['rider'] }}
                            {% if picks['250']['auto_random'] %}<span class="random-pick">(Random)</span>{% endif %}
                        {% else %}
                            <span style="color:#999;">Not picked yet</span>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
        {% endif %}
        <div style="margin-top: 30px;">
            <a href="/dashboard" class="link">← Back to Dashboard</a>
        </div>
    </div>
    ''', round_num=round_num, riders_450=riders_450, riders_250=riders_250,
         existing_picks=existing_picks, message=message, deadline_passed=deadline_passed,
         all_players_picks=all_players_picks, location=location, session=session, 
         deadline_iso=deadline_iso, race_type_info=race_type_info, class_250_type=class_250_type)

@app.route('/leaderboard')
def leaderboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    
    view = request.args.get('view', 'overall')
    conn = get_db_connection()
    c = conn.cursor()
    
    # ============================================================================
    # SIMPLE CACHE-ONLY LEADERBOARD - Fast, relies on admin to recalculate
    # ============================================================================

    now_utc = datetime.now(timezone.utc)
    
    # Query 1: Get totals from cache (pre-calculated, pre-ranked)
    c.execute('''SELECT user_id, username, total_points, rank 
                 FROM leaderboard_totals 
                 WHERE view_type = %s 
                 ORDER BY rank''', (view,))
    totals = c.fetchall()
    
    # Query 2: Get ALL schedule data
    c.execute('SELECT * FROM schedule ORDER BY round')
    schedule = c.fetchall()
    
    # Query 3: Get results counts for ALL rounds
    c.execute('''SELECT round_num, COUNT(*) as count 
                 FROM results 
                 GROUP BY round_num''')
    results_counts = {row['round_num']: row['count'] for row in c.fetchall()}
    
    # Calculate visible rounds (deadline passed)
    visible_rounds = []
    for s in schedule:
        tz = get_location_timezone(s['location'])
        deadline = datetime.combine(s['race_date'], datetime.min.time(), tzinfo=tz)

        if now_utc > deadline:
            round_info = dict(s)
            round_info['has_results'] = results_counts.get(s['round'], 0) > 0
            round_info['short_location'] = location.split(',')[0]
            
            if view == 'overall' or s['race_type'] == view:
                visible_rounds.append(round_info)
    
    # Get round numbers for query
    round_nums = [r['round'] for r in visible_rounds]
    
    # Query 4: Get all round details from cache
    round_details = {}
    if round_nums:
        if view == 'overall':
            c.execute('''SELECT user_id, round_num, class, rider_initials, points, auto_random 
                         FROM user_round_points 
                         WHERE round_num = ANY(%s)
                         ORDER BY user_id, round_num, class''', (round_nums,))
        else:
            c.execute('''SELECT user_id, round_num, class, rider_initials, points, auto_random 
                         FROM user_round_points 
                         WHERE round_num = ANY(%s) AND race_type = %s
                         ORDER BY user_id, round_num, class''', (round_nums, view))
        
        all_round_data = c.fetchall()
        
        # Index by (user_id, round_num, class) for fast lookup
        for row in all_round_data:
            key = (row['user_id'], row['round_num'], row['class'])
            round_details[key] = {
                'initials': row['rider_initials'] or '—',
                'points': row['points'] if row['points'] is not None else '-',
                'random': bool(row['auto_random'])
            }
    
    # Query 5: Get last updated time
    c.execute("SELECT updated_at FROM leaderboard_metadata WHERE key = 'last_recalculated'")
    last_updated_row = c.fetchone()
    last_updated = last_updated_row['updated_at'] if last_updated_row else None
    
    conn.close()
    
    # Build player_data structure for template
    player_data = []
    for total_row in totals:
        user_id = total_row['user_id']
        username = total_row['username']
        total = total_row['total_points']
        rank = total_row['rank']
        
        round_picks = {}
        for rnd_info in visible_rounds:
            rnd = rnd_info['round']
            has_results = rnd_info['has_results']
            
            pick_450 = round_details.get((user_id, rnd, '450'), {'initials': '—', 'points': '-', 'random': False})
            pick_250 = round_details.get((user_id, rnd, '250'), {'initials': '—', 'points': '-', 'random': False})
            
            # If no results yet, show '-' for points
            if not has_results:
                pick_450 = dict(pick_450)
                pick_250 = dict(pick_250)
                pick_450['points'] = '-'
                pick_250['points'] = '-'
            
            round_picks[rnd] = {
                '450': pick_450,
                '250': pick_250
            }
        
        player_data.append({
            'username': username,
            'total': total,
            'rank': rank,
            'round_picks': round_picks
        })
    
    # Check if cache is empty
    cache_empty = len(player_data) == 0
    
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>🏆 Season Leaderboard</h1>
        
        {% if cache_empty %}
        <div class="card" style="background: #f39c12; color: #1a1a1a; border-left-color: #e67e22;">
            <h3 style="margin-top: 0; color: #1a1a1a;">⚠️ Leaderboard Not Yet Calculated</h3>
            <p>The leaderboard cache is empty. An admin needs to click "Recalculate Leaderboard" to populate the standings.</p>
        </div>
        {% endif %}
        
        {% if last_updated %}
        <p style="color: #888; font-size: 0.85em; margin-bottom: 15px;">
            📊 Last updated: {{ last_updated.strftime('%b %d, %Y at %I:%M %p') }} UTC
        </p>
        {% endif %}
        
        <div class="leaderboard-tabs">
            <div class="leaderboard-tab {% if view == 'overall' %}active{% endif %}" 
                 onclick="window.location.href='/leaderboard?view=overall'">
                🏆 Overall
            </div>
            <div class="leaderboard-tab {% if view == 'supercross' %}active{% endif %}" 
                 onclick="window.location.href='/leaderboard?view=supercross'">
                🏟️ Supercross
            </div>
            <div class="leaderboard-tab {% if view == 'motocross' %}active{% endif %}" 
                 onclick="window.location.href='/leaderboard?view=motocross'">
                🏞️ Motocross
            </div>
            <div class="leaderboard-tab {% if view == 'SMX' %}active{% endif %}" 
                 onclick="window.location.href='/leaderboard?view=SMX'">
                🏁 SMX
            </div>
        </div>
        
        {% if not cache_empty %}
        <div style="overflow-x: auto;">
            <table>
                <thead>
                    <tr>
                        <th style="text-align: center;">Rank</th>
                        <th>Player</th>
                        <th style="text-align: center;">Total Points</th>
                        {% for rnd_info in visible_rounds %}
                        <th style="text-align: center; {% if not rnd_info['has_results'] %}background: #f39c12;{% endif %}">
                            R{{ rnd_info['round'] }} {{ rnd_info['short_location'] }}<br>
                            <small style="opacity: 0.7;">{{ get_race_type_display(rnd_info['race_type'])['emoji'] }} 450 | 250</small>
                            {% if not rnd_info['has_results'] %}
                            <br><small style="font-weight: normal; opacity: 0.9;">⏱️ In Progress</small>
                            {% endif %}
                        </th>
                        {% endfor %}
                    </tr>
                </thead>
                <tbody>
                    {% for player in player_data %}
                    <tr {% if player.username == session.username %}style="background: #3d3d3d; font-weight: 600; border-left: 3px solid #c9975b;"{% endif %}>
                        <td style="text-align: center; font-size: 1.3em; font-weight: bold;">
                            {% if player.rank == 1 %}🥇
                            {% elif player.rank == 2 %}🥈
                            {% elif player.rank == 3 %}🥉
                            {% else %}{{ player.rank }}
                            {% endif %}
                        </td>
                        <td style="font-weight: 600;">{{ player.username }}</td>
                        <td style="text-align: center; font-size: 1.4em; font-weight: bold; color: #c9975b;">
                            {{ player.total }}
                        </td>
                        {% for rnd_info in visible_rounds %}
                        <td style="text-align: center; font-size: 0.9em; {% if not rnd_info['has_results'] %}background: #3a3a3a;{% endif %}">
                            <div style="margin-bottom: 3px;">
                                <span {% if player.round_picks[rnd_info['round']]['450']['random'] %}class="random-pick"{% endif %}>
                                    {{ player.round_picks[rnd_info['round']]['450']['initials'] }}
                                </span>
                                |
                                <span {% if player.round_picks[rnd_info['round']]['250']['random'] %}class="random-pick"{% endif %}>
                                    {{ player.round_picks[rnd_info['round']]['250']['initials'] }}
                                </span>
                            </div>
                            {% if rnd_info['has_results'] %}
                            <div style="font-size: 0.75em; color: #c9975b; font-weight: 600;">
                                {{ player.round_picks[rnd_info['round']]['450']['points'] }} | {{ player.round_picks[rnd_info['round']]['250']['points'] }}
                            </div>
                            {% else %}
                            <div style="font-size: 0.75em; color: #999; font-style: italic;">
                                - | -
                            </div>
                            {% endif %}
                        </td>
                        {% endfor %}
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        <p style="margin-top: 20px; color: #b0b0b0; font-size: 0.9em;">
            <span class="random-pick">Red text</span> = auto-pick (missed deadline) | 
            <span style="color: #f39c12;">Orange header</span> = picks visible, results pending
        </p>
        {% endif %}
        
        <div style="margin-top: 30px;">
            <a href="/dashboard" class="link">← Back to Dashboard</a>
        </div>
    </div>
    ''', player_data=player_data, visible_rounds=visible_rounds, session=session, 
         get_race_type_display=get_race_type_display, 
         view=view, cache_empty=cache_empty, last_updated=last_updated)

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
                <li>Picks lock at <strong>midnight the night before each race</strong> (USA race time)</li>
            </ul>
        </div>
        <div class="card">
            <h3 style="margin-top: 0;">🏟️ 250 East/West System</h3>
            <ul style="line-height: 1.8; margin-left: 20px;">
                <li>The 250 class is split into <strong>East</strong> and <strong>West</strong> divisions during Supercross</li>
                <li>Some rounds are <strong>West only</strong>, some are <strong>East only</strong>, and some are <strong>Combined</strong></li>
                <li>You can only pick from riders racing in that round's division</li>
                <li>During Motocross and SMX, all 250 riders race together</li>
            </ul>
        </div>
        <div class="card">
            <h3 style="margin-top: 0;">⏰ Missed Picks?</h3>
            <ul style="line-height: 1.8; margin-left: 20px;">
                <li>If you forget to pick, <strong>smart auto-picks will be assigned</strong> from the top 10 championship performers</li>
                <li>Auto-picks respect the 3-round rule (won't pick riders you used recently)</li>
                <li>Random picks are shown in <span class="random-pick">red</span> on the leaderboard</li>
                <li><strong>Note:</strong> Admin assigns auto-picks after each deadline - check back after race day</li>
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
                <tr><td style="border: none;"><strong>5th Place:</strong></td><td style="border: none;">17 points</td></tr>
                <tr><td style="border: none;"><strong>6th-20th:</strong></td><td style="border: none;">16 down to 2 points</td></tr>
                <tr><td style="border: none;"><strong>21st+:</strong></td><td style="border: none;">0 points</td></tr>
            </table>
            <p style="margin-top: 15px;"><strong>Round Score</strong> = 450 pick points + 250 pick points</p>
            <p><strong>Season Winner</strong> = Player with highest total points!</p>
        </div>
        <div class="card">
            <h3 style="margin-top: 0;">🏁 Race Types</h3>
            <ul style="line-height: 1.8; margin-left: 20px;">
                <li><strong>🏟️ Supercross</strong> - Indoor stadium racing (Jan-May)</li>
                <li><strong>🏞️ Motocross</strong> - Outdoor track racing (May-Aug)</li>
                <li><strong>🏁 SuperMotocross</strong> - Championship playoffs (Sept)</li>
            </ul>
            <p style="margin-top: 15px;">View separate leaderboards for each race type or see overall standings!</p>
        </div>
        <div style="margin-top: 30px;">
            <a href="/dashboard" class="link">← Back to Dashboard</a>
        </div>
    </div>
    ''')

@app.route('/admin/schedule', methods=['GET', 'POST'])
def admin_schedule():
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    conn = get_db_connection()
    c = conn.cursor()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            round_num = request.form.get('round')
            race_date = request.form.get('race_date')
            location = request.form.get('location')
            race_type = request.form.get('race_type')
            class_250 = request.form.get('class_250')
            try:
                c.execute('INSERT INTO schedule (round, race_date, location, race_type, class_250) VALUES (%s, %s, %s, %s, %s)',
                          (round_num, race_date, location, race_type, class_250))
                conn.commit()
                flash('Round added successfully!')
            except psycopg2.IntegrityError:
                flash('Round number already exists')
        elif action == 'delete':
            round_id = request.form.get('round_id')
            # Get round number first so we can cascade-delete picks and results
            c.execute('SELECT round FROM schedule WHERE id = %s', (round_id,))
            sched_row = c.fetchone()
            if sched_row:
                round_num_to_delete = sched_row['round']
                c.execute('DELETE FROM results WHERE round_num = %s', (round_num_to_delete,))
                c.execute('DELETE FROM picks WHERE round_num = %s', (round_num_to_delete,))
                c.execute('DELETE FROM user_round_points WHERE round_num = %s', (round_num_to_delete,))
            c.execute('DELETE FROM schedule WHERE id = %s', (round_id,))
            conn.commit()
            flash('Round deleted successfully (picks, results, and cache entries removed)!')
    c.execute('SELECT * FROM schedule ORDER BY round')
    schedule = c.fetchall()
    conn.close()
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>🔧 Manage Schedule</h1>
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="flash">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <div class="card">
            <h3 style="margin-top: 0;">Add New Round</h3>
            <form method="post">
                <input type="hidden" name="action" value="add">
                <div class="form-row">
                    <div>
                        <label><strong>Round #</strong></label>
                        <input type="number" name="round" required min="1">
                    </div>
                    <div>
                        <label><strong>Race Date</strong></label>
                        <input type="date" name="race_date" required>
                    </div>
                    <div>
                        <label><strong>Location</strong></label>
                        <input type="text" name="location" required placeholder="City, State">
                    </div>
                </div>
                <div class="form-row">
                    <div>
                        <label><strong>Race Type</strong></label>
                        <select name="race_type" required>
                            <option value="supercross">🏟️ Supercross</option>
                            <option value="motocross">🏞️ Motocross</option>
                            <option value="SMX">🏁 SMX</option>
                        </select>
                    </div>
                    <div>
                        <label><strong>250 Class</strong></label>
                        <select name="class_250" required>
                            <option value="West">West</option>
                            <option value="East">East</option>
                            <option value="Combined">Combined</option>
                        </select>
                    </div>
                </div>
                <button type="submit" class="btn">Add Round</button>
            </form>
        </div>
        <h3>Current Schedule</h3>
        <table>
            <thead>
                <tr>
                    <th>Round</th>
                    <th>Date</th>
                    <th>Location</th>
                    <th>Race Type</th>
                    <th>250 Class</th>
                    <th>Delete</th>
                </tr>
            </thead>
            <tbody>
                {% for s in schedule %}
                <tr>
                    <td><strong>{{ s['round'] }}</strong></td>
                    <td>{{ s['race_date'].strftime('%b %d, %Y') }}</td>
                    <td>{{ s['location'] }}</td>
                    <td>
                        <span class="race-type-badge" style="background: {{ get_race_type_display(s['race_type'])['color'] }}20; color: {{ get_race_type_display(s['race_type'])['color'] }}; border: 1px solid {{ get_race_type_display(s['race_type'])['color'] }};">
                            {{ get_race_type_display(s['race_type'])['emoji'] }} {{ get_race_type_display(s['race_type'])['name'] }}
                        </span>
                    </td>
                    <td>{{ s['class_250'] }}</td>
                    <td>
                        <form method="post" style="display:inline;" onsubmit="return confirm('Delete Round {{ s['round'] }}?');">
                            <input type="hidden" name="action" value="delete">
                            <input type="hidden" name="round_id" value="{{ s['id'] }}">
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
    ''', schedule=schedule, get_race_type_display=get_race_type_display)

@app.route('/admin/riders', methods=['GET', 'POST'])
def admin_riders():
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    conn = get_db_connection()
    c = conn.cursor()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'add':
            name = request.form.get('name')
            rider_class = request.form.get('class')
            try:
                c.execute('INSERT INTO riders (name, class, active) VALUES (%s, %s, TRUE)', (name, rider_class))
                conn.commit()
                flash(f'Rider {name} added successfully!')
            except psycopg2.IntegrityError:
                flash('Rider already exists')
        elif action == 'toggle':
            rider_id = request.form.get('rider_id')
            c.execute('UPDATE riders SET active = NOT active WHERE id = %s', (rider_id,))
            conn.commit()
            flash('Rider status updated!')
        elif action == 'delete':
            rider_id = request.form.get('rider_id')
            c.execute('DELETE FROM riders WHERE id = %s', (rider_id,))
            conn.commit()
            flash('Rider deleted!')
    c.execute('SELECT * FROM riders ORDER BY class, name')
    riders = c.fetchall()
    conn.close()
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>🔧 Manage Riders</h1>
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="flash">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <div class="card">
            <h3 style="margin-top: 0;">Add New Rider</h3>
            <form method="post">
                <input type="hidden" name="action" value="add">
                <div class="form-row">
                    <div>
                        <label><strong>Rider Name</strong></label>
                        <input type="text" name="name" required placeholder="First Last">
                    </div>
                    <div>
                        <label><strong>Class</strong></label>
                        <select name="class" required>
                            <option value="450">450</option>
                            <option value="250_West">250 West</option>
                            <option value="250_East">250 East</option>
                        </select>
                    </div>
                </div>
                <button type="submit" class="btn">Add Rider</button>
            </form>
        </div>
        <h3>Current Riders</h3>
        {% for class_name in ['450', '250_West', '250_East'] %}
        <h4 style="color: #d4a574; margin-top: 30px;">
            {% if class_name == '450' %}450 Class
            {% elif class_name == '250_West' %}250 West
            {% else %}250 East{% endif %}
        </h4>
        <table>
            <thead>
                <tr>
                    <th>Name</th>
                    <th>Status</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {% for rider in riders %}
                {% if rider['class'] == class_name %}
                <tr>
                    <td style="font-weight: 600;">{{ rider['name'] }}</td>
                    <td>
                        {% if rider['active'] %}
                            <span style="color: #27ae60;">✓ Active</span>
                        {% else %}
                            <span style="color: #e74c3c;">✗ Inactive</span>
                        {% endif %}
                    </td>
                    <td>
                        <form method="post" style="display:inline;">
                            <input type="hidden" name="action" value="toggle">
                            <input type="hidden" name="rider_id" value="{{ rider['id'] }}">
                            <button type="submit" class="btn btn-small">
                                {% if rider['active'] %}Deactivate{% else %}Activate{% endif %}
                            </button>
                        </form>
                        <form method="post" style="display:inline;" onsubmit="return confirm('Delete {{ rider['name'] }}?');">
                            <input type="hidden" name="action" value="delete">
                            <input type="hidden" name="rider_id" value="{{ rider['id'] }}">
                            <button type="submit" class="btn btn-small btn-danger">Delete</button>
                        </form>
                    </td>
                </tr>
                {% endif %}
                {% endfor %}
            </tbody>
        </table>
        {% endfor %}
        <div style="margin-top: 30px;">
            <a href="/dashboard" class="link">← Back to Dashboard</a>
        </div>
    </div>
    ''', riders=riders)

@app.route('/admin/fetch-results/<int:round_num>')
def fetch_results(round_num):
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    
    round_info = get_round_info(round_num)
    if not round_info:
        flash('Invalid round')
        return redirect(url_for('admin_results_selector'))
    
    location = round_info['location']
    race_type = round_info['race_type']
    
    # Try to fetch event ID
    event_id = get_event_id(round_num)
    
    conn = get_db_connection()
    c = conn.cursor()
    results_450 = {}
    results_250 = {}
    fetch_errors = []

    try:
        if event_id:
            # Try to fetch results for each class
            for cls in ['450', '250']:
                try:
                    url = get_overall_url(event_id, cls)
                    if url:
                        results = parse_results(url, cls, round_num)
                        if results:
                            if cls == '450':
                                results_450 = results
                            else:
                                results_250 = results

                            # Save results to database
                            for rider, pos in results.items():
                                c.execute('DELETE FROM results WHERE round_num = %s AND class = %s AND rider = %s',
                                          (round_num, cls, rider))
                                c.execute('INSERT INTO results (round_num, class, rider, position) VALUES (%s, %s, %s, %s)',
                                          (round_num, cls, rider, pos))
                    else:
                        fetch_errors.append(f'Could not find results URL for {cls} class')
                except Exception as e:
                    fetch_errors.append(f'Error fetching {cls} results: {str(e)}')
        else:
            fetch_errors.append(f'Could not find event on supermotocross.com for {location}')

        conn.commit()
    except Exception as e:
        conn.rollback()
        fetch_errors.append(f'Database error: {str(e)}')
    finally:
        conn.close()
    
    # Provide detailed feedback
    total_results = len(results_450) + len(results_250)
    
    if total_results > 0:
        flash(f'✓ Auto-fetch completed for Round {round_num}! Found {len(results_450)} riders in 450 class, {len(results_250)} riders in 250 class.')
        if fetch_errors:
            for error in fetch_errors:
                flash(f'⚠️ {error}')
    else:
        flash(f'⚠️ No results found for Round {round_num} ({location}). The results may not be available yet on supermotocross.com, or you may need to enter them manually.')
        for error in fetch_errors:
            flash(f'Debug: {error}')
    
    return redirect(url_for('admin_results_selector'))

@app.route('/admin/assign-autopicks/<int:round_num>')
def admin_assign_autopicks(round_num):
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    
    round_info = get_round_info(round_num)
    if not round_info:
        flash('Invalid round')
        return redirect(url_for('dashboard'))
    
    # Check if deadline has passed
    deadline = get_deadline_for_round(round_num)
    now_utc = datetime.now(timezone.utc)
    
    if deadline and now_utc <= deadline:
        flash(f'Deadline for Round {round_num} has not passed yet. Cannot assign auto-picks.')
        return redirect(url_for('admin_results_selector'))
    
    conn = get_db_connection()
    c = conn.cursor()
    
    # Get all users
    c.execute('SELECT id, username FROM users')
    all_users = c.fetchall()
    
    riders_450 = get_riders_by_class('450')
    riders_250 = get_available_250_riders(round_num)
    
    assigned_count = 0
    
    for user in all_users:
        user_id = user['id']
        username = user['username']
        
        # Check if user already has picks for this round
        c.execute('SELECT COUNT(*) as count FROM picks WHERE user_id = %s AND round_num = %s', 
                  (user_id, round_num))
        has_picks = c.fetchone()['count'] > 0
        
        if not has_picks:
            # Get riders the user picked within 2 rounds in EITHER direction (3-round rule)
            # This handles cases where admin runs auto-picks out of order
            # Check rounds: round_num-2, round_num-1, round_num+1, round_num+2
            nearby_rounds = [round_num-2, round_num-1, round_num+1, round_num+2]
            # Filter out invalid round numbers (0 or negative)
            nearby_rounds = [r for r in nearby_rounds if r > 0]
            
            c.execute('''SELECT rider FROM picks 
                        WHERE user_id = %s AND class = %s AND round_num = ANY(%s)''',
                      (user_id, '450', nearby_rounds))
            excluded_450 = [r['rider'] for r in c.fetchall()]
            
            c.execute('''SELECT rider FROM picks 
                        WHERE user_id = %s AND class = %s AND round_num = ANY(%s)''',
                      (user_id, '250', nearby_rounds))
            excluded_250 = [r['rider'] for r in c.fetchall()]
            
            # Get top 10 riders by points, excluding recently picked riders
            top_450 = get_top_riders_by_points('450', round_num, excluded_450)
            top_250 = get_top_riders_by_points('250', round_num, excluded_250)
            
            # Pick randomly from top riders
            random_450 = random.choice(top_450) if top_450 else None
            random_250 = random.choice(top_250) if top_250 else None
            
            if random_450 and random_250:
                c.execute('INSERT INTO picks (user_id, round_num, class, rider, auto_random) VALUES (%s, %s, %s, %s, %s)',
                          (user_id, round_num, '450', random_450, 1))
                c.execute('INSERT INTO picks (user_id, round_num, class, rider, auto_random) VALUES (%s, %s, %s, %s, %s)',
                          (user_id, round_num, '250', random_250, 1))
                assigned_count += 1
    
    conn.commit()
    conn.close()
    
    flash(f'Auto-picks assigned to {assigned_count} user(s) who missed Round {round_num} deadline')
    return redirect(url_for('admin_results_selector'))

@app.route('/admin/recalculate-leaderboard')
def admin_recalculate_leaderboard():
    """Admin-only route to recalculate the entire leaderboard cache"""
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    
    try:
        num_users, num_rounds = recalculate_leaderboard()
        flash(f'✓ Leaderboard recalculated successfully! Processed {num_users} users across {num_rounds} rounds.')
    except Exception as e:
        flash(f'⚠️ Error recalculating leaderboard: {str(e)}')
    
    return redirect(url_for('admin_results_selector'))

@app.route('/admin/results-selector')
def admin_results_selector():
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    schedule = get_schedule()

    now_utc = datetime.now(timezone.utc)

    # Pre-calculate deadline_passed for each round in Python (avoids N DB calls in template)
    schedule_with_status = []
    for s in schedule:
        tz = get_location_timezone(s['location'])
        deadline = datetime.combine(s['race_date'], datetime.min.time(), tzinfo=tz)
        s_dict = dict(s)
        s_dict['deadline_passed'] = now_utc > deadline
        schedule_with_status.append(s_dict)

    # Get last leaderboard update time
    last_updated = get_leaderboard_last_updated()

    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>🔧 Admin - Rounds Management</h1>
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="flash">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        
        <!-- LEADERBOARD RECALCULATION SECTION -->
        <div class="card" style="background: #2a4a2a; border-left-color: #27ae60; margin-bottom: 20px;">
            <h3 style="margin-top: 0; color: #27ae60;">📊 Leaderboard Cache</h3>
            <p style="color: #b0b0b0; margin-bottom: 15px;">
                The leaderboard is cached for fast loading. After entering results or assigning auto-picks, 
                click the button below to update the leaderboard.
            </p>
            {% if last_updated %}
            <p style="color: #888; font-size: 0.9em; margin-bottom: 15px;">
                Last updated: {{ last_updated['updated_at'].strftime('%b %d, %Y at %I:%M %p') }} UTC
            </p>
            {% else %}
            <p style="color: #f39c12; font-size: 0.9em; margin-bottom: 15px;">
                ⚠️ Leaderboard has never been calculated. Click below to initialize.
            </p>
            {% endif %}
            <a href="/admin/recalculate-leaderboard" class="btn" 
               style="background: #27ae60;"
               onclick="this.innerHTML='⏳ Calculating...'; this.style.pointerEvents='none';">
               🔄 Recalculate Leaderboard
            </a>
        </div>
        
        <div class="card">
            <h3 style="margin-top: 0;">Manage rounds - Enter results or assign auto-picks:</h3>
            <table>
                <thead>
                    <tr>
                        <th>Round</th>
                        <th>Date</th>
                        <th>Location</th>
                        <th>Race Type</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for s in schedule_with_status %}
                    <tr>
                        <td><strong>{{ s['round'] }}</strong></td>
                        <td>{{ s['race_date'].strftime('%b %d, %Y') }}</td>
                        <td>{{ s['location'] }}</td>
                        <td>
                            <span class="race-type-badge" style="background: {{ get_race_type_display(s['race_type'])['color'] }}20; color: {{ get_race_type_display(s['race_type'])['color'] }}; border: 1px solid {{ get_race_type_display(s['race_type'])['color'] }};">
                                {{ get_race_type_display(s['race_type'])['emoji'] }} {{ get_race_type_display(s['race_type'])['name'] }}
                            </span>
                        </td>
                        <td>
                            {% if s['deadline_passed'] %}
                                <span style="color: #e74c3c;">⏰ Deadline Passed</span>
                            {% else %}
                                <span style="color: #27ae60;">✓ Open for Picks</span>
                            {% endif %}
                        </td>
                        <td>
                            <a href="/admin/{{ s['round'] }}" class="btn btn-small">Enter Results</a>
                            {% if s['deadline_passed'] %}
                                <a href="/admin/fetch-results/{{ s['round'] }}" class="btn btn-small" 
                                   style="background: #27ae60;"
                                   onclick="return confirm('Auto-fetch results from supermotocross.com for Round {{ s['round'] }}?');">
                                   Auto-Fetch
                                </a>
                                <a href="/admin/assign-autopicks/{{ s['round'] }}" class="btn btn-small" 
                                   style="background: #f39c12;" 
                                   onclick="return confirm('Assign auto-picks to all users who missed Round {{ s['round'] }}?');">
                                   Auto-Pick All
                                </a>
                            {% endif %}
                        </td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
        <div style="margin-top: 30px;">
            <a href="/dashboard" class="link">← Back to Dashboard</a>
        </div>
    </div>
    ''', schedule_with_status=schedule_with_status, get_race_type_display=get_race_type_display,
         last_updated=last_updated)

@app.route('/admin/<int:round_num>', methods=['GET', 'POST'])
def admin_results(round_num):
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    round_info = get_round_info(round_num)
    if not round_info:
        flash('Invalid round')
        return redirect(url_for('dashboard'))
    riders_450 = get_riders_by_class('450')
    riders_250 = get_available_250_riders(round_num)
    if request.method == 'POST':
        conn = get_db_connection()
        c = conn.cursor()
        try:
            # Collect submitted positions and check for duplicates per class
            submitted = {}
            for cls, riders in [('450', riders_450), ('250', riders_250)]:
                positions_seen = {}
                for rider in riders:
                    pos_str = request.form.get(f'{cls}_{rider.replace(" ", "_")}')
                    if pos_str and pos_str.isdigit():
                        pos = int(pos_str)
                        if pos in positions_seen:
                            flash(f'⚠️ Duplicate position {pos} in {cls} class '
                                  f'({rider} and {positions_seen[pos]}). Fix and resubmit.')
                            conn.close()
                            return redirect(url_for('admin_results', round_num=round_num))
                        positions_seen[pos] = rider
                        submitted.setdefault(cls, []).append((rider, pos))

            # All valid — apply to DB
            saved_count = 0
            for cls, entries in submitted.items():
                for rider, pos in entries:
                    c.execute('DELETE FROM results WHERE round_num = %s AND class = %s AND rider = %s',
                              (round_num, cls, rider))
                    c.execute('INSERT INTO results (round_num, class, rider, position) VALUES (%s, %s, %s, %s)',
                              (round_num, cls, rider, pos))
                    saved_count += 1

            conn.commit()
            if saved_count > 0:
                flash(f'✓ Manual results saved — {saved_count} rider positions recorded.')
            else:
                flash('No positions were entered. Fill in at least one position to save.')
        except Exception as e:
            conn.rollback()
            flash(f'⚠️ Error saving results: {str(e)}')
        finally:
            conn.close()
    location = round_info['location'].split(',')[0]
    race_type_info = get_race_type_display(round_info['race_type'])
    return render_template_string(get_base_style() + '''
    <div class="container">
        <h1>🔧 Manual Results Entry - Round {{ round_num }} {{ location }}
            <span class="race-type-badge" style="background: {{ race_type_info['color'] }}20; color: {{ race_type_info['color'] }}; border: 1px solid {{ race_type_info['color'] }};">
                {{ race_type_info['emoji'] }} {{ race_type_info['name'] }}
            </span>
        </h1>
        {% with messages = get_flashed_messages() %}
            {% if messages %}
                {% for message in messages %}
                    <div class="flash">{{ message }}</div>
                {% endfor %}
            {% endif %}
        {% endwith %}
        <form method="post">
            {% for cls, riders in [('450', riders_450), ('250', riders_250)] %}
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
    ''', round_num=round_num, riders_450=riders_450, riders_250=riders_250, 
         location=location, race_type_info=race_type_info)

@app.route('/admin/manage-users', methods=['GET', 'POST'])
def admin_manage_users():
    if session.get('username') != 'admin':
        return redirect(url_for('login'))
    conn = get_db_connection()
    c = conn.cursor()
    if request.method == 'POST':
        action = request.form.get('action')
        user_id = request.form.get('user_id')
        if action == 'reset_password':
            new_password = request.form.get('new_password')
            if new_password and len(new_password) >= 6:
                new_pass_hash = generate_password_hash(new_password)
                c.execute('UPDATE users SET password = %s WHERE id = %s', (new_pass_hash, user_id))
                conn.commit()
                flash('Password reset successfully!')
            else:
                flash('Password must be at least 6 characters')
        elif action == 'delete_user':
            if int(user_id) == session.get('user_id'):
                flash('Cannot delete your own admin account!')
            else:
                c.execute('DELETE FROM picks WHERE user_id = %s', (user_id,))
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
        <div class="card" style="margin-bottom: 20px;">
            <h3 style="margin-top: 0;">Admin Password Reset</h3>
            <p style="color: #b0b0b0;">
                Use this section to reset passwords for users who have forgotten their password.
            </p>
        </div>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Username</th>
                    <th>Email</th>
                    <th style="width: 300px;">Reset Password</th>
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
                        <form method="post" style="display: flex; gap: 10px; align-items: center; margin: 0;">
                            <input type="hidden" name="action" value="reset_password">
                            <input type="hidden" name="user_id" value="{{ user['id'] }}">
                            <input type="password" name="new_password" placeholder="New password (min 6 chars)" required 
                                   style="width: 180px; margin: 0; padding: 8px;" minlength="6">
                            <button type="submit" class="btn btn-small" style="margin: 0;">Reset</button>
                        </form>
                    </td>
                    <td>
                        <form method="post" style="display:inline; margin: 0;" 
                              onsubmit="return confirm('Delete {{ user['username'] }}? This will also delete all their picks.');">
                            <input type="hidden" name="action" value="delete_user">
                            <input type="hidden" name="user_id" value="{{ user['id'] }}">
                            <button type="submit" class="btn btn-small btn-danger" style="margin: 0;">Delete</button>
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
    tables = ['users', 'picks', 'results', 'schedule', 'riders']
    zip_buffer = BytesIO()
    with ZipFile(zip_buffer, 'w') as zip_file:
        for table in tables:
            c.execute(f'SELECT * FROM {table}')
            rows = c.fetchall()
            csv_buffer = StringIO()
            csv_writer = csv.writer(csv_buffer)
            if rows:
                csv_writer.writerow(rows[0].keys())
                for row in rows:
                    csv_writer.writerow(row.values())
            else:
                # Fetch column names from information schema so empty tables still have headers
                c.execute('''SELECT column_name FROM information_schema.columns
                             WHERE table_name = %s ORDER BY ordinal_position''', (table,))
                col_names = [r['column_name'] for r in c.fetchall()]
                csv_writer.writerow(col_names)
            zip_file.writestr(f'{table}.csv', csv_buffer.getvalue())
    conn.close()
    zip_buffer.seek(0)
    return send_file(zip_buffer, as_attachment=True, download_name='fantasy_league_export.zip', mimetype='application/zip')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

if __name__ == '__main__':
    app.run(debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true')
