import os
import secrets
import warnings
import random
from datetime import datetime, timedelta
from flask import Flask, render_template, url_for, flash, redirect, request, jsonify, session
from flask_login import LoginManager, login_user, current_user, logout_user, login_required
from flask_migrate import Migrate
from flask_mail import Mail, Message
from flask_wtf.csrf import CSRFProtect
import pandas as pd
from sqlalchemy import func
from models import db, User, Product, Warehouse, Stock, Operation, StockMovement
from forms import LoginForm, RegistrationForm, ProductForm, WarehouseForm, ForgotPasswordForm, ResetPasswordForm, UpdateProfileForm
from utils import load_inventory_data, validate_csv_data, calculate_inventory_metrics

# Suppress warnings
warnings.filterwarnings('ignore', category=UserWarning)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-key-for-local-dev-only')
if os.environ.get('VERCEL'):
    app.config['SESSION_COOKIE_SECURE'] = True  # Required for HTTPS on Vercel
else:
    app.config['SESSION_COOKIE_SECURE'] = False

app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///inventory.db')

# Vercel-specific database adjustments
if os.environ.get('VERCEL'):
    if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:///'):
        # SQLite must be in /tmp/ on Vercel to be writable (though not persistent)
        app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////tmp/inventory.db'

if app.config['SQLALCHEMY_DATABASE_URI'].startswith("postgres://"):
    app.config['SQLALCHEMY_DATABASE_URI'] = app.config['SQLALCHEMY_DATABASE_URI'].replace("postgres://", "postgresql://", 1)
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = 'data_set'
if os.environ.get('VERCEL'):
    app.config['UPLOAD_FOLDER'] = '/tmp'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

# Flask-Mail Configuration for Gmail SMTP
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = 'YOUR_GMAIL_ADDRESS@gmail.com' # REPLACE THIS
app.config['MAIL_PASSWORD'] = 'YOUR_GMAIL_APP_PASSWORD'     # REPLACE THIS
app.config['MAIL_DEFAULT_SENDER'] = 'YOUR_GMAIL_ADDRESS@gmail.com' # REPLACE THIS

db.init_app(app)
migrate = Migrate(app, db)
mail = Mail(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message_category = 'info'
csrf = CSRFProtect(app)

# Flexible requirements: Only date and product_name are strictly required for analytics.
# Others will be filled with defaults if missing.
REQUIRED_ANALYTICS_COLUMNS = {'date', 'product_name'}
OPTIONAL_ANALYTICS_COLUMNS = ['product_id', 'warehouse', 'qty_received', 'qty_delivered', 'adjustment', 'stock_after']


def is_known_product_name(name):
    value = (name or '').strip().lower()
    return bool(value) and 'unknown' not in value


def load_transaction_dataset(csv_path):
    """Load and normalize transaction-style inventory CSV data with flexible columns."""
    if not os.path.exists(csv_path):
        return None

    try:
        df = pd.read_csv(csv_path)
    except Exception:
        return None

    if not REQUIRED_ANALYTICS_COLUMNS.issubset(df.columns):
        return None

    df = df.copy()
    
    # Fill missing optional columns with sensible defaults
    for col in OPTIONAL_ANALYTICS_COLUMNS:
        if col not in df.columns:
            if col in ['qty_received', 'qty_delivered', 'adjustment', 'stock_after']:
                df[col] = 0
            elif col == 'product_id':
                df[col] = range(len(df))
            elif col == 'warehouse':
                df[col] = 'Default Warehouse'

    df['date'] = pd.to_datetime(df['date'], errors='coerce')
    for col in ['qty_received', 'qty_delivered', 'adjustment', 'stock_after']:
        df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    
    df = df.dropna(subset=['date', 'product_name'])
    df = df[df['product_name'].apply(is_known_product_name)]
    return df


def build_transaction_analytics(df):
    """Build analytics payload from transaction-style dataset."""
    price_per_unit = 2500.0

    ordered = df.sort_values('date')
    product_summary = (
        ordered.groupby(['product_id', 'product_name'], as_index=False)
        .agg(
            quantity_stock=('stock_after', 'last'),
            qty_received=('qty_received', 'sum'),
            qty_delivered=('qty_delivered', 'sum'),
            adjustment=('adjustment', 'sum')
        )
    )
    product_summary['total_revenue'] = product_summary['qty_delivered'] * price_per_unit

    daily = (
        ordered.groupby('date', as_index=False)
        .agg(
            delivered=('qty_delivered', 'sum'),
            received=('qty_received', 'sum'),
            adjusted=('adjustment', 'sum')
        )
        .sort_values('date')
    )

    warehouse_summary = (
        ordered.groupby('warehouse', as_index=False)
        .agg(
            qty_received=('qty_received', 'sum'),
            qty_delivered=('qty_delivered', 'sum')
        )
        .sort_values('qty_delivered', ascending=False)
    )

    top_stock = product_summary.sort_values('quantity_stock', ascending=False).head(5).to_dict(orient='records')
    low_stock = product_summary.sort_values('quantity_stock', ascending=True).head(5).to_dict(orient='records')
    top_sales = product_summary.sort_values('qty_delivered', ascending=False).head(5).to_dict(orient='records')

    total_sales = float(product_summary['total_revenue'].sum())
    total_delivered = int(product_summary['qty_delivered'].sum())
    total_received = int(product_summary['qty_received'].sum())
    avg_order_value = total_sales / len(product_summary) if len(product_summary) else 0

    return {
        'total_sales': total_sales,
        'average_order_value': avg_order_value,
        'total_delivered_units': total_delivered,
        'total_received_units': total_received,
        'top_selling_products': top_stock,
        'bottom_selling_products': low_stock,
        'top_sales_products': top_sales,
        'sales_trend_labels': daily['date'].dt.strftime('%d %b').tolist(),
        'sales_trend_delivered': daily['delivered'].tolist(),
        'sales_trend_received': daily['received'].tolist(),
        'warehouse_breakdown': warehouse_summary.to_dict(orient='records'),
    }


def build_transaction_dashboard(df):
    latest = (
        df.sort_values('date')
        .groupby(['product_id', 'product_name'], as_index=False)
        .agg(
            quantity_stock=('stock_after', 'last'),
            qty_delivered=('qty_delivered', 'sum'),
            qty_received=('qty_received', 'sum')
        )
    )
    
    total_products = int(len(latest))
    low_stock_count = int((latest['quantity_stock'] <= 75).sum())
    out_of_stock_count = int((latest['quantity_stock'] <= 0).sum())

    # Advanced Calculations
    price_per_unit = 2500.0
    total_revenue = float(latest['qty_delivered'].sum() * price_per_unit)
    total_delivered = int(latest['qty_delivered'].sum())
    total_received = int(latest['qty_received'].sum())
    
    # AOV calculation (avoid div zero)
    products_sold_count = len(latest[latest['qty_delivered'] > 0])
    aov = total_revenue / products_sold_count if products_sold_count > 0 else 0
    
    # Inventory Health Score (Turnover proxy): (Units Sold / Total Stock Available) * 100
    # Capped at 100 for display purposes
    total_stock = int(latest['quantity_stock'].sum())
    health_score = min(100, round((total_delivered / total_stock) * 100)) if total_stock > 0 else 0

    # Trend calculation (Simulated by splitting data in half chronologically)
    ordered_df = df.sort_values('date')
    if len(ordered_df) > 10:
        midpoint = len(ordered_df) // 2
        first_half = ordered_df.iloc[:midpoint]
        second_half = ordered_df.iloc[midpoint:]
        
        rev_first = first_half['qty_delivered'].sum() * price_per_unit
        rev_second = second_half['qty_delivered'].sum() * price_per_unit
        
        if rev_first > 0:
            growth_pct = ((rev_second - rev_first) / rev_first) * 100
        else:
            growth_pct = 100.0 # From 0 to something
    else:
        growth_pct = 0.0
        
    growth_trend = f"↑ {abs(growth_pct):.1f}%" if growth_pct >= 0 else f"↓ {abs(growth_pct):.1f}%"

    # AI Smart Insights Generation
    top_selling = latest.sort_values('qty_delivered', ascending=False).head(5)
    insights = []
    
    if not top_selling.empty:
        top_product = top_selling.iloc[0]
        if top_product['quantity_stock'] < 100:
             insights.append({
                 'type': 'warning',
                 'icon': 'fa-exclamation-triangle',
                 'text': f"Warning: Your top seller '{top_product['product_name'][:20]}' is running low on stock ({top_product['quantity_stock']} left)!"
             })
             
    if health_score < 20:
         insights.append({
             'type': 'danger',
             'icon': 'fa-box-open',
             'text': f"Alert: Overall inventory health is very low ({health_score}%). You have too much stagnant stock."
         })
    elif health_score > 80:
         insights.append({
             'type': 'success',
             'icon': 'fa-check-circle',
             'text': "Great Job: Inventory turnover is excellent. Products are moving fast."
         })
         
    # Warehouse breakdown
    warehouse_summary = (
        ordered_df.groupby('warehouse', as_index=False)
        .agg(
            qty_delivered=('qty_delivered', 'sum')
        )
        .sort_values('qty_delivered', ascending=False)
    )
    if not warehouse_summary.empty:
        top_wh = warehouse_summary.iloc[0]
        total_deliv_all = warehouse_summary['qty_delivered'].sum()
        pct = (top_wh['qty_delivered'] / total_deliv_all) * 100 if total_deliv_all > 0 else 0
        insights.append({
            'type': 'info',
            'icon': 'fa-map-marker-alt',
            'text': f"Logistics Insight: {top_wh['warehouse']} handled {pct:.0f}% of all deliveries."
        })
        
    top_selling_dicts = top_selling.to_dict(orient='records')
    warehouse_dicts = warehouse_summary.to_dict(orient='records')
    
    daily = (
        ordered_df.groupby('date', as_index=False)
        .agg(
            delivered=('qty_delivered', 'sum'),
            received=('qty_received', 'sum')
        )
        .sort_values('date')
    )

    # EXTRA FIELDS FOR NEW DASHBOARD (From Database)
    from datetime import datetime, date
    from sqlalchemy.orm import joinedload
    today_start = datetime.combine(date.today(), datetime.min.time())
    today_operations = Operation.query.filter(Operation.timestamp >= today_start).count()
    
    all_products_db = Product.query.options(joinedload(Product.stocks)).all()
    all_products_db = [p for p in all_products_db if is_known_product_name(p.name)]
    low_stock_list = []
    for p in all_products_db:
        total_qty = sum(s.quantity for s in p.stocks)
        if total_qty <= p.min_stock_level:
            status = 'OUT' if total_qty == 0 else ('CRITICAL' if total_qty <= p.min_stock_level / 2 else 'LOW')
            low_stock_list.append({
                'name': p.name,
                'sku': p.sku,
                'qty': total_qty,
                'unit': p.unit,
                'status': status
            })

    all_warehouses = Warehouse.query.all()
    warehouse_capacities = []
    max_cap = 2000
    for wh in all_warehouses:
        total_qty = sum(s.quantity for s in wh.stocks)
        pct = min(100, int((total_qty / max_cap) * 100))
        warehouse_capacities.append({'name': wh.name, 'pct': pct})

    recent_operations = Operation.query.order_by(Operation.timestamp.desc()).limit(5).all()

    return {
        'total_products': total_products,
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'pending_receipts': 0,
        'pending_deliveries': 0,
        'internal_transfers': int(df['warehouse'].nunique()),
        'dashboard_source': 'transaction csv',
        # V3 Advanced Fields
        'total_revenue': total_revenue,
        'total_delivered': total_delivered,
        'total_received': total_received,
        'aov': aov,
        'health_score': health_score,
        'growth_trend': growth_trend,
        'growth_is_positive': growth_pct >= 0,
        'smart_insights': insights,
        'top_sellers': top_selling_dicts,
        'warehouse_breakdown': warehouse_dicts,
        'sales_trend_labels': daily['date'].dt.strftime('%d %b').tolist(),
        'sales_trend_delivered': daily['delivered'].tolist(),
        'sales_trend_received': daily['received'].tolist(),
        # Fields for new unified dashboard:
        'today_operations': today_operations,
        'low_stock_list': low_stock_list[:5],
        'warehouse_capacities': warehouse_capacities,
        'recent_operations': recent_operations
    }


def build_summary_dashboard(df):
    total_products = int(len(df))
    low_stock_count = int((df['quantity_stock'] <= df['minimum_stock_level']).sum())
    out_of_stock_count = int((df['quantity_stock'] <= 0).sum())

    return {
        'total_products': total_products,
        'low_stock_count': low_stock_count,
        'out_of_stock_count': out_of_stock_count,
        'pending_receipts': 0,
        'pending_deliveries': 0,
        'internal_transfers': 0,
        'dashboard_source': 'summary csv',
    }

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Authentication Routes ---

@app.route("/signup", methods=['GET', 'POST'])
def signup():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    form = RegistrationForm()
    if form.validate_on_submit():
        user = User(username=form.username.data, email=form.email.data, role=form.role.data)
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        flash('Your account has been created! You are now able to log in', 'success')
        return redirect(url_for('login'))
    return render_template('signup.html', title='Sign Up', form=form)

@app.route("/login", methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            flash(f'Welcome, {user.username}!', 'success')
            from urllib.parse import urlparse
            next_page = request.args.get('next')
            if next_page and (urlparse(next_page).netloc or urlparse(next_page).scheme):
                next_page = None
            return redirect(next_page or url_for('dashboard'))
        else:
            flash('Login Unsuccessful. Please check email and password', 'danger')
    return render_template('login.html', title='Login', form=form)

@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route("/forgot_password", methods=['GET', 'POST'])
def forgot_password():
    form = ForgotPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user:
            otp = ''.join([str(random.randint(0, 9)) for _ in range(6)])
            user.otp = otp
            user.otp_expiry = datetime.utcnow() + timedelta(minutes=10)
            db.session.commit()
            
            # Sending OTP via Email
            try:
                msg = Message('Your IMS 2.0 Password Reset OTP', 
                              sender=app.config['MAIL_USERNAME'],
                              recipients=[user.email])
                msg.body = f"Hello {user.username},\n\nYour OTP for password reset is: {otp}.\nThis code will expire in 10 minutes.\n\nIf you did not request this, please ignore this email."
                mail.send(msg)
                flash(f'An OTP has been sent to {user.email}. Please check your inbox.', 'success')
            except Exception as e:
                print(f"Error sending email: {str(e)}")
                flash('There was an error sending the OTP email. Please try again later.', 'danger')
            
            return redirect(url_for('reset_password'))
        else:
            flash('If that email is registered, an OTP has been sent.', 'info')
    return render_template('forgot_password.html', title='Forgot Password', form=form)

@app.route("/reset_password", methods=['GET', 'POST'])
def reset_password():
    form = ResetPasswordForm()
    if form.validate_on_submit():
        user = User.query.filter_by(otp=form.otp.data).first()
        if user and user.otp_expiry > datetime.utcnow():
            user.set_password(form.password.data)
            user.otp = None
            user.otp_expiry = None
            db.session.commit()
            session.pop('otp_attempts', None)
            flash('Your password has been reset!', 'success')
            return redirect(url_for('login'))
        else:
            session['otp_attempts'] = session.get('otp_attempts', 0) + 1
            if session['otp_attempts'] >= 5:
                flash('Too many failed attempts. Please request a new OTP.', 'danger')
                session.pop('otp_attempts', None)
                return redirect(url_for('forgot_password'))
            flash('Invalid or expired OTP.', 'danger')
    return render_template('reset_password.html', title='Reset Password', form=form)

# --- Dashboard & Core Routes ---

@app.route("/profile", methods=['GET', 'POST'])
@login_required
def profile():
    form = UpdateProfileForm(
        original_username=current_user.username,
        original_email=current_user.email
    )

    if form.validate_on_submit():
        current_user.username = form.username.data
        current_user.email    = form.email.data

        if form.new_password.data:
            current_user.set_password(form.new_password.data)

        db.session.commit()
        flash('Your profile has been updated successfully!', 'success')

        if current_user.role == 'manager':
            return redirect(url_for('dashboard'))
        else:
            return redirect(url_for('staff_panel'))

    elif request.method == 'POST':
        flash('Profile update failed. Please check the errors below.', 'danger')

    elif request.method == 'GET':
        form.username.data = current_user.username
        form.email.data    = current_user.email

    total_operations = Operation.query.filter_by(user_id=current_user.id).count()

    return render_template('profile.html',
                           title='My Profile',
                           form=form,
                           total_operations=total_operations)

@app.route("/health")
def health_check():
    db_uri = app.config['SQLALCHEMY_DATABASE_URI']
    db_type = "PostgreSQL" if db_uri.startswith('postgresql') else "SQLite (Temporary/Local)"
    
    tables = []
    engine_error = None
    try:
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        tables = inspector.get_table_names()
    except Exception as e:
        engine_error = str(e)

    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "database_type": db_type,
        "database_location": db_uri.split('@')[-1], # Mask credentials
        "vercel_environment": bool(os.environ.get('VERCEL')),
        "secret_key_stable": os.environ.get('SECRET_KEY') is not None,
        "database_tables": tables,
        "engine_error": engine_error
    }), 200

@app.route("/")
@app.route("/dashboard")
@login_required
def dashboard():
    if current_user.role == 'staff':
        return redirect(url_for('staff_panel'))

    from datetime import datetime, date as date_type
    
    # Initialize all CSV-based metrics with defaults
    total_revenue          = 0
    health_score           = 0
    growth_trend           = '—'
    growth_is_positive     = True
    csv_products_count     = 0
    internal_transfers_csv = 0
    sales_trend_labels    = []
    sales_trend_delivered = []
    sales_trend_received  = []
    smart_insights        = [] # Global default for dashboard

    # ── Step 1: ALWAYS query live database ──────────────────
    all_products = [p for p in Product.query.all()
                    if is_known_product_name(p.name)]
    db_total_products = len(all_products)

    low_stock_count    = 0
    out_of_stock_count = 0
    low_stock_list     = []
    for p in all_products:
        total_qty = sum(s.quantity for s in p.stocks)
        if total_qty == 0:
            out_of_stock_count += 1
        if total_qty <= p.min_stock_level:
            low_stock_count += 1
            status = 'OUT' if total_qty == 0 else (
                'CRITICAL' if total_qty <= p.min_stock_level / 2
                else 'LOW')
            low_stock_list.append({
                'name': p.name, 'sku': p.sku,
                'qty': total_qty, 'unit': p.unit,
                'status': status
            })

    pending_receipts   = Operation.query.filter_by(
        type='Receipt',  status='Waiting').count()
    pending_deliveries = Operation.query.filter_by(
        type='Delivery', status='Waiting').count()
    db_transfers       = Operation.query.filter_by(
        type='Transfer').count()

    today_start      = datetime.combine(
        date_type.today(), datetime.min.time())
    today_operations = Operation.query.filter(
        Operation.timestamp >= today_start).count()

    all_warehouses       = Warehouse.query.all()
    warehouse_capacities = []
    for wh in all_warehouses:
        total_qty = sum(s.quantity for s in wh.stocks)
        pct       = min(100, int((total_qty / 2000) * 100))
        warehouse_capacities.append({'name': wh.name, 'pct': pct})

    recent_operations = Operation.query.order_by(
        Operation.timestamp.desc()).limit(5).all()

    sales_trend_received  = []

    # Path resolution: Check /tmp first, then repo data_set
    transaction_csv_path = os.path.join(app.config['UPLOAD_FOLDER'], 'inventory_demo_dataset.csv')
    if not os.path.exists(transaction_csv_path):
        transaction_csv_path = os.path.join('data_set', 'inventory_demo_dataset.csv')

    summary_csv_path = os.path.join(app.config['UPLOAD_FOLDER'], 'data.csv')
    if not os.path.exists(summary_csv_path):
        summary_csv_path = os.path.join('data_set', 'data.csv')
    
    transaction_df = load_transaction_dataset(transaction_csv_path)

    if transaction_df is not None:
        csv_data = build_transaction_dashboard(transaction_df)
        total_revenue          = csv_data.get('total_revenue', 0)
        health_score           = csv_data.get('health_score', 0)
        growth_trend           = csv_data.get('growth_trend', '—')
        growth_is_positive     = csv_data.get('growth_is_positive', True)
        csv_products_count     = csv_data.get('total_products', 0)
        internal_transfers_csv = csv_data.get('internal_transfers', 0)
        
        # Extract chart data
        sales_trend_labels    = csv_data.get('sales_trend_labels', [])
        sales_trend_delivered = csv_data.get('sales_trend_delivered', [])
        sales_trend_received  = csv_data.get('sales_trend_received', [])
        smart_insights        = csv_data.get('smart_insights', [])
    else:
        # Fallback to Summary CSV if Transaction CSV is missing
        summary_df = load_inventory_data(summary_csv_path)
        if summary_df is not None:
            metrics = calculate_inventory_metrics(summary_df)
            total_revenue      = metrics.get('total_revenue', 0)
            csv_products_count = metrics.get('total_products', 0)
            health_score       = 100 # Default if unknown
            smart_insights     = ["Summary data loaded. Upload a transaction history CSV for advanced growth metrics."]

    # ── Step 3: Use best value for each KPI ─────────────────
    # Total products: DB if has data, else CSV count
    total_products = db_total_products if db_total_products > 0 \
                     else csv_products_count

    # Transfers: DB operations if any, else CSV warehouse count
    internal_transfers = db_transfers if db_transfers > 0 \
                         else internal_transfers_csv

    return render_template('dashboard.html',
        now                  = datetime.now(),
        current_date         = datetime.now().strftime('%d %b %Y'),
        total_products       = total_products,
        low_stock_count      = low_stock_count,
        out_of_stock_count   = out_of_stock_count,
        pending_receipts     = pending_receipts,
        pending_deliveries   = pending_deliveries,
        internal_transfers   = internal_transfers,
        today_operations     = today_operations,
        total_revenue        = total_revenue,
        health_score         = health_score,
        growth_trend         = growth_trend,
        growth_is_positive   = growth_is_positive,
        sales_trend_labels   = sales_trend_labels,
        sales_trend_delivered = sales_trend_delivered,
        sales_trend_received = sales_trend_received,
        low_stock_list       = low_stock_list[:5],
        warehouse_capacities = warehouse_capacities,
        recent_operations    = recent_operations,
        smart_insights       = csv_data.get('smart_insights', []) if transaction_df is not None else [],
        dashboard_source     = 'live')

@app.route("/staff_panel")
@login_required
def staff_panel():
    if current_user.role != 'staff':
        return redirect(url_for('dashboard'))

    from datetime import datetime, date
    today = date.today()
    today_start = datetime.combine(today, datetime.min.time())
    
    my_operations_today = Operation.query.filter(
        Operation.user_id == current_user.id,
        Operation.timestamp >= today_start
    ).count()

    pending_tasks = Operation.query.filter(
        Operation.user_id == current_user.id,
        Operation.status.in_(['Waiting', 'Draft'])
    ).count()

    from sqlalchemy.orm import joinedload
    all_products = Product.query.options(joinedload(Product.stocks)).all()
    all_products = [p for p in all_products if is_known_product_name(p.name)]
    
    low_stock_count = 0
    low_stock_list = []
    
    for p in all_products:
        total_qty = sum(s.quantity for s in p.stocks)
        if total_qty <= p.min_stock_level:
            low_stock_count += 1
            status = 'OUT' if total_qty == 0 else ('CRITICAL' if total_qty <= p.min_stock_level/2 else 'LOW')
            low_stock_list.append({
                'name': p.name,
                'sku': p.sku,
                'qty': total_qty,
                'unit': p.unit,
                'status': status
            })

    main_wh = Warehouse.query.filter_by(name='Main Warehouse').first()
    if main_wh:
        products_in_warehouse = len(set([s.product_id for s in main_wh.stocks if s.quantity > 0]))
    else:
        products_in_warehouse = db.session.query(Stock.product_id).filter(Stock.quantity > 0).distinct().count()

    recent_operations = Operation.query.filter_by(user_id=current_user.id).order_by(Operation.timestamp.desc()).limit(5).all()
    
    all_warehouses = Warehouse.query.all()
    warehouse_capacities = []
    max_cap = 2000
    for wh in all_warehouses:
        total_qty = sum(s.quantity for s in wh.stocks)
        pct = min(100, int((total_qty / max_cap) * 100))
        warehouse_capacities.append({'name': wh.name, 'pct': pct})

    inventory_list = []
    for p in all_products:
        total_qty = sum(s.quantity for s in p.stocks)
        status = 'OK' if total_qty > p.min_stock_level else ('Out' if total_qty == 0 else ('Critical' if total_qty <= p.min_stock_level/2 else 'Low'))
        inventory_list.append({
            'name': p.name,
            'sku': p.sku,
            'qty': total_qty,
            'unit': p.unit,
            'status': status
        })

    return render_template('staff_panel.html',
                           today_date=datetime.now().strftime('%d %b %Y'),
                           today_time=datetime.now().strftime('%I:%M %p'),
                           my_operations_today=my_operations_today,
                           pending_tasks=pending_tasks,
                           low_stock_count=low_stock_count,
                           products_in_warehouse=products_in_warehouse,
                           low_stock_list=low_stock_list[:4],
                           warehouse_capacities=warehouse_capacities,
                           recent_operations=recent_operations,
                           inventory_list=inventory_list)

# --- Product Management ---

@app.route("/products")
@login_required
def products():
    from sqlalchemy.orm import joinedload
    all_products = Product.query.options(joinedload(Product.stocks).joinedload(Stock.warehouse)).all()
    all_products = [p for p in all_products if is_known_product_name(p.name)]
    # Add calculated total stock to each product object for the template
    for p in all_products:
        p.total_stock = sum(s.quantity for s in p.stocks)
        p.stock_by_warehouse = []
        for s in p.stocks:
            p.stock_by_warehouse.append({
                'warehouse': s.warehouse.name,
                'location': s.warehouse.location,
                'qty': s.quantity,
                'unit': p.unit
            })
    return render_template('products.html', products=all_products)

@app.route("/product/new", methods=['GET', 'POST'])
@login_required
def new_product():
    if current_user.role != 'manager':
        flash('Only managers can add products.', 'danger')
        return redirect(url_for('products'))
    form = ProductForm()
    # Populate warehouse choices
    warehouses = Warehouse.query.all()
    form.warehouse_id.choices = [(0, '-- No initial warehouse --')] + [(w.id, w.name) for w in warehouses]

    if form.validate_on_submit():
        # Check if SKU already exists
        existing = Product.query.filter_by(sku=form.sku.data).first()
        if existing:
            flash(f'A product with SKU "{form.sku.data}" already exists. Please use a unique SKU.', 'danger')
            return render_template('edit_product.html', title='New Product', form=form)
        try:
            product = Product(name=form.name.data, sku=form.sku.data, 
                             category=form.category.data, unit=form.unit.data,
                             min_stock_level=form.min_stock_level.data)
            db.session.add(product)
            db.session.flush() # Get product ID before commit

            # Create initial stock entry if a warehouse was selected
            if form.warehouse_id.data and form.warehouse_id.data != 0:
                initial_stock = Stock(product_id=product.id, warehouse_id=form.warehouse_id.data, quantity=0)
                db.session.add(initial_stock)

            db.session.commit()
            flash('Product created successfully!', 'success')
            return redirect(url_for('products'))
        except Exception as e:
            db.session.rollback()
            flash(f'An error occurred: {str(e)}', 'danger')
    return render_template('edit_product.html', title='New Product', form=form)

@app.route("/product/edit/<int:product_id>", methods=['GET', 'POST'])
@login_required
def edit_product(product_id):
    if current_user.role != 'manager':
        flash('Only managers can edit products.', 'danger')
        return redirect(url_for('products'))
    product = Product.query.get_or_404(product_id)
    form = ProductForm()
    # Populate warehouse choices
    warehouses = Warehouse.query.all()
    form.warehouse_id.choices = [(0, '-- No change --')] + [(w.id, w.name) for w in warehouses]

    if form.validate_on_submit():
        product.name = form.name.data
        product.sku = form.sku.data
        product.category = form.category.data
        product.unit = form.unit.data
        product.min_stock_level = form.min_stock_level.data
        
        # Optionally handle warehouse update if needed, but usually products can be in multiple warehouses.
        # User requested to see existed warehouse when ADDING product. 
        # For editing, we might just show options but usually stock is managed via operations.
        # However, for consistency we populate it.
        
        db.session.commit()
        flash('Product updated successfully!', 'success')
        return redirect(url_for('products'))
    elif request.method == 'GET':
        form.name.data = product.name
        form.sku.data = product.sku
        form.category.data = product.category
        form.unit.data = product.unit
        form.min_stock_level.data = product.min_stock_level
    return render_template('edit_product.html', title='Edit Product', form=form)

# --- Warehouse Management ---

@app.route("/warehouses")
@login_required
def warehouses():
    if current_user.role == 'staff':
        return redirect(url_for('staff_panel'))
    all_warehouses = Warehouse.query.all()
    return render_template('warehouses.html', warehouses=all_warehouses)

@app.route("/warehouse/<int:warehouse_id>")
@login_required
def warehouse_detail(warehouse_id):
    if current_user.role == 'staff':
        return redirect(url_for('staff_panel'))
    warehouse = Warehouse.query.get_or_404(warehouse_id)
    # Get all stocks for this warehouse where quantity is > 0
    # Actually user might want to see all associated products, but >0 is more practical.
    # I'll show all and highlight those with 0.
    return render_template('warehouse_details.html', warehouse=warehouse)

@app.route("/warehouse/new", methods=['GET', 'POST'])
@login_required
def new_warehouse():
    if current_user.role != 'manager':
        flash('Only managers can add warehouses.', 'danger')
        return redirect(url_for('warehouses'))
    form = WarehouseForm()
    if form.validate_on_submit():
        warehouse = Warehouse(name=form.name.data, location=form.location.data)
        db.session.add(warehouse)
        db.session.commit()
        flash('Warehouse created successfully!', 'success')
        return redirect(url_for('warehouses'))
    return render_template('edit_warehouse.html', title='New Warehouse', form=form)

# --- Operations Management ---

@app.route("/operations")
@login_required
def operations():
    all_operations = Operation.query.order_by(Operation.timestamp.desc()).all()
    return render_template('operations.html', operations=all_operations)

@app.route("/operation/new/<op_type>", methods=['GET', 'POST'])
@login_required
def new_operation(op_type):
    warehouses = Warehouse.query.all()
    products = Product.query.all()
    
    if request.method == 'POST':
        try:
            product_id = int(request.form.get('product_id'))
            qty = int(request.form.get('quantity'))
            from_wh_val = request.form.get('from_warehouse_id')
            to_wh_val = request.form.get('to_warehouse_id')
            
            from_wh = int(from_wh_val) if from_wh_val and from_wh_val.isdigit() else None
            to_wh = int(to_wh_val) if to_wh_val and to_wh_val.isdigit() else None
            
            if qty <= 0:
                flash('Quantity must be positive!', 'danger')
                return redirect(url_for('new_operation', op_type=op_type))

            op = Operation(type=op_type, user_id=current_user.id, status='Draft')
            if op_type == 'Receipt':
                supplier_name = request.form.get('supplier_name', '')
                op.supplier_name = supplier_name

            db.session.add(op)
            db.session.flush() 
            
            movement = StockMovement(operation_id=op.id, product_id=product_id, 
                                    quantity=qty, from_warehouse_id=from_wh, to_warehouse_id=to_wh)
            db.session.add(movement)
            
            if op_type == 'Receipt':
                if not to_wh:
                    flash('Destination warehouse is required for receipts!', 'danger')
                    return redirect(url_for('new_operation', op_type=op_type))
                stock = Stock.query.filter_by(product_id=product_id, warehouse_id=to_wh).first()
                if not stock:
                    stock = Stock(product_id=product_id, warehouse_id=to_wh, quantity=0)
                    db.session.add(stock)
                stock.quantity += qty
            elif op_type == 'Delivery':
                if not from_wh:
                    flash('Origin warehouse is required for deliveries!', 'danger')
                    return redirect(url_for('new_operation', op_type=op_type))
                stock = Stock.query.filter_by(product_id=product_id, warehouse_id=from_wh).first()
                if stock and stock.quantity >= qty:
                    stock.quantity -= qty
                else:
                    flash('Insufficient stock!', 'danger')
                    db.session.rollback()
                    return redirect(url_for('operations'))
            elif op_type == 'Transfer':
                if not from_wh or not to_wh:
                    flash('Both warehouses are required for transfers!', 'danger')
                    return redirect(url_for('new_operation', op_type=op_type))
                s_from = Stock.query.filter_by(product_id=product_id, warehouse_id=from_wh).first()
                if s_from and s_from.quantity >= qty:
                    s_from.quantity -= qty
                    s_to = Stock.query.filter_by(product_id=product_id, warehouse_id=to_wh).first()
                    if not s_to:
                        s_to = Stock(product_id=product_id, warehouse_id=to_wh, quantity=0)
                        db.session.add(s_to)
                    s_to.quantity += qty
                else:
                    flash('Insufficient stock!', 'danger')
                    db.session.rollback()
                    return redirect(url_for('operations'))
            elif op_type == 'Adjustment':
                 if not to_wh:
                    flash('Target warehouse is required for adjustments!', 'danger')
                    return redirect(url_for('new_operation', op_type=op_type))
                 stock = Stock.query.filter_by(product_id=product_id, warehouse_id=to_wh).first()
                 if not stock:
                     stock = Stock(product_id=product_id, warehouse_id=to_wh, quantity=0)
                     db.session.add(stock)
                 stock.quantity = qty 
                 
            db.session.commit()
            flash(f'{op_type} operation completed!', 'success')
            return redirect(url_for('operations'))
        except Exception as e:
            db.session.rollback()
            app.logger.error(f'Operation error: {str(e)}')
            flash('An error occurred while processing the operation. Please try again.', 'danger')
            return redirect(url_for('operations'))
        
    return render_template('new_operation.html', type=op_type, warehouses=warehouses, products=products)

@app.route("/operation/<int:op_id>/validate", methods=['POST'])
@login_required
def validate_operation(op_id):
    op = Operation.query.get_or_404(op_id)
    status_flow = {'Draft': 'Waiting', 'Waiting': 'Ready', 'Ready': 'Done'}
    if op.status in status_flow:
        op.status = status_flow[op.status]
        db.session.commit()
        if op.status == 'Done':
            flash('Operation validated and completed!', 'success')
        else:
            flash(f'Operation moved to {op.status}.', 'info')
    return redirect(url_for('operations'))

# --- Analytics & Predictions ---

@app.route("/analytics/upload", methods=['POST'])
@login_required
def upload_analytics_csv():
    if 'file' not in request.files:
        flash('Please choose a CSV or Excel file to upload.', 'danger')
        return redirect(url_for('analytics'))

    file = request.files['file']
    if not file or file.filename == '':
        flash('Please choose a CSV or Excel file to upload.', 'danger')
        return redirect(url_for('analytics'))

    allowed_exts = {'.csv', '.xlsx', '.xls'}
    _, ext = os.path.splitext(file.filename or '')
    ext = ext.lower()
    if ext not in allowed_exts:
        flash('Only CSV / Excel files are supported.', 'danger')
        return redirect(url_for('analytics'))

    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
    transaction_path = os.path.join(app.config['UPLOAD_FOLDER'], 'inventory_demo_dataset.csv')
    summary_path = os.path.join(app.config['UPLOAD_FOLDER'], 'data.csv')
    temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f'_upload_temp{ext}')

    try:
        file.save(temp_path)

        # Try to load the uploaded file in a way that supports both CSV and Excel.
        if ext in ['.xlsx', '.xls']:
            df = pd.read_excel(temp_path)
        else:
            df = pd.read_csv(temp_path)

        # Combine checks for target path
        active_paths = {transaction_path, summary_path}
        
        # Determine if the uploaded file is a transaction file or summary
        if REQUIRED_ANALYTICS_COLUMNS.issubset(df.columns):
            target_path = transaction_path
            uploaded_source = 'transaction csv'
        else:
            is_valid, message = validate_csv_data(df)
            if is_valid:
                target_path = summary_path
                uploaded_source = 'summary csv'
            else:
                raise ValueError(message)

        # Save to target and remove the OTHER one to prevent conflict
        df.to_csv(target_path, index=False)
        for old_path in active_paths:
            if old_path != target_path and os.path.exists(old_path):
                os.remove(old_path)

        flash(f'File uploaded successfully. Active source: {uploaded_source}.', 'success')
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        flash(f'Error uploading file: {str(e)}', 'danger')

    return redirect(url_for('analytics'))

@app.route("/analytics")
@login_required
def analytics():
    if current_user.role == 'staff':
        return redirect(url_for('staff_panel'))
    return render_template('analytics.html')

# --- Initialization ---

def init_db():
    with app.app_context():
        # Ensure upload folder exists
        os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
        
        db.create_all()
        
        # Manual fix for PostgreSQL password_hash length
        from sqlalchemy import text
        try:
            db.session.execute(text('ALTER TABLE "user" ALTER COLUMN password_hash TYPE VARCHAR(256)'))
            db.session.commit()
        except Exception as e:
            # Table might not exist yet or column already altered
            db.session.rollback()

        # Seed products if empty
        if not Product.query.first():
            demo_prods = [
                {'name': 'Industrial Motor XL', 'sku': 'IND-MOT-001', 'category': 'Machinery', 'unit': 'unit', 'min': 5},
                {'name': 'Copper Wire 100m', 'sku': 'ELE-WIR-052', 'category': 'Electrical', 'unit': 'roll', 'min': 20},
                {'name': 'Steel Bolts M8', 'sku': 'FAS-BLT-008', 'category': 'Fasteners', 'unit': 'box', 'min': 50},
                {'name': 'Hydraulic Oil 20L', 'sku': 'LUB-OIL-020', 'category': 'Lubricants', 'unit': 'can', 'min': 10},
                {'name': 'Safety Helmet', 'sku': 'PPE-HLM-001', 'category': 'Safety', 'unit': 'unit', 'min': 15},
                {'name': 'Welding Rods E6013', 'sku': 'WLD-ROD-613', 'category': 'Welding', 'unit': 'kg', 'min': 30},
                {'name': 'Bearing 6204-2RS', 'sku': 'MCH-BRG-204', 'category': 'Mechanical', 'unit': 'unit', 'min': 40},
                {'name': 'LED Panel 40W', 'sku': 'LGT-PAN-040', 'category': 'Lighting', 'unit': 'unit', 'min': 12},
            ]
            for p_data in demo_prods:
                p = Product(name=p_data['name'], sku=p_data['sku'], category=p_data['category'], unit=p_data['unit'], min_stock_level=p_data['min'])
                db.session.add(p)
            db.session.commit()

        # Add default warehouses if none exist
        if not Warehouse.query.first():
            w1 = Warehouse(name='Main Warehouse', location='North Zone')
            w2 = Warehouse(name='Production Floor', location='Central Zone')
            w3 = Warehouse(name='Store A', location='South Zone')
            db.session.add_all([w1, w2, w3])
            db.session.commit()

# Initialize database on app startup
try:
    print(f"Starting database initialization on {app.config['SQLALCHEMY_DATABASE_URI'].split('@')[-1]}")
    init_db()
    print("Database initialization successful.")
except Exception as e:
    import traceback
    print(f"Database initialization failed: {str(e)}")
    print(traceback.format_exc())

if __name__ == '__main__':
    import os as _os
    # Use 'stat' reloader instead of 'watchdog' to prevent the reloader from
    # monitoring site-packages (SQLAlchemy etc.), which causes ERR_CONNECTION_RESET
    # on Windows when watchdog detects filesystem access as "changes".
    app.run(debug=True, host='0.0.0.0', port=5000,
            use_reloader=True,
            reloader_type='stat',
            extra_files=[
                _os.path.join(_os.path.dirname(__file__), 'templates'),
                _os.path.join(_os.path.dirname(__file__), 'static'),
            ])
