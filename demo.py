"""Create fictional data in demo_data/ and optionally launch its isolated app."""
import argparse
import datetime
import os
import sqlite3
from pathlib import Path

import main as app

DEMO_USERNAME = "demo"
DEMO_PASSWORD = "BudgetlyDemo2026!"
DEMO_PIN = "7391"
ROOT = Path(__file__).resolve().parent
DEMO_DB = ROOT / "demo_data" / "budgetly_demo.db"


def create_demo():
    DEMO_DB.parent.mkdir(exist_ok=True)
    # Exclusive reservation: never replace a database, even if this is run twice.
    descriptor = os.open(DEMO_DB, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    app.DB_FILE = str(DEMO_DB)
    try:
        app.DatabaseManager.init_db()
        conn = app.DatabaseManager.get_connection()
        try:
            with conn:
                cursor = conn.execute(
                    "INSERT INTO users(username,password_hash,pin_hash) VALUES(?,?,?)",
                    (DEMO_USERNAME, app.hash_secret(DEMO_PASSWORD), app.hash_secret(DEMO_PIN)),
                )
                user_id = cursor.lastrowid
        finally:
            conn.close()
        for name, kind, balance in [("Demo Cash", "Cash", "12500.00"),
                                    ("Demo Bank", "Bank Account", "40000.00"),
                                    ("Demo bKash", "Mobile Wallet", "1500.00")]:
            app.create_account(user_id, name, kind, balance)
        accounts = {name: account_id for account_id, name in app.user_accounts(user_id)}
        today = datetime.date.today()
        def date(days_ago):
            return (today - datetime.timedelta(days=days_ago)).isoformat()
        app.save_transaction(user_id, accounts["Demo Bank"], "Sample salary", "25000.00",
                             today.replace(day=1).isoformat(), "Salary", "Income")
        samples = [
            ("Groceries", "950.50", "Food & Dining", "Demo Cash", 1),
            ("Lunch", "250.00", "Food & Dining", "Demo Cash", 2),
            ("Internet bill", "1200.00", "Bills & Utilities", "Demo Bank", 3),
            ("Books", "780.75", "Shopping", "Demo bKash", 4),
            ("Dinner", "425.25", "Food & Dining", "Demo Cash", 5),
            ("Desk supplies", "320.00", "Shopping", "Demo Cash", 6),
        ]
        for title, amount, category, account, days in samples:
            app.save_transaction(user_id, accounts[account], title, amount, date(days), category, "Expense")
        app.transfer_money(user_id, accounts["Demo Bank"], accounts["Demo Cash"], "3000.00", date(1), "Sample cash withdrawal")
        app.transfer_money(user_id, accounts["Demo Bank"], accounts["Demo bKash"], "1000.00", date(0), "Sample wallet top-up")
        for category, limit in [("Food & Dining", "5000.00"), ("Bills & Utilities", "10000.00"), ("Shopping", "4000.00")]:
            app.set_budget(user_id, category, limit)
        app.create_recurring_rule(user_id, accounts["Demo Bank"], "Sample monthly rent", "6000.00", 1, "Expense", "Bills & Utilities")
        app.DatabaseManager.process_recurring_transactions(user_id)
    except Exception:
        # The reserved file belongs to this attempt. Do not leave a partial demo.
        DEMO_DB.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true", help="Open the isolated demo after creating/reusing it")
    args = parser.parse_args()
    app.DB_FILE = str(DEMO_DB)
    try:
        create_demo()
        print("Created fictional demo data.")
    except FileExistsError:
        print("Demo already exists; it was not overwritten.")
    print(f"Demo database: {DEMO_DB}")
    print(f"Demo-only login: {DEMO_USERNAME}")
    print(f"Demo-only password: {DEMO_PASSWORD}")
    print(f"Demo-only recovery PIN: {DEMO_PIN}")
    print("These are public demonstration credentials. Never use them for personal data.")
    if args.launch:
        app.DatabaseManager.init_db()
        window = app.MainApplication()
        window.title("Budgetly 2.3.0 - FICTIONAL DEMO")
        window.mainloop()


if __name__ == "__main__":
    main()
