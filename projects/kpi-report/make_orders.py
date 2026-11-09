"""Deterministic synthetic order data (6 weeks) with one revenue spike, for demos and tests."""
import csv, random, sys
from datetime import date, timedelta

rng = random.Random(42)
PRODUCTS = {"Starter Kit": 29.0, "Pro Kit": 79.0, "Refill Pack": 12.5, "Gift Card": 50.0, "Accessory Set": 19.9}
start = date(2026, 9, 28)  # a Monday
out = sys.argv[1] if len(sys.argv) > 1 else "samples/orders.csv"
with open(out, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["order_id", "date", "product", "qty", "revenue", "status"])
    n = 0
    for d in range(42):
        day = start + timedelta(days=d)
        count = rng.randint(18, 26) + (40 if day == date(2026, 11, 4) else 0)  # promo spike
        for _ in range(count):
            n += 1
            p = rng.choice(list(PRODUCTS))
            q = rng.choice([1, 1, 1, 2, 3])
            w.writerow([f"o{n}", day.isoformat(), p, q, round(PRODUCTS[p] * q, 2), "refunded" if rng.random() < 0.04 else "paid"])
print(f"{n} orders -> {out}")
