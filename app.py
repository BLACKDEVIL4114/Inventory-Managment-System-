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
import re
from sqlalchemy import func, text
from models import db, User, Product, Warehouse, Stock, Operation, StockMovement
from forms import LoginForm, RegistrationForm, ProductForm, WarehouseForm, ForgotPasswordForm, ResetPasswordForm, UpdateProfileForm
from utils import validate_csv_data

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
# Database Configuration
db_uri = os.environ.get('DATABASE_URL')
if db_uri and db_uri.startswith("postgres://"):
    db_uri = db_uri.replace("postgres://", "postgresql://", 1)

if not db_uri:
    # Local development fallback
    db_path = os.path.join(os.getcwd(), 'inventory.db')
    db_uri = f'sqlite:///{db_path}'
    if os.environ.get('VERCEL'):
        # Vercel temporary fallback (non-persistent)
        db_uri = 'sqlite:////tmp/inventory.db'

app.config['SQLALCHEMY_DATABASE_URI'] = db_uri
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = '/tmp' if os.environ.get('VERCEL') else 'data_set'
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

import os as _os_flask
@app.context_processor
def inject_now():
    return {
        'now': datetime.utcnow(),
        'os': _os_flask
    }

# Flexible requirements: Only date and product_name are strictly required for analytics.
# Others will be filled with defaults if missing.
REQUIRED_ANALYTICS_COLUMNS = {'date', 'product_name'}
OPTIONAL_ANALYTICS_COLUMNS = ['product_id', 'warehouse', 'qty_received', 'qty_delivered', 'adjustment', 'stock_after']


def is_known_product_name(name):
    value = (name or '').strip().lower()
    return bool(value) and 'unknown' not in value


def sync_csv_to_db(csv_path):
    """Parse CSV and sync with DB models. Returns (bool, message)"""
    if not os.path.exists(csv_path):
        return False, "File does not exist"

    try:
        # Load data
        if csv_path.endswith(('.xlsx', '.xls')):
            df = pd.read_excel(csv_path)
        else:
            df = pd.read_csv(csv_path)
    except Exception as e:
        return False, f"Failed to read file: {str(e)}"

    if df.empty:
        return False, "File is empty"

    # ── Robust Column Detection ──
    col_map = {}
    norm_cols = {c.strip().lower().replace(' ', '_'): c for c in df.columns}
    
    mapping_rules = {
        'product_name': ['product_name', 'name', 'product', 'item_name', 'item', 'p_name', 'productname', 'title'],
        'sku': ['sku', 'product_id', 'id', 'item_code', 'code', 'part_number', 'model_no', 'sku_no'],
        'quantity_stock': ['quantity_stock', 'stock', 'quantity', 'qty', 'on_hand', 'quantity_on_hand', 'current_stock', 'inventory_level', 'count'],
        'min_stock': ['minimum_stock_level', 'min_stock', 'min_level', 'alert_level', 'safety_stock', 'reorder_point'],
        'unit_price': ['total_revenue', 'total_sales', 'revenue', 'price', 'unit_price', 'product_price', 'sale_price', 'mrp', 'rate'],
        'cost_price': ['cost', 'cost_price', 'purchase_price', 'unit_cost', 'buying_price', 'wholesale_price'],
        'category': ['category', 'group', 'type', 'class', 'department', 'family', 'tags'],
        'warehouse': ['warehouse', 'loc', 'location', 'store', 'bin', 'rack', 'branch', 'hub'],
        'date': ['date', 'timestamp', 'time', 'day', 'created_at', 'last_updated', 'transaction_date'],
        'qty_received': ['qty_received', 'received', 'incoming', 'in', 'purchased', 'receipt_qty'],
        'qty_delivered': ['qty_delivered', 'delivered', 'outgoing', 'out', 'sold', 'sale_qty', 'delivery_qty']
    }
    
    # Fill col_map with found column names
    for target, aliases in mapping_rules.items():
        for alias in aliases:
            if alias in norm_cols:
                col_map[target] = norm_cols[alias]
                break
    
    # Simple helper to get mapped value
    def get_val(row, key, default=None):
        col = col_map.get(key)
        if col and col in row:
            val = row[col]
            if pd.isna(val): return default
            return val
        return default

    # Identify if it's a Transaction vs Summary file
    # Transaction files must have a date and either incoming or outgoing movement
    is_transaction = 'date' in col_map and \
                     any(x in norm_cols for x in ['qty_received', 'qty_delivered', 'received', 'delivered', 'in', 'out'])
    
    try:
        # 1. Ensure User exists for operations
        creator_id = current_user.id if (current_user and not current_user.is_anonymous) else 1
        
        # 2. Map Warehouses
        wh_map = {} # name -> id
        
        # 3. Process Rows
        for _, row in df.iterrows():
            pname = str(get_val(row, 'product_name', '')).strip()
            if not pname or pname.lower() == 'nan': continue
            
            sku = str(get_val(row, 'sku', '')).strip()
            if not sku or sku.lower() == 'nan': 
                sku = f"PROD-{pname[:3].upper()}-{random.randint(100,999)}"
            
            # Find or Create Product
            product = Product.query.filter((Product.name == pname) | (Product.sku == sku)).first()
            if not product:
                product = Product(name=pname, sku=sku)
                db.session.add(product)
            
            # Update Product Metadata
            cat = get_val(row, 'category')
            if cat: product.category = str(cat)
            
            price = get_val(row, 'unit_price')
            if price is not None:
                try: 
                    # Use re.sub for robust currency removal
                    p_str = re.sub(r'[₹$, ]', '', str(price))
                    p_val = float(p_str)
                    product.unit_price = p_val
                except: pass
                
            cost = get_val(row, 'cost_price')
            if cost is not None:
                try: 
                    c_str = re.sub(r'[₹$, ]', '', str(cost))
                    product.cost_price = float(c_str)
                except: pass

            mins = get_val(row, 'min_stock')
            if mins is not None:
                try: product.min_stock_level = int(mins)
                except: pass
            
            db.session.flush() # Get product.id

            # 4. Handle Warehouse
            wh_name = str(get_val(row, 'warehouse', 'Main Warehouse')).strip()
            if wh_name not in wh_map:
                wh = Warehouse.query.filter_by(name=wh_name).first()
                if not wh:
                    wh = Warehouse(name=wh_name, location="Imported Zone")
                    db.session.add(wh)
                    db.session.flush()
                wh_map[wh_name] = wh.id
            
            wh_id = wh_map[wh_name]

            # 5. Stock Level (Snapshot)
            q_stock = get_val(row, 'quantity_stock')
            if q_stock is not None:
                try:
                    # Coerce to numeric
                    qty_str = re.sub(r'[^0-9.]', '', str(q_stock))
                    qty = int(float(qty_str))
                    stock = Stock.query.filter_by(product_id=product.id, warehouse_id=wh_id).first()
                    if not stock:
                        stock = Stock(product_id=product.id, warehouse_id=wh_id)
                        db.session.add(stock)
                    
                    diff = qty - (stock.quantity or 0)
                    stock.quantity = qty
                    
                    # Synthesize Transaction for Summary Files (if it contributes to stock increase)
                    if not is_transaction and diff != 0:
                        s_type = 'Receipt' if diff > 0 else 'Adjustment'
                        op = Operation(type=s_type, status='Done', timestamp=datetime.utcnow(), user_id=creator_id, supplier_name="System Sync")
                        db.session.add(op); db.session.flush()
                        db.session.add(StockMovement(
                            operation_id=op.id, product_id=product.id, 
                            to_warehouse_id=wh_id if diff > 0 else None,
                            from_warehouse_id=wh_id if diff < 0 else None,
                            quantity=abs(diff),
                            unit_price=product.cost_price if diff > 0 else product.unit_price,
                            total_price=abs(diff) * (product.cost_price if diff > 0 else product.unit_price)
                        ))
                except: pass

            # 6. Movement (Operation)
            if is_transaction:
                m_date_raw = get_val(row, 'date')
                m_date = pd.to_datetime(m_date_raw, errors='coerce')
                if pd.isna(m_date): m_date = datetime.utcnow()
                
                # Use helper for movement amounts
                def get_qty(keys):
                    for k in keys:
                        v = get_val(row, k)
                        if v is not None:
                            try:
                                v_str = re.sub(r'[^0-9.]', '', str(v))
                                return int(float(v_str))
                            except: pass
                    return 0

                recv = get_qty(['qty_received', 'received', 'incoming', 'in'])
                deliv = get_qty(['qty_delivered', 'delivered', 'outgoing', 'out'])
                
                if recv > 0:
                    op = Operation(type='Receipt', status='Done', timestamp=m_date, user_id=creator_id, supplier_name="CSV Import")
                    db.session.add(op); db.session.flush()
                    db.session.add(StockMovement(operation_id=op.id, product_id=product.id, to_warehouse_id=wh_id, quantity=recv,
                                                unit_price=product.cost_price, total_price=recv*product.cost_price))
                    
                if deliv > 0:
                    op = Operation(type='Delivery', status='Done', timestamp=m_date, user_id=creator_id)
                    db.session.add(op); db.session.flush()
                    db.session.add(StockMovement(operation_id=op.id, product_id=product.id, from_warehouse_id=wh_id, quantity=deliv,
                                                unit_price=product.unit_price, total_price=deliv*product.unit_price))

        db.session.commit()
        p_count = Product.query.count()
        return True, f"Successfully synchronized {p_count} products to the persistent database. Your data is now safe."
    except Exception as e:
        db.session.rollback()
        return False, f"Sync error: {str(e)}"


def build_transaction_dashboard(df):
    if df is None or len(df) == 0:
        return {
            'total_revenue': 0, 'health_score': 0, 'growth_trend': '—',
            'growth_is_positive': True, 'total_products': 0,
            'internal_transfers': 0, 'sales_trend_labels': [],
            'sales_trend_delivered': [], 'sales_trend_received': [],
            'smart_insights': ["No valid transaction data found in file."]
        }
    
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
@app.route("/home")
@app.route("/dashboard")
@login_required
def dashboard():
    if current_user.role == 'staff':
        return redirect(url_for('staff_panel'))

    # 1. Basic Stats from DB
    all_products = Product.query.all()
    total_products = len(all_products)
    
    # 2. Stock Levels & Alerts
    low_stock_count = 0
    out_of_stock_count = 0
    low_stock_list = []
    
    for p in all_products:
        total_qty = sum(s.quantity for s in p.stocks)
        if total_qty == 0:
            out_of_stock_count += 1
        if total_qty <= p.min_stock_level:
            low_stock_count += 1
            status = 'OUT' if total_qty == 0 else ('CRITICAL' if total_qty <= p.min_stock_level/2 else 'LOW')
            low_stock_list.append({'name': p.name, 'sku': p.sku, 'qty': total_qty, 'unit': p.unit, 'status': status})

    # 3. Revenue & Growth
    today = datetime.utcnow()
    this_month_start = datetime(today.year, today.month, 1)
    # Correct handling for month wrap-back
    if today.month == 1:
        last_month_start = datetime(today.year - 1, 12, 1)
    else:
        last_month_start = datetime(today.year, today.month - 1, 1)

    this_month_rev = db.session.query(func.sum(StockMovement.total_price))\
        .join(Operation).filter(Operation.type == 'Delivery', Operation.timestamp >= this_month_start).scalar() or 0
    
    last_month_rev = db.session.query(func.sum(StockMovement.total_price))\
        .join(Operation).filter(Operation.type == 'Delivery', Operation.timestamp >= last_month_start, Operation.timestamp < this_month_start).scalar() or 0
    
    total_revenue = db.session.query(func.sum(StockMovement.total_price))\
        .join(Operation).filter(Operation.type == 'Delivery').scalar() or 0

    growth_pct = 0
    if last_month_rev > 0:
        growth_pct = ((this_month_rev - last_month_rev) / last_month_rev) * 100
    
    growth_trend = f"{growth_pct:+.1f}%"
    growth_is_positive = growth_pct >= 0

    # 4. Weekly Sales Trend
    sales_trend_labels = []
    sales_trend_delivered = []
    sales_trend_received = []
    
    for i in range(6, -1, -1):
        d = (today - timedelta(days=i)).date()
        sales_trend_labels.append(d.strftime('%b %d'))
        
        d_start = datetime.combine(d, datetime.min.time())
        d_end = datetime.combine(d, datetime.max.time())
        
        delivered = db.session.query(func.sum(StockMovement.quantity))\
            .join(Operation).filter(Operation.type == 'Delivery', Operation.timestamp >= d_start, Operation.timestamp <= d_end).scalar() or 0
        received = db.session.query(func.sum(StockMovement.quantity))\
            .join(Operation).filter(Operation.type == 'Receipt', Operation.timestamp >= d_start, Operation.timestamp <= d_end).scalar() or 0
            
        sales_trend_delivered.append(int(delivered))
        sales_trend_received.append(int(received))

    # 5. Other KPIs
    pending_receipts = Operation.query.filter_by(type='Receipt', status='Waiting').count()
    pending_deliveries = Operation.query.filter_by(type='Delivery', status='Waiting').count()
    internal_transfers = Operation.query.filter_by(type='Transfer').count()
    today_operations = Operation.query.filter(Operation.timestamp >= datetime.combine(today.date(), datetime.min.time())).count()

    # 6. Smart Insights
    smart_insights = []
    if low_stock_count > 0:
        smart_insights.append({"type": "warning", "icon": "fa-exclamation-triangle", "text": f"Warning: {low_stock_count} products are below minimum stock level."})
    
    top_selling = db.session.query(Product.name, func.sum(StockMovement.quantity).label('total'))\
        .join(StockMovement).join(Operation).filter(Operation.type == 'Delivery')\
        .group_by(Product.id).order_by(text('total DESC')).first()
        
    if top_selling:
        smart_insights.append({"type": "success", "icon": "fa-chart-line", "text": f"Bestseller: {top_selling[0]} is your top performing product."})

    # 7. Warehouse Capacities
    warehouse_capacities = []
    for wh in Warehouse.query.all():
        total_qty = sum(s.quantity for s in wh.stocks)
        pct = min(100, int((total_qty / 2000) * 100))
        warehouse_capacities.append({'name': wh.name, 'pct': pct})

    recent_operations = Operation.query.order_by(Operation.timestamp.desc()).limit(5).all()

    return render_template('dashboard.html',
        now=datetime.now(),
        current_date=datetime.now().strftime('%d %b %Y'),
        total_products=total_products,
        low_stock_count=low_stock_count,
        out_of_stock_count=out_of_stock_count,
        pending_receipts=pending_receipts,
        pending_deliveries=pending_deliveries,
        internal_transfers=internal_transfers,
        today_operations=today_operations,
        total_revenue=total_revenue,
        health_score=round(100 - min(100, (low_stock_count/total_products*100)), 1) if total_products > 0 else 100,
        growth_trend=growth_trend,
        growth_is_positive=growth_is_positive,
        sales_trend_labels=sales_trend_labels,
        sales_trend_delivered=sales_trend_delivered,
        sales_trend_received=sales_trend_received,
        smart_insights=smart_insights,
        warehouse_capacities=warehouse_capacities,
        recent_operations=recent_operations,
        low_stock_list=low_stock_list
    )

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

    temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f'_upload_temp{ext}')
    try:
        file.save(temp_path)
        
        if ext in ['.xlsx', '.xls']:
            success, msg = sync_csv_to_db(temp_path)
        else:
            success, msg = sync_csv_to_db(temp_path)
            
        if success:
            db.session.commit() # Extra insurance
            flash(msg, 'success')
        else:
            flash(msg, 'danger')
            
    except Exception as e:
        flash(f'Error uploading file: {str(e)}', 'danger')
    finally:
        if os.path.exists(temp_path): os.remove(temp_path)

    return redirect(url_for('analytics'))

@app.route("/analytics")
@login_required
def analytics():
    if current_user.role == 'staff':
        return redirect(url_for('staff_panel'))
    return render_template('analytics.html')

# --- Analytics API Endpoints ---

@app.route("/api/analytics/kpis")
@login_required
def api_kpis():
    all_products = Product.query.all()
    total_products = len(all_products)
    low_stock = sum(1 for p in all_products if sum(s.quantity for s in p.stocks) <= p.min_stock_level)
    
    total_revenue = db.session.query(func.sum(StockMovement.total_price))\
        .join(Operation).filter(Operation.type == 'Delivery').scalar() or 0
    
    avg_order = db.session.query(func.avg(StockMovement.total_price))\
        .join(Operation).filter(Operation.type == 'Delivery').scalar() or 0

    return jsonify({
        "total_revenue": float(total_revenue),
        "total_delivered": int(db.session.query(func.sum(StockMovement.quantity)).join(Operation).filter(Operation.type == 'Delivery').scalar() or 0),
        "total_received": int(db.session.query(func.sum(StockMovement.quantity)).join(Operation).filter(Operation.type == 'Receipt').scalar() or 0),
        "avg_order_value": float(avg_order),
        "low_stock_count": low_stock
    })

@app.route("/api/analytics/revenue")
@login_required
def api_revenue():
    # Last 30 days revenue trend
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=30)
    
    results = db.session.query(
        func.date(Operation.timestamp).label('date'),
        func.sum(StockMovement.total_price).label('revenue')
    ).join(StockMovement).filter(
        Operation.type == 'Delivery',
        Operation.timestamp >= start_date
    ).group_by(func.date(Operation.timestamp)).order_by('date').all()
    
    return jsonify([{"date": str(r.date), "revenue": float(r.revenue)} for r in results])

@app.route("/api/analytics/inventory")
@login_required
def api_inventory():
    delivered = db.session.query(func.sum(StockMovement.quantity)).join(Operation).filter(Operation.type == 'Delivery').scalar() or 0
    received = db.session.query(func.sum(StockMovement.quantity)).join(Operation).filter(Operation.type == 'Receipt').scalar() or 0
    adjusted = db.session.query(func.sum(StockMovement.quantity)).join(Operation).filter(Operation.type == 'Adjustment').scalar() or 0
    
    return jsonify({
        "delivered": int(delivered),
        "received": int(received),
        "adjusted": int(adjusted)
    })

@app.route("/api/analytics/products")
@login_required
def api_products():
    top_sales = db.session.query(
        Product.name, func.sum(StockMovement.quantity).label('total')
    ).join(StockMovement).join(Operation).filter(Operation.type == 'Delivery')\
    .group_by(Product.id).order_by(text('total DESC')).limit(5).all()
    
    top_stock = []
    for p in Product.query.all():
        qty = sum(s.quantity for s in p.stocks)
        top_stock.append({"product_name": p.name, "quantity_stock": qty})
    top_stock.sort(key=lambda x: x['quantity_stock'], reverse=True)
    
    return jsonify({
        "top_sales": [{"product_name": r.name, "qty_delivered": int(r.total)} for r in top_sales],
        "top_stock": top_stock[:5]
    })

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
        except:
            db.session.rollback()

        # Add missing columns for all models (SQLite/Postgres)
        cols_to_add = [
            ('product', 'unit_price', 'FLOAT DEFAULT 0.0'),
            ('product', 'cost_price', 'FLOAT DEFAULT 0.0'),
            ('product', 'category', 'VARCHAR(50)'),
            ('product', 'unit', 'VARCHAR(20)'),
            ('operation', 'supplier_name', 'VARCHAR(100)'),
            ('stock_movement', 'unit_price', 'FLOAT DEFAULT 0.0'),
            ('stock_movement', 'total_price', 'FLOAT DEFAULT 0.0'),
            ('user', 'otp', 'VARCHAR(6)'),
            ('user', 'otp_expiry', 'TIMESTAMP'),
        ]
        for table, col, col_type in cols_to_add:
            try:
                # Quote table name for 'user' which is reserved in Postgres
                t_name = f'"{table}"' if table == 'user' else table
                db.session.execute(text(f'ALTER TABLE {t_name} ADD COLUMN {col} {col_type}'))
                db.session.commit()
            except:
                db.session.rollback()

        # Seed products if empty
        if not Product.query.first():
            demo_prods = [
                {'name': 'Industrial Motor XL', 'sku': 'IND-MOT-001', 'category': 'Machinery', 'unit': 'unit', 'min': 5, 'price': 15000, 'cost': 12000},
                {'name': 'Copper Wire 100m', 'sku': 'ELE-WIR-052', 'category': 'Electrical', 'unit': 'roll', 'min': 20, 'price': 2500, 'cost': 1800},
                {'name': 'Steel Bolts M8', 'sku': 'FAS-BLT-008', 'category': 'Fasteners', 'unit': 'box', 'min': 50, 'price': 1200, 'cost': 800},
                {'name': 'Hydraulic Oil 20L', 'sku': 'LUB-OIL-020', 'category': 'Lubricants', 'unit': 'can', 'min': 10, 'price': 4500, 'cost': 3200},
                {'name': 'Safety Helmet', 'sku': 'PPE-HLM-001', 'category': 'Safety', 'unit': 'unit', 'min': 15, 'price': 850, 'cost': 500},
                {'name': 'Welding Rods E6013', 'sku': 'WLD-ROD-613', 'category': 'Welding', 'unit': 'kg', 'min': 30, 'price': 1500, 'cost': 1100},
                {'name': 'Bearing 6204-2RS', 'sku': 'MCH-BRG-204', 'category': 'Mechanical', 'unit': 'unit', 'min': 40, 'price': 600, 'cost': 400},
                {'name': 'LED Panel 40W', 'sku': 'LGT-PAN-040', 'category': 'Lighting', 'unit': 'unit', 'min': 12, 'price': 1800, 'cost': 1200},
            ]
            for p_data in demo_prods:
                p = Product(name=p_data['name'], sku=p_data['sku'], category=p_data['category'], 
                            unit=p_data['unit'], min_stock_level=p_data['min'],
                            unit_price=p_data['price'], cost_price=p_data['cost'])
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
