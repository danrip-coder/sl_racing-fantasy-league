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
    c.execute('SELECT id, username, email FROM users')
    users = c.fetchall()
    conn.close()
    return render_template_string('''
    <h2>Manage Users (Admin Only)</h2>
    <table border="1">
        <tr><th>ID</th><th>Username</th><th>Email</th><th>Reset Password</th></tr>
        {% for uid, uname, email in users %}
        <tr>
            <td>{{ uid }}</td>
            <td>{{ uname }}</td>
            <td>{{ email or 'No email' }}</td>
            <td>
                <form method="post" style="display:inline;">
                    <input type="hidden" name="user_id" value="{{ uid }}">
                    <input type="password" name="new_password" placeholder="New password" required>
                    <input type="submit" value="Reset">
                </form>
            </td>
        </tr>
        {% endfor %}
    </table>
    <a href="/dashboard">Back to Dashboard</a>
    ''', users=users)
