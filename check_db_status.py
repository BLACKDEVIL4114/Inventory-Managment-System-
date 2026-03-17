from app import app, db, Product, Stock, Operation, StockMovement
import os

def check_db():
    with app.app_context():
        print("--- Database Check ---")
        products = Product.query.all()
        print(f"Total Products: {len(products)}")
        for p in products[:10]:
            total_stock = sum(s.quantity for s in p.stocks)
            print(f"  - {p.name} (SKU: {p.sku}, Cat: {p.category}): Stock={total_stock}, Price={p.unit_price}, Cost={p.cost_price}")
        
        ops = Operation.query.count()
        print(f"Total Operations: {ops}")
        
        movements = StockMovement.query.count()
        print(f"Total Stock Movements: {movements}")
        
        if ops > 0:
            latest_op = Operation.query.order_by(Operation.timestamp.desc()).first()
            print(f"Latest Operation: {latest_op.type} at {latest_op.timestamp}")

if __name__ == "__main__":
    check_db()
