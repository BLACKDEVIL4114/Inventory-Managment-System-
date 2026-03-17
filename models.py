from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256))
    role = db.Column(db.String(20), default='staff')  # 'manager' or 'staff'
    otp = db.Column(db.String(6), nullable=True)
    otp_expiry = db.Column(db.DateTime, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Warehouse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    location = db.Column(db.String(200))
    stocks = db.relationship('Stock', backref='warehouse', lazy=True)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    sku = db.Column(db.String(50), unique=True, nullable=False)
    category = db.Column(db.String(50))
    unit = db.Column(db.String(20))  # kg, piece, liter, etc.
    unit_price = db.Column(db.Float, default=0.0)
    cost_price = db.Column(db.Float, default=0.0)
    min_stock_level = db.Column(db.Integer, default=10)
    stocks = db.relationship('Stock', backref='product', lazy=True)

class Stock(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=False)
    quantity = db.Column(db.Integer, default=0)

    __table_args__ = (db.UniqueConstraint('product_id', 'warehouse_id', name='_product_warehouse_uc'),)

class Operation(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50), nullable=False)  # 'Receipt', 'Delivery', 'Transfer', 'Adjustment'
    status = db.Column(db.String(20), default='Draft')  # 'Draft', 'Waiting', 'Ready', 'Done', 'Canceled'
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    supplier_name = db.Column(db.String(100), nullable=True)
    
    # Relationships
    movements = db.relationship('StockMovement', backref='operation', lazy=True)
    user = db.relationship('User', backref='operations', lazy=True)

class StockMovement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    operation_id = db.Column(db.Integer, db.ForeignKey('operation.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    from_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True)
    to_warehouse_id = db.Column(db.Integer, db.ForeignKey('warehouse.id'), nullable=True)
    quantity = db.Column(db.Integer, nullable=False)
    unit_price = db.Column(db.Float, default=0.0)
    total_price = db.Column(db.Float, default=0.0)

    # Add relationships for convenience
    product = db.relationship('Product', backref='movements', lazy=True)
    from_warehouse = db.relationship('Warehouse', foreign_keys=[from_warehouse_id], backref='outgoing_movements', lazy=True)
    to_warehouse = db.relationship('Warehouse', foreign_keys=[to_warehouse_id], backref='incoming_movements', lazy=True)
