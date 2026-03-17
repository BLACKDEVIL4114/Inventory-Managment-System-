from app import app, db, Product, StockMovement
import random

def seed_prices():
    with app.app_context():
        print("--- Seeding Prices ---")
        products = Product.query.all()
        for p in products:
            if p.unit_price == 0:
                p.unit_price = random.randint(500, 5000)
                p.cost_price = p.unit_price * random.uniform(0.6, 0.8)
                print(f"  - Set price for {p.name}: {p.unit_price}")
        
        db.session.commit()
        
        print("--- Updating Stock Movement Totals ---")
        movements = StockMovement.query.all()
        for m in movements:
            if m.unit_price == 0:
                m.unit_price = m.product.unit_price
                m.total_price = m.unit_price * m.quantity
        
        db.session.commit()
        print("Successfully seeded financial data.")

if __name__ == "__main__":
    seed_prices()
