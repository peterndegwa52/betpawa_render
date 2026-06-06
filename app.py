import os, json
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, g, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from functools import wraps, reduce
from db import init_db, query, execute
from match_engine import (start_scheduler, generate_odds, LEAGUES,
                           active_simulations, create_next_matchday,
                           admin_set_score, admin_force_start, admin_force_finish)

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'betpawa-secret-change-in-production')

init_db(app)

# ── Jinja filters ──────────────────────────────────────────────────────────
def fmt(v):
    try:    return f"{float(v):,.2f}"
    except: return "0.00"

app.jinja_env.filters['money']    = fmt
app.jinja_env.filters['tzs']      = lambda v: fmt(v) + " TZS"
app.jinja_env.filters['fromjson'] = lambda s: json.loads(s or '{}')
app.jinja_env.filters['zfill']    = lambda s, w: str(s).zfill(w)

# ── Before request ─────────────────────────────────────────────────────────
@app.before_request
def load_user():
    g.user = None
    if 'user_id' in session:
        u = query("SELECT * FROM users WHERE id=?", (session['user_id'],), one=True)
        if u:
            g.user = u
            session['balance'] = fmt(u['balance'])

# ── Decorators ─────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if not g.user:
            flash('Please login to continue.', 'warning')
            return redirect(url_for('login'))
        return f(*a, **kw)
    return dec

def admin_required(f):
    @wraps(f)
    def dec(*a, **kw):
        if not g.user or g.user['role'] != 'admin':
            flash('Admin access required.', 'danger')
            return redirect(url_for('login'))
        return f(*a, **kw)
    return dec

def log_admin(action, details=''):
    if 'user_id' in session:
        execute("INSERT INTO admin_logs (admin_id,action,details) VALUES (?,?,?)",
                (session['user_id'], action, details))

# ══════════════════════════════════════════════════════════════════════════
# AUTH ROUTES
# ══════════════════════════════════════════════════════════════════════════
@app.route('/')
def index():
    return redirect(url_for('virtuals'))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        phone = request.form.get('phone','').strip()
        pw    = request.form.get('password','')
        user  = query("SELECT * FROM users WHERE phone=? OR username=?",
                      (phone, phone), one=True)
        if not user or not check_password_hash(user['password_hash'], pw):
            flash('Invalid credentials.', 'danger')
            return render_template('login.html')
        if not user['is_active']:
            flash('Account suspended. Contact support.', 'danger')
            return render_template('login.html')
        session.clear()
        session.update({
            'user_id':  user['id'],
            'username': user['username'],
            'role':     user['role'],
            'balance':  fmt(user['balance'])
        })
        return redirect(url_for('admin_dashboard') if user['role'] == 'admin'
                        else url_for('virtuals'))
    return render_template('login.html')

@app.route('/register', methods=['GET','POST'])
def register():
    if request.method == 'POST':
        phone    = request.form.get('phone','').strip()
        username = request.form.get('username','').strip()
        pw       = request.form.get('password','')
        if not phone or not username or not pw:
            flash('All fields are required.', 'danger')
            return render_template('register.html')
        if len(pw) < 6:
            flash('Password must be at least 6 characters.', 'danger')
            return render_template('register.html')
        if query("SELECT id FROM users WHERE phone=? OR username=?",
                 (phone, username), one=True):
            flash('Phone number or username already registered.', 'danger')
            return render_template('register.html')
        execute("INSERT INTO users (username,phone,password_hash) VALUES (?,?,?)",
                (username, phone, generate_password_hash(pw)))
        flash('Account created! Please login.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ══════════════════════════════════════════════════════════════════════════
# USER — VIRTUALS LOBBY
# ══════════════════════════════════════════════════════════════════════════
@app.route('/virtuals')
def virtuals():
    league_filter = request.args.get('league', 'all')
    matchdays = {}
    for lk in LEAGUES:
        if league_filter not in ('all', lk):
            continue
        md = query("""SELECT * FROM matchdays
                      WHERE league=? AND status IN ('upcoming','live')
                      ORDER BY CASE status WHEN 'live' THEN 0 ELSE 1 END, starts_at
                      LIMIT 1""", (lk,), one=True)
        if not md:
            md = query("""SELECT * FROM matchdays WHERE league=?
                          ORDER BY created_at DESC LIMIT 1""", (lk,), one=True)
        if md:
            matches = query("""SELECT * FROM matches WHERE matchday_id=?
                               ORDER BY kickoff_time""", (md['id'],))
            matchdays[lk] = {'info': md, 'matches': matches}

    return render_template('virtuals.html', matchdays=matchdays,
                           league_filter=league_filter, leagues=LEAGUES, user=g.user)

# ── Match detail ───────────────────────────────────────────────────────────
@app.route('/match/<int:mid>')
def match_detail(mid):
    match = query("SELECT * FROM matches WHERE id=?", (mid,), one=True)
    if not match:
        flash('Match not found or already finished.', 'warning')
        return redirect(url_for('virtuals'))
    md     = query("SELECT * FROM matchdays WHERE id=?",
                   (match['matchday_id'],), one=True)
    odds   = json.loads(match['odds_json'] or '{}')
    events = query("""SELECT * FROM match_events WHERE match_id=?
                      ORDER BY minute DESC LIMIT 30""", (mid,))
    betslip = session.get('betslip', [])
    return render_template('match_detail.html', match=match, md=md, odds=odds,
                           events=events, betslip=betslip,
                           user=g.user, leagues=LEAGUES)

# ── Live poll endpoint ─────────────────────────────────────────────────────
@app.route('/match/<int:mid>/poll')
def match_poll(mid):
    m = query("SELECT * FROM matches WHERE id=?", (mid,), one=True)
    if not m:
        return jsonify({'status': 'deleted'}), 200   # match cleaned up
    events = query("""SELECT * FROM match_events WHERE match_id=?
                      ORDER BY minute DESC LIMIT 10""", (mid,))
    return jsonify({
        'home_score':     m['home_score'],
        'away_score':     m['away_score'],
        'current_minute': m['current_minute'],
        'status':         m['status'],
        'ht_home':        m['ht_home'],
        'ht_away':        m['ht_away'],
        'preset_home':    m['preset_home'],
        'preset_away':    m['preset_away'],
        'events':         [dict(e) for e in events],
    })

# ══════════════════════════════════════════════════════════════════════════
# BETSLIP
# ══════════════════════════════════════════════════════════════════════════
@app.route('/betslip/add', methods=['POST'])
def betslip_add():
    data    = request.get_json()
    betslip = session.get('betslip', [])
    mid, mkt = data['match_id'], data['market']
    betslip = [b for b in betslip
               if not (b['match_id'] == mid and b['market'] == mkt)]
    betslip.append({
        'match_id':        mid,
        'match_label':     data['match_label'],
        'market':          mkt,
        'market_label':    data['market_label'],
        'selection':       data['selection'],
        'selection_label': data['selection_label'],
        'odds':            data['odds'],
    })
    session['betslip'] = betslip
    session.modified = True
    return jsonify({'count': len(betslip)})

@app.route('/betslip/remove', methods=['POST'])
def betslip_remove():
    data    = request.get_json()
    betslip = session.get('betslip', [])
    betslip = [b for b in betslip
               if not (b['match_id'] == data['match_id']
                       and b['market'] == data['market'])]
    session['betslip'] = betslip
    session.modified = True
    return jsonify({'count': len(betslip)})

@app.route('/betslip/clear', methods=['POST'])
def betslip_clear():
    session['betslip'] = []
    session.modified = True
    return jsonify({'ok': True})

@app.route('/betslip/place', methods=['POST'])
@login_required
def place_bet():
    betslip = session.get('betslip', [])
    if not betslip:
        flash('Your betslip is empty.', 'danger')
        return redirect(url_for('virtuals'))
    try:
        stake = float(request.form.get('stake', 0))
    except ValueError:
        flash('Invalid stake amount.', 'danger')
        return redirect(url_for('virtuals'))
    if stake < 100:
        flash('Minimum stake is 100 TZS.', 'danger')
        return redirect(url_for('virtuals'))
    if stake > 1_000_000:
        flash('Maximum stake is 1,000,000 TZS.', 'danger')
        return redirect(url_for('virtuals'))
    user = g.user
    if user['balance'] < stake:
        flash('Insufficient balance. Please deposit first.', 'danger')
        return redirect(url_for('account'))

    total_odds = round(
        reduce(lambda a, b: a * b, [float(s['odds']) for s in betslip], 1.0), 2)
    potential  = round(stake * total_odds, 2)

    execute("UPDATE users SET balance=balance-? WHERE id=?", (stake, user['id']))
    bid = execute("INSERT INTO bets (user_id,total_stake,potential_win) VALUES (?,?,?)",
                  (user['id'], stake, potential))
    for sel in betslip:
        execute("""INSERT INTO bet_selections
                   (bet_id,match_id,market,selection,odds) VALUES (?,?,?,?,?)""",
                (bid, sel['match_id'], sel['market'], sel['selection'], sel['odds']))
    execute("INSERT INTO transactions (user_id,type,amount,status,note) VALUES (?,?,?,?,?)",
            (user['id'], 'bet', -stake, 'confirmed', f'Bet #{bid}'))

    session['betslip'] = []
    session['balance']  = fmt(user['balance'] - stake)
    flash(f'Bet placed! ✅ Potential win: {fmt(potential)} TZS', 'success')
    return redirect(url_for('my_bets'))

# ══════════════════════════════════════════════════════════════════════════
# USER — ACCOUNT
# ══════════════════════════════════════════════════════════════════════════
@app.route('/account')
@login_required
def account():
    txns = query("""SELECT * FROM transactions WHERE user_id=?
                    ORDER BY created_at DESC LIMIT 30""", (g.user['id'],))
    return render_template('account.html', user=g.user, txns=txns)

@app.route('/deposit', methods=['GET','POST'])
@login_required
def deposit():
    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', 0))
        except ValueError:
            flash('Invalid amount.', 'danger')
            return render_template('deposit.html', user=g.user)
        method = request.form.get('method', 'mpesa')
        phone  = request.form.get('phone', g.user['phone'] or '')
        if amount < 500:
            flash('Minimum deposit is 500 TZS.', 'danger')
            return render_template('deposit.html', user=g.user)
        import time as _time
        ref = f"DEP{g.user['id']}{int(_time.time())}"
        execute("""INSERT INTO transactions
                   (user_id,type,amount,status,reference,note) VALUES (?,?,?,?,?,?)""",
                (g.user['id'], 'deposit', amount, 'pending',
                 ref, f'{method.upper()} – {phone}'))
        flash(f'✅ Deposit request submitted! Ref: {ref}. '
              f'Admin will confirm shortly.', 'info')
        return redirect(url_for('account'))
    return render_template('deposit.html', user=g.user)

@app.route('/withdraw', methods=['GET','POST'])
@login_required
def withdraw():
    if request.method == 'POST':
        try:
            amount = float(request.form.get('amount', 0))
        except ValueError:
            flash('Invalid amount.', 'danger')
            return render_template('withdraw.html', user=g.user)
        phone = request.form.get('phone', '').strip()
        if amount < 500:
            flash('Minimum withdrawal is 500 TZS.', 'danger')
            return render_template('withdraw.html', user=g.user)
        if g.user['balance'] < amount:
            flash('Insufficient balance.', 'danger')
            return render_template('withdraw.html', user=g.user)
        import time as _time
        ref = f"WIT{g.user['id']}{int(_time.time())}"
        execute("""INSERT INTO transactions
                   (user_id,type,amount,status,reference,note) VALUES (?,?,?,?,?,?)""",
                (g.user['id'], 'withdrawal', -amount, 'pending',
                 ref, f'M-Pesa – {phone}'))
        flash(f'✅ Withdrawal request submitted! Ref: {ref}', 'info')
        return redirect(url_for('account'))
    return render_template('withdraw.html', user=g.user)

@app.route('/my-bets')
@login_required
def my_bets():
    bets = query("""SELECT * FROM bets WHERE user_id=?
                    ORDER BY placed_at DESC LIMIT 50""", (g.user['id'],))
    bets_detail = []
    for bet in bets:
        sels = query("""SELECT bs.*, m.home_team, m.away_team,
                               m.home_score, m.away_score,
                               m.status as match_status,
                               m.home_code, m.away_code
                        FROM bet_selections bs
                        LEFT JOIN matches m ON bs.match_id = m.id
                        WHERE bs.bet_id=?""", (bet['id'],))
        bets_detail.append({'bet': bet, 'selections': sels})
    return render_template('my_bets.html', user=g.user, bets_detail=bets_detail)

# ══════════════════════════════════════════════════════════════════════════
# ADMIN — DASHBOARD
# ══════════════════════════════════════════════════════════════════════════
@app.route('/admin')
@admin_required
def admin_dashboard():
    stats = {
        'users':            query("SELECT COUNT(*) as c FROM users WHERE role='user'",
                                  one=True)['c'],
        'pending_bets':     query("SELECT COUNT(*) as c FROM bets WHERE status='pending'",
                                  one=True)['c'],
        'total_bets':       query("SELECT COUNT(*) as c FROM bets", one=True)['c'],
        'revenue':          query("SELECT COALESCE(SUM(total_stake),0) as s FROM bets "
                                  "WHERE status='lost'", one=True)['s'],
        'pending_deposits': query("SELECT COUNT(*) as c FROM transactions "
                                  "WHERE type='deposit' AND status='pending'", one=True)['c'],
        'live_matches':     query("SELECT COUNT(*) as c FROM matches "
                                  "WHERE status='live'", one=True)['c'],
        'total_balance':    query("SELECT COALESCE(SUM(balance),0) as s FROM users "
                                  "WHERE role='user'", one=True)['s'],
        'won_payouts':      query("SELECT COALESCE(SUM(potential_win),0) as s FROM bets "
                                  "WHERE status='won'", one=True)['s'],
    }
    recent_logs  = query("""SELECT l.*, u.username FROM admin_logs l
                            LEFT JOIN users u ON l.admin_id=u.id
                            ORDER BY l.timestamp DESC LIMIT 15""")
    pending_deps = query("""SELECT t.*, u.username, u.phone FROM transactions t
                            JOIN users u ON t.user_id=u.id
                            WHERE t.type='deposit' AND t.status='pending'
                            ORDER BY t.created_at""")
    pending_wits = query("""SELECT t.*, u.username, u.phone FROM transactions t
                            JOIN users u ON t.user_id=u.id
                            WHERE t.type='withdrawal' AND t.status='pending'
                            ORDER BY t.created_at""")
    return render_template('admin/dashboard.html',
                           stats=stats, recent_logs=recent_logs,
                           pending_deps=pending_deps, pending_wits=pending_wits)

# ── Admin Users ────────────────────────────────────────────────────────────
@app.route('/admin/users')
@admin_required
def admin_users():
    users = query("SELECT * FROM users ORDER BY created_at DESC")
    return render_template('admin/users.html', users=users)

@app.route('/admin/user/<int:uid>/balance', methods=['POST'])
@admin_required
def admin_balance(uid):
    user   = query("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin_users'))
    try:
        amount = float(request.form.get('amount', 0))
    except ValueError:
        flash('Invalid amount.', 'danger')
        return redirect(url_for('admin_users'))
    action = request.form.get('action', 'add')
    note   = request.form.get('note', '').strip() or ('Admin credit' if action=='add' else 'Admin debit')
    if action == 'add':
        execute("UPDATE users SET balance=balance+? WHERE id=?", (amount, uid))
        execute("INSERT INTO transactions (user_id,type,amount,status,note) VALUES (?,?,?,?,?)",
                (uid, 'admin_credit', amount, 'confirmed', note))
    else:
        execute("UPDATE users SET balance=MAX(0,balance-?) WHERE id=?", (amount, uid))
        execute("INSERT INTO transactions (user_id,type,amount,status,note) VALUES (?,?,?,?,?)",
                (uid, 'admin_debit', -amount, 'confirmed', note))
    log_admin('balance_edit',
              f"User {user['username']}: {action} {fmt(amount)} TZS — {note}")
    flash(f'Balance updated for {user["username"]}.', 'success')
    return redirect(url_for('admin_users'))

@app.route('/admin/user/<int:uid>/toggle', methods=['POST'])
@admin_required
def admin_toggle_user(uid):
    user = query("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not user:
        flash('User not found.', 'danger')
        return redirect(url_for('admin_users'))
    new = 0 if user['is_active'] else 1
    execute("UPDATE users SET is_active=? WHERE id=?", (new, uid))
    status = 'Activated' if new else 'Suspended'
    log_admin('toggle_user', f"{status} {user['username']}")
    flash(f'User {user["username"]} {status.lower()}.', 'success')
    return redirect(url_for('admin_users'))

# ── Admin Transactions ─────────────────────────────────────────────────────
@app.route('/admin/transactions')
@admin_required
def admin_transactions():
    status_filter = request.args.get('status', '')
    sql = """SELECT t.*, u.username, u.phone FROM transactions t
             JOIN users u ON t.user_id=u.id WHERE 1=1"""
    params = []
    if status_filter:
        sql += " AND t.status=?"; params.append(status_filter)
    sql += " ORDER BY t.created_at DESC LIMIT 300"
    txns = query(sql, params)
    return render_template('admin/transactions.html', txns=txns,
                           status_filter=status_filter)

@app.route('/admin/transaction/<int:tid>/approve', methods=['POST'])
@admin_required
def admin_approve_txn(tid):
    txn = query("SELECT * FROM transactions WHERE id=?", (tid,), one=True)
    if not txn or txn['status'] != 'pending':
        flash('Transaction not found or already processed.', 'danger')
        return redirect(url_for('admin_transactions'))
    if txn['type'] == 'deposit':
        execute("UPDATE transactions SET status='confirmed' WHERE id=?", (tid,))
        execute("UPDATE users SET balance=balance+? WHERE id=?",
                (txn['amount'], txn['user_id']))
        log_admin('approve_deposit',
                  f"{fmt(txn['amount'])} TZS → user #{txn['user_id']}")
        flash(f"Deposit of {fmt(txn['amount'])} TZS approved. ✅", 'success')
    elif txn['type'] == 'withdrawal':
        user = query("SELECT * FROM users WHERE id=?", (txn['user_id'],), one=True)
        if user['balance'] < abs(txn['amount']):
            flash('User has insufficient balance for this withdrawal.', 'danger')
            return redirect(url_for('admin_transactions'))
        execute("UPDATE transactions SET status='confirmed' WHERE id=?", (tid,))
        execute("UPDATE users SET balance=balance+? WHERE id=?",   # amount is negative
                (txn['amount'], txn['user_id']))
        log_admin('approve_withdrawal',
                  f"{fmt(abs(txn['amount']))} TZS from user #{txn['user_id']}")
        flash('Withdrawal approved. ✅', 'success')
    return redirect(url_for('admin_transactions'))

@app.route('/admin/transaction/<int:tid>/reject', methods=['POST'])
@admin_required
def admin_reject_txn(tid):
    execute("UPDATE transactions SET status='rejected' WHERE id=?", (tid,))
    log_admin('reject_txn', f"Transaction #{tid} rejected")
    flash('Transaction rejected.', 'warning')
    return redirect(url_for('admin_transactions'))

# ── Admin Bets ─────────────────────────────────────────────────────────────
@app.route('/admin/bets')
@admin_required
def admin_bets():
    status = request.args.get('status', '')
    sql    = """SELECT b.*, u.username FROM bets b
                JOIN users u ON b.user_id=u.id WHERE 1=1"""
    params = []
    if status:
        sql += " AND b.status=?"; params.append(status)
    sql += " ORDER BY b.placed_at DESC LIMIT 300"
    bets = query(sql, params)
    return render_template('admin/bets.html', bets=bets, status=status)

@app.route('/admin/bet/<int:bid>/settle', methods=['POST'])
@admin_required
def admin_settle_bet(bid):
    result = request.form.get('result', 'lost')
    bet    = query("SELECT * FROM bets WHERE id=?", (bid,), one=True)
    if not bet:
        flash('Bet not found.', 'danger')
        return redirect(url_for('admin_bets'))
    if result == 'won':
        execute("UPDATE bets SET status='won',settled_at=datetime('now') WHERE id=?",
                (bid,))
        execute("UPDATE users SET balance=balance+? WHERE id=?",
                (bet['potential_win'], bet['user_id']))
        execute("""INSERT INTO transactions
                   (user_id,type,amount,status,note) VALUES (?,?,?,?,?)""",
                (bet['user_id'], 'winnings', bet['potential_win'],
                 'confirmed', f'Bet #{bid} won (manual)'))
    else:
        execute("UPDATE bets SET status='lost',settled_at=datetime('now') WHERE id=?",
                (bid,))
    log_admin('settle_bet', f"Bet #{bid} → {result}")
    flash(f'Bet #{bid} settled as {result}. ✅', 'success')
    return redirect(url_for('admin_bets'))

# ── Admin Matches ──────────────────────────────────────────────────────────
@app.route('/admin/matches')
@admin_required
def admin_matches():
    mds = query("""SELECT * FROM matchdays
                   ORDER BY CASE status
                     WHEN 'live'     THEN 0
                     WHEN 'upcoming' THEN 1
                     ELSE 2 END, starts_at DESC
                   LIMIT 30""")
    return render_template('admin/matches.html', mds=mds, leagues=LEAGUES)

@app.route('/admin/matchday/<int:mdid>/matches')
@admin_required
def admin_matchday_matches(mdid):
    matches = query("SELECT * FROM matches WHERE matchday_id=? ORDER BY kickoff_time",
                    (mdid,))
    return jsonify({'matches': [dict(m) for m in matches]})

@app.route('/admin/match/<int:mid>/set_score', methods=['POST'])
@admin_required
def admin_set_match_score(mid):
    try:
        home = int(request.form.get('home', 0))
        away = int(request.form.get('away', 0))
    except ValueError:
        flash('Invalid score values.', 'danger')
        return redirect(url_for('admin_matches'))
    admin_set_score(mid, home, away)
    match = query("SELECT * FROM matches WHERE id=?", (mid,), one=True)
    if match:
        log_admin('set_score',
                  f"Match #{mid} ({match['home_code']} v {match['away_code']}): "
                  f"preset {home}-{away}")
    flash(f'✅ Preset score {home}-{away} set for match #{mid}.', 'success')
    return redirect(url_for('admin_matches'))

@app.route('/admin/match/<int:mid>/force_start', methods=['POST'])
@admin_required
def admin_force_start_route(mid):
    admin_force_start(mid)
    match = query("SELECT * FROM matches WHERE id=?", (mid,), one=True)
    if match:
        log_admin('force_start',
                  f"Force-started #{mid} "
                  f"({match['home_code']} v {match['away_code']})")
    flash(f'Match #{mid} force-started. ▶', 'success')
    return redirect(url_for('admin_matches'))

@app.route('/admin/match/<int:mid>/force_finish', methods=['POST'])
@admin_required
def admin_force_finish_route(mid):
    match = query("SELECT * FROM matches WHERE id=?", (mid,), one=True)
    admin_force_finish(mid)
    if match:
        log_admin('force_finish',
                  f"Force-finished #{mid} "
                  f"({match['home_code']} v {match['away_code']})")
    flash(f'Match #{mid} force-finished and bets settled. ⏹', 'success')
    return redirect(url_for('admin_matches'))

@app.route('/admin/matchday/create', methods=['POST'])
@admin_required
def admin_create_matchday():
    create_next_matchday(app)
    log_admin('create_matchday', 'Manual matchday creation')
    flash('New matchday batch created! ✅', 'success')
    return redirect(url_for('admin_matches'))

# ── Admin Predictions ──────────────────────────────────────────────────────
@app.route('/admin/predictions')
@admin_required
def admin_predictions():
    upcoming = query("""SELECT m.*, md.matchday_number, md.league
                        FROM matches m
                        JOIN matchdays md ON m.matchday_id=md.id
                        WHERE m.status='upcoming'
                        ORDER BY m.kickoff_time LIMIT 120""")
    predictions = []
    for m in upcoming:
        odds  = json.loads(m['odds_json'] or '{}')
        o1x2  = odds.get('1x2', {})
        h_odd = float(o1x2.get('1', 2.0))
        d_odd = float(o1x2.get('X', 3.5))
        a_odd = float(o1x2.get('2', 3.5))
        ph    = round(1/h_odd*100, 1)
        pd    = round(1/d_odd*100, 1)
        pa    = round(1/a_odd*100, 1)
        cs    = odds.get('cs', {})
        best  = min(
            ((k, v) for k, v in cs.items() if k != 'other'),
            key=lambda x: x[1], default=('1-0', '—')
        )
        predictions.append({
            'match':        m,
            'ph': ph, 'pd': pd, 'pa': pa,
            'likely_score': best[0],
            'cs_odds':      best[1],
            'over25':       odds.get('ou',{}).get('over_2.5', '—'),
            'btts':         odds.get('btts',{}).get('yes', '—'),
            'preset':       m['preset_home'] is not None,
            'preset_score': f"{m['preset_home']}-{m['preset_away']}"
                            if m['preset_home'] is not None else '—',
        })
    return render_template('admin/predictions.html',
                           predictions=predictions, leagues=LEAGUES)

# ── Admin Logs ─────────────────────────────────────────────────────────────
@app.route('/admin/logs')
@admin_required
def admin_logs():
    logs = query("""SELECT l.*, u.username FROM admin_logs l
                    LEFT JOIN users u ON l.admin_id=u.id
                    ORDER BY l.timestamp DESC LIMIT 500""")
    return render_template('admin/logs.html', logs=logs)

# ══════════════════════════════════════════════════════════════════════════
# SEED
# ══════════════════════════════════════════════════════════════════════════
def seed():
    if not query("SELECT id FROM users WHERE username='admin'", one=True):
        execute("INSERT INTO users (username,phone,password_hash,role,balance) "
                "VALUES (?,?,?,?,?)",
                ('admin', '0700000000',
                 generate_password_hash('admin123'), 'admin', 0))
    if not query("SELECT id FROM users WHERE username='demo'", one=True):
        execute("INSERT INTO users (username,phone,password_hash,balance) "
                "VALUES (?,?,?,?)",
                ('demo', '0712345678',
                 generate_password_hash('demo123'), 50000))
    if not query("SELECT id FROM matchdays LIMIT 1", one=True):
        create_next_matchday(app)
        create_next_matchday(app)

if __name__ == '__main__':
    with app.app_context():
        seed()
    start_scheduler(app)
    print("\n" + "="*56)
    print("  betPawa Virtual Sports → http://localhost:5000")
    print("  Admin : admin / admin123  →  /admin")
    print("  Demo  : demo  / demo123")
    print("="*56 + "\n")
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)
