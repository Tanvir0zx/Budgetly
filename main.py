import csv
import datetime
import hashlib
import os
import hmac
import sqlite3
import time
import re
from decimal import Decimal, InvalidOperation
import tkinter as tk
from tkinter import filedialog, messagebox
import customtkinter as ctk

# --- MATPLOTLIB INTEGRATION ---
import matplotlib

matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import matplotlib.pyplot as plt

# --- REPORTLAB INTEGRATION ---
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
from xml.sax.saxutils import escape

# --- GLOBAL CONFIGURATION ---
ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")
DB_FILE = os.path.abspath("budgetly_master.db")
APP_VERSION = "2.3.0"


def legacy_hash_str(value):
    """Old SHA-256 hashing kept only for existing accounts."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_secret(value):
    """Securely hash a password or security PIN using PBKDF2."""
    salt = os.urandom(16)

    iterations = 600_000

    derived_key = hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        salt,
        iterations
    )

    return f"pbkdf2_sha256${iterations}${salt.hex()}${derived_key.hex()}"


def verify_secret(value, stored_hash):
    """Accept legacy SHA-256 and validated PBKDF2 records."""
    if not isinstance(value, str) or not isinstance(stored_hash, str):
        return False
    if stored_hash.startswith("pbkdf2_sha256$"):
        try:
            algorithm, rounds, salt_hex, key_hex = stored_hash.split("$")
            rounds = int(rounds)
            if not 100_000 <= rounds <= 2_000_000:
                return False
            salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(key_hex)
            if len(salt) != 16 or len(expected) != 32:
                return False
            actual = hashlib.pbkdf2_hmac("sha256", value.encode("utf-8"), salt, rounds)
            return hmac.compare_digest(actual, expected)
        except (ValueError, TypeError, OverflowError):
            return False
    if len(stored_hash) != 64 or any(c not in "0123456789abcdef" for c in stored_hash):
        return False
    return hmac.compare_digest(legacy_hash_str(value), stored_hash)


def decimal_to_minor(value):
    scaled = value * 100
    if not scaled.is_finite() or scaled != scaled.to_integral_value():
        raise ValueError("Money must have at most two decimal places.")
    return int(scaled)


def minor_to_decimal(raw):
    return Decimal(int(raw)) / Decimal(100)


sqlite3.register_adapter(Decimal, decimal_to_minor)
sqlite3.register_converter("MONEY_CENTS", minor_to_decimal)


# --- DATABASE MANAGER ---
class DatabaseManager:

    @staticmethod
    def get_connection():
        connection = sqlite3.connect(DB_FILE, timeout=15, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def init_db():
        backup_before_upgrade()
        conn = DatabaseManager.get_connection()
        try:
            # Rebuild legacy money tables atomically. Foreign keys are checked
            # before commit and enabled again for every normal connection.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.cursor()

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    pin_hash TEXT NOT NULL,
                    currency TEXT DEFAULT '৳',
                    theme TEXT DEFAULT 'Dark'
                )
            """
            )

            columns = {row[1] for row in cursor.execute("PRAGMA table_info(users)")}
            for column, definition in [("currency", "currency TEXT DEFAULT '৳'"),
                                       ("theme", "theme TEXT DEFAULT 'Dark'")]:
                if column not in columns:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {definition}")

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL,
                    balance REAL DEFAULT 0.0,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    type TEXT NOT NULL,
                    amount REAL NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                )
            """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS budgets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    amount_limit REAL NOT NULL,
                    UNIQUE(user_id, category),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS recurring_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    account_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    type TEXT NOT NULL,
                    amount REAL NOT NULL,
                    day_of_month INTEGER NOT NULL,
                    last_processed TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(account_id) REFERENCES accounts(id)
                )
            """
            )
            cursor.execute("""CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                from_account_id INTEGER NOT NULL,
                to_account_id INTEGER NOT NULL,
                amount REAL NOT NULL CHECK(amount > 0),
                date TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                CHECK(from_account_id != to_account_id),
                FOREIGN KEY(user_id) REFERENCES users(id),
                FOREIGN KEY(from_account_id) REFERENCES accounts(id),
                FOREIGN KEY(to_account_id) REFERENCES accounts(id)
            )""")
            validate_existing_data(conn)
            cursor.execute("""CREATE TABLE IF NOT EXISTS recurring_occurrences (
                rule_id INTEGER NOT NULL REFERENCES recurring_rules(id) ON DELETE CASCADE,
                period TEXT NOT NULL,
                transaction_id INTEGER REFERENCES transactions(id) ON DELETE SET NULL,
                PRIMARY KEY(rule_id, period)
            )""")
            # Preserve legacy processing markers without guessing which transaction
            # belongs to a rule. Deleting a generated record must not regenerate it.
            cursor.execute("""INSERT OR IGNORE INTO recurring_occurrences(rule_id, period)
                SELECT id, substr(last_processed,1,7) FROM recurring_rules
                WHERE last_processed IS NOT NULL AND length(last_processed)>=7""")
            migrate_exact_money(conn)
            validate_existing_data(conn)
            install_money_guards(conn)
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Migration failed its relationship check; original data retained.")
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


    @staticmethod
    def process_recurring_transactions(user_id):
        today = datetime.date.today()
        period = today.strftime("%Y-%m")
        conn = DatabaseManager.get_connection()
        try:
            with conn:
                # Lock before reading markers so two running apps cannot claim
                # the same monthly occurrence.
                conn.execute("BEGIN IMMEDIATE")
                rules = conn.execute("""SELECT id,account_id,title,category,type,amount,
                    day_of_month,last_processed FROM recurring_rules WHERE user_id=?""",(user_id,)).fetchall()
                for rule_id, account_id, title, category, kind, raw, day, last in rules:
                    if today.day < day or (last and last.startswith(period)):
                        continue
                    if conn.execute("SELECT 1 FROM recurring_occurrences WHERE rule_id=? AND period=?",(rule_id,period)).fetchone():
                        continue
                    amount = money_input(raw)
                    if kind not in ("Income", "Expense"):
                        raise ValueError(f"Recurring rule #{rule_id} has an invalid transaction type.")
                    require_account(conn,user_id,account_id)
                    transaction = conn.execute("""INSERT INTO transactions
                        (user_id,account_id,date,title,category,type,amount) VALUES(?,?,?,?,?,?,?)""",
                        (user_id,account_id,today.isoformat(),f"[Auto] {title}",category,kind,amount))
                    conn.execute("INSERT INTO recurring_occurrences VALUES(?,?,?)",(rule_id,period,transaction.lastrowid))
                    conn.execute("UPDATE accounts SET balance=balance+? WHERE id=? AND user_id=?",
                                 (amount if kind=="Income" else -amount,account_id,user_id))
                    conn.execute("UPDATE recurring_rules SET last_processed=? WHERE id=? AND user_id=?",
                                 (today.isoformat(),rule_id,user_id))
        finally:
            conn.close()

    @staticmethod
    def check_budget_alerts(user_id, category):
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT amount_limit FROM budgets WHERE user_id = ? AND category = ?",
            (user_id, category),
        )
        b_row = cursor.fetchone()

        if not b_row or b_row[0] <= 0:
            conn.close()
            return

        limit = b_row[0]
        spent = monthly_spending(conn, user_id, category)
        conn.close()

        pct = (spent / limit) * 100
        if pct >= 100:
            messagebox.showwarning(
                "🚨 Budget Exceeded!",
                f"Alert: You have reached {pct:.1f}% of your budget for '{category}'!\nSpent: {spent:,.2f} / Limit: {limit:,.2f}",
            )
        elif pct >= 80:
            messagebox.showinfo(
                "⚠️ Budget Alert (80%)",
                f"Warning: You have used {pct:.1f}% of your budget for '{category}'.\nSpent: {spent:,.2f} / Limit: {limit:,.2f}",
            )


def money_input(value, allow_zero=False, allow_negative=False):
    try:
        amount = Decimal(str(value).strip())
        if not amount.is_finite() or abs(amount) > Decimal("999999999999.99"):
            raise ValueError("Enter a finite amount below 1 trillion.")
        if (amount == 0 and not allow_zero) or (amount < 0 and not allow_negative):
            raise ValueError("Enter a positive amount.")
        if amount != amount.quantize(Decimal("0.01")):
            raise ValueError("Use no more than two decimal places.")
        return amount.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError):
        raise ValueError("Enter a valid amount.") from None


def backup_before_upgrade():
    if not os.path.isfile(DB_FILE) or os.path.getsize(DB_FILE) == 0:
        return
    source = sqlite3.connect(DB_FILE)
    try:
        if source.execute("PRAGMA user_version").fetchone()[0] >= 230:
            return
        # SQLite backup API safely captures a consistent existing database.
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        destination = sqlite3.connect(f"{DB_FILE}.before_2_3_0_{stamp}.bak")
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()


def migrate_exact_money(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > 230:
        raise ValueError("This database was created by a newer Budgetly version.")
    if version == 230:
        return
    money_tables = {"accounts":"balance", "transactions":"amount", "budgets":"amount_limit",
                    "recurring_rules":"amount", "transfers":"amount"}
    for table, column in money_tables.items():
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()[0]
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
        money_index = columns.index(column)
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        converted, before_total, after_total = [], Decimal(0), 0
        for row in rows:
            original = Decimal(str(row[money_index]))
            rounded = original.quantize(Decimal("0.01"))
            # Accept tiny binary-float residue only; meaningful fractional cents
            # require explicit repair rather than an undocumented rounding choice.
            if abs(original-rounded) > Decimal("0.0000001"):
                raise ValueError(f"{table} row #{row[0]} has fractional poisha/cents; migration cancelled. Original database retained.")
            cents = int(rounded * 100)
            values = list(row)
            values[money_index] = cents
            converted.append(tuple(values))
            before_total += original
            after_total += cents
        if before_total.quantize(Decimal("0.01")) != Decimal(after_total)/100:
            raise ValueError(f"{table} total would change after rounding; migration cancelled.")
        # Preserve IDs, unique constraints, foreign keys, and AUTOINCREMENT high-water marks.
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?",(table,)).fetchone()
        extras = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL",(table,)).fetchall()
        temporary = f"_money_upgrade_{table}"
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",(temporary,)).fetchone():
            raise ValueError("An unexpected migration table exists. Original database retained.")
        new_schema = re.sub(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?[\"`\[]?"+table+r"[\"`\]]?", f'CREATE TABLE "{temporary}"', schema, count=1, flags=re.I)
        new_schema, count = re.subn(r"\b"+column+r"\s+REAL\b", column+" MONEY_CENTS INTEGER", new_schema, count=1, flags=re.I)
        if count != 1:
            raise ValueError(f"Unrecognized legacy schema for {table}; migration cancelled.")
        new_schema = new_schema.replace("DEFAULT 0.0", "DEFAULT 0")
        conn.execute(new_schema)
        if converted:
            placeholders = ",".join("?" for _ in columns)
            conn.executemany(f'INSERT INTO "{temporary}" VALUES({placeholders})',converted)
        # SQL CAST avoids automatic Decimal conversion during raw-cent verification.
        actual = conn.execute(f'SELECT id,CAST({column} AS INTEGER) FROM "{temporary}" ORDER BY id').fetchall()
        if actual != [(row[0],row[money_index]) for row in converted]:
            raise ValueError(f"Verification failed for {table}; migration cancelled.")
        conn.execute(f'DROP TABLE "{table}"')
        conn.execute(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
        if sequence:
            if conn.execute("SELECT 1 FROM sqlite_sequence WHERE name=?",(table,)).fetchone():
                conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?",(sequence[0],table))
            else:
                conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)",(table,sequence[0]))
        for kind, name, sql in extras:
            if not name.startswith("guard_"):
                conn.execute(sql)
    conn.execute("PRAGMA user_version = 230")


def validate_existing_data(conn):
    issues = []
    for table, column, positive in [("accounts","balance",False),("transactions","amount",True),
                                    ("budgets","amount_limit",True),("recurring_rules","amount",True),
                                    ("transfers","amount",True)]:
        exact = any(row[1] == column and row[2].startswith("MONEY_CENTS")
                    for row in conn.execute(f"PRAGMA table_info({table})"))
        condition = (f"{column} IS NULL OR typeof({column}) != 'integer' OR abs({column}) > 99999999999999"
                     if exact else f"{column} IS NULL OR typeof({column}) NOT IN ('real','integer') OR abs({column}) > 999999999999.99")
        if positive:
            condition += f" OR {column} <= 0"
        bad = conn.execute(f"SELECT id FROM {table} WHERE {condition} LIMIT 5").fetchall()
        if bad:
            issues.append(f"{table}: invalid money in row(s) " + ", ".join(str(r[0]) for r in bad))
    if conn.execute("PRAGMA foreign_key_check").fetchone():
        issues.append("Records reference a missing user or account")
    for table in ("transactions", "recurring_rules"):
        if conn.execute(f"SELECT 1 FROM {table} t JOIN accounts a ON a.id=t.account_id WHERE t.user_id!=a.user_id LIMIT 1").fetchone():
            issues.append(f"{table}: account ownership mismatch")
    if issues:
        raise ValueError("Existing database needs repair before continuing: " + "; ".join(issues) +
                         ". Existing balances were left unchanged. Keep your database and any before_2_3_0 backup for repair.")


def install_money_guards(conn):
    for table, column, positive in [("accounts","balance",False),("transactions","amount",True),
                                    ("budgets","amount_limit",True),("recurring_rules","amount",True),
                                    ("transfers","amount",True)]:
        condition = f"NEW.{column} IS NULL OR typeof(NEW.{column}) != 'integer' OR abs(NEW.{column}) > 99999999999999"
        if positive:
            condition += f" OR NEW.{column} <= 0"
        for operation in ("INSERT", "UPDATE"):
            conn.execute(f"DROP TRIGGER IF EXISTS guard_{table}_{operation.lower()}")
            conn.execute(f"""CREATE TRIGGER IF NOT EXISTS guard_{table}_{operation.lower()}
                BEFORE {operation} ON {table} WHEN {condition}
                BEGIN SELECT RAISE(ABORT, 'Invalid or out-of-range money value'); END""")


def require_account(conn, user_id, account_id):
    row = conn.execute("SELECT balance FROM accounts WHERE id=? AND user_id=?",(account_id,user_id)).fetchone()
    if row is None:
        raise ValueError("Select one of your accounts.")
    return row[0]


def delete_transaction_record(user_id, transaction_id):
    conn = DatabaseManager.get_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT account_id,type,amount FROM transactions WHERE id=? AND user_id=?",(transaction_id,user_id)).fetchone()
            if row is None:
                return False
            account_id, kind, amount = row
            require_account(conn,user_id,account_id)
            conn.execute("UPDATE accounts SET balance=balance+? WHERE id=? AND user_id=?",
                         (-amount if kind=="Income" else amount,account_id,user_id))
            conn.execute("DELETE FROM transactions WHERE id=? AND user_id=?",(transaction_id,user_id))
            return True
    finally:
        conn.close()


def create_account(user_id, name, kind, opening):
    amount = money_input(opening or "0", allow_zero=True, allow_negative=True)
    if not name.strip():
        raise ValueError("Enter an account name.")
    conn = DatabaseManager.get_connection()
    try:
        with conn:
            conn.execute("INSERT INTO accounts(user_id,name,type,balance) VALUES(?,?,?,?)",(user_id,name.strip(),kind,amount))
    finally:
        conn.close()


def set_budget(user_id, category, raw):
    amount = money_input(raw)
    conn = DatabaseManager.get_connection()
    try:
        with conn:
            conn.execute("""INSERT INTO budgets(user_id,category,amount_limit) VALUES(?,?,?)
                ON CONFLICT(user_id,category) DO UPDATE SET amount_limit=excluded.amount_limit""",(user_id,category,amount))
    finally:
        conn.close()


def create_recurring_rule(user_id, account_id, title, raw, day, kind, category):
    amount = money_input(raw)
    try:
        day = int(day)
    except (ValueError, TypeError):
        raise ValueError("Due day must be a whole number from 1 to 28.") from None
    if not 1 <= day <= 28 or not title.strip() or kind not in ("Income","Expense"):
        raise ValueError("Enter a title, a day from 1 to 28, and Income or Expense.")
    conn = DatabaseManager.get_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            require_account(conn,user_id,account_id)
            conn.execute("""INSERT INTO recurring_rules(user_id,account_id,title,category,type,amount,day_of_month)
                VALUES(?,?,?,?,?,?,?)""",(user_id,account_id,title.strip(),category,kind,amount,day))
    finally:
        conn.close()


def monthly_spending(conn, user_id, category, month=None):
    month = month or datetime.date.today().strftime("%Y-%m")
    return conn.execute("""SELECT COALESCE(SUM(amount),0) AS "total [MONEY_CENTS]" FROM transactions
        WHERE user_id=? AND category=? AND type='Expense' AND date LIKE ?""",
        (user_id,category,month+"-%")).fetchone()[0]


def valid_date(value):
    try:
        parsed = datetime.date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return value
    except ValueError:
        raise ValueError("Enter a valid date as YYYY-MM-DD.") from None


def user_accounts(user_id):
    conn = DatabaseManager.get_connection()
    try:
        return conn.execute("SELECT id, name FROM accounts WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    finally:
        conn.close()


def account_picker(parent, user_id, selected=None):
    choices = {f"{name} (#{aid})": aid for aid, name in user_accounts(user_id)}
    menu = ctk.CTkOptionMenu(parent, values=list(choices) or ["Create an account first"])
    for label, aid in choices.items():
        if aid == selected:
            menu.set(label)
    return menu, choices


def save_transaction(user_id, account_id, title, amount, date, category, kind, transaction_id=None):
    amount = money_input(amount)
    date = valid_date(date)
    if not title.strip() or kind not in ("Income", "Expense"):
        raise ValueError("Enter a description and select Income or Expense.")
    conn = DatabaseManager.get_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT id FROM accounts WHERE id=? AND user_id=?", (account_id,user_id)).fetchone():
                raise ValueError("Select one of your accounts.")
            if transaction_id is not None:
                old = conn.execute("SELECT account_id, type, amount FROM transactions WHERE id=? AND user_id=?", (transaction_id,user_id)).fetchone()
                if not old:
                    raise ValueError("Transaction no longer exists.")
                conn.execute("UPDATE accounts SET balance=balance+? WHERE id=? AND user_id=?", (-old[2] if old[1]=='Income' else old[2],old[0],user_id))
                conn.execute("UPDATE transactions SET account_id=?,title=?,amount=?,date=?,category=?,type=? WHERE id=? AND user_id=?", (account_id,title.strip(),amount,date,category,kind,transaction_id,user_id))
            else:
                conn.execute("INSERT INTO transactions(user_id,account_id,title,amount,date,category,type) VALUES(?,?,?,?,?,?,?)",(user_id,account_id,title.strip(),amount,date,category,kind))
            conn.execute("UPDATE accounts SET balance=balance+? WHERE id=? AND user_id=?",(amount if kind=='Income' else -amount,account_id,user_id))
    finally:
        conn.close()


def transfer_money(user_id, source, destination, amount, date, note=""):
    amount = money_input(amount)
    date = valid_date(date)
    if source is None or destination is None or source == destination:
        raise ValueError("Select two different accounts.")
    conn = DatabaseManager.get_connection()
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            owned = dict(conn.execute("SELECT id,balance FROM accounts WHERE user_id=? AND id IN (?,?)",(user_id,source,destination)))
            if len(owned) != 2:
                raise ValueError("Both accounts must belong to you.")
            if Decimal(str(owned[source])) < Decimal(str(amount)):
                raise ValueError("The source account has insufficient funds.")
            conn.execute("UPDATE accounts SET balance=balance-? WHERE id=? AND user_id=?",(amount,source,user_id))
            conn.execute("UPDATE accounts SET balance=balance+? WHERE id=? AND user_id=?",(amount,destination,user_id))
            conn.execute("INSERT INTO transfers(user_id,from_account_id,to_account_id,amount,date,note) VALUES(?,?,?,?,?,?)",(user_id,source,destination,amount,date,note.strip()))
    finally:
        conn.close()


def export_transactions_pdf(user_id, path):
    conn = DatabaseManager.get_connection()
    try:
        rows = conn.execute("""SELECT t.date,t.title,t.category,t.type,t.amount,a.name
            FROM transactions t JOIN accounts a ON a.id=t.account_id
            WHERE t.user_id=? ORDER BY t.date DESC,t.id DESC""",(user_id,)).fetchall()
    finally:
        conn.close()
    styles = getSampleStyleSheet()
    style = styles["BodyText"]
    style.fontSize = 8
    style.leading = 10
    data = [["Date","Description","Category","Type","Amount","Account"]]
    for date,title,category,kind,amount,account in rows:
        data.append([Paragraph(escape(str(value)),style) for value in
                     (date,title,category,kind,f"{amount:,.2f}",account)])
    table = Table(data,colWidths=[62,135,85,50,70,90],repeatRows=1,splitByRow=1,splitInRow=1)
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#DBEAFE")),
                              ("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),
                              ("VALIGN",(0,0),(-1,-1),"TOP"),
                              ("BOTTOMPADDING",(0,0),(-1,-1),6),
                              ("GRID",(0,0),(-1,-1),.25,colors.lightgrey)]))
    doc = SimpleDocTemplate(path,pagesize=letter,leftMargin=30,rightMargin=30,topMargin=36,bottomMargin=36)
    def footer(pdf, document):
        pdf.setFont("Helvetica",8)
        pdf.drawRightString(582,18,f"Page {document.page}")
    doc.build([Paragraph("Budgetly — Transaction Report",styles["Title"]),
               Paragraph(f"All {len(rows)} income/expense transactions. Transfers are listed separately in Accounts.",styles["BodyText"]),
               Spacer(1,12),table],onFirstPage=footer,onLaterPages=footer)
    return len(rows)


class TransferModal(ctk.CTkToplevel):
    def __init__(self, parent, user_id, on_save):
        super().__init__(parent)
        self.user_id, self.on_save = user_id, on_save
        self.title("Transfer between accounts")
        self.geometry("440x530")
        self.transient(parent.winfo_toplevel())
        self.grab_set()
        ctk.CTkLabel(self,text="Transfer Money",font=ctk.CTkFont(size=20,weight="bold")).pack(pady=12)
        ctk.CTkLabel(self,text="From account").pack()
        self.source, self.choices = account_picker(self,user_id)
        self.source.pack(fill="x",padx=24,pady=4)
        ctk.CTkLabel(self,text="To account").pack()
        self.destination, _ = account_picker(self,user_id)
        if len(self.choices)>1:
            self.destination.set(list(self.choices)[1])
        self.destination.pack(fill="x",padx=24,pady=4)
        self.amount = ctk.CTkEntry(self,placeholder_text="Amount")
        self.amount.pack(fill="x",padx=24,pady=8)
        self.date = ctk.CTkEntry(self,placeholder_text="YYYY-MM-DD")
        self.date.insert(0,datetime.date.today().isoformat())
        self.date.pack(fill="x",padx=24,pady=8)
        self.note = ctk.CTkEntry(self,placeholder_text="Note (optional)")
        self.note.pack(fill="x",padx=24,pady=8)
        self.error = ctk.CTkLabel(self,text="",wraplength=370,text_color="#EF4444")
        self.error.pack(pady=8)
        ctk.CTkLabel(self,text="Transfers do not count as income or expenses.").pack()
        ctk.CTkButton(self,text="Transfer",command=self.save).pack(pady=12)

    def save(self):
        try:
            transfer_money(self.user_id,self.choices.get(self.source.get()),self.choices.get(self.destination.get()),self.amount.get(),self.date.get(),self.note.get())
        except (ValueError, sqlite3.Error) as error:
            self.error.configure(text=str(error))
            return
        self.on_save()
        self.destroy()


# --- MODAL: EDIT TRANSACTION WINDOW ---
class EditTransactionModal(ctk.CTkToplevel):

    def __init__(self, parent, user_id, tx_data, on_save_callback):
        super().__init__(parent)
        self.user_id = user_id
        self.tx_id, self.date, self.tx_title, self.category, self.t_type, self.amount, self.acc_id = (
            tx_data
        )
        self.on_save_callback = on_save_callback

        self.title("Edit Transaction")
        self.geometry("400x550")
        self.resizable(False, False)
        self.grab_set()

        lbl = ctk.CTkLabel(
            self,
            text="✏️ Edit Transaction",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        lbl.pack(pady=(25, 15))

        self.ent_title = ctk.CTkEntry(self, width=320, height=40, corner_radius=10)
        self.ent_title.insert(0, self.tx_title)
        self.ent_title.pack(pady=6)

        self.ent_amt = ctk.CTkEntry(self, width=320, height=40, corner_radius=10)
        self.ent_amt.insert(0, str(self.amount))
        self.ent_amt.pack(pady=6)

        self.ent_date = ctk.CTkEntry(self, width=320, height=40, corner_radius=10)
        self.ent_date.insert(0, self.date)
        self.ent_date.pack(pady=6)

        self.opt_type = ctk.CTkOptionMenu(
            self, values=["Expense", "Income"], width=320, height=40, corner_radius=10
        )
        self.opt_type.set(self.t_type)
        self.opt_type.pack(pady=6)

        self.opt_cat = ctk.CTkOptionMenu(
            self,
            values=[
                "Food & Dining",
                "Bills & Utilities",
                "Shopping",
                "Salary",
                "Other",
            ],
            width=320,
            height=40,
            corner_radius=10,
        )
        self.opt_cat.set(self.category)
        self.opt_cat.pack(pady=6)

        ctk.CTkLabel(self,text="Account").pack()
        self.opt_account, self.account_choices = account_picker(self,self.user_id,self.acc_id)
        self.opt_account.pack(fill="x",padx=40,pady=6)
        btn_save = ctk.CTkButton(
            self,
            text="Save Changes",
            fg_color="#10B981",
            hover_color="#059669",
            width=320,
            height=42,
            corner_radius=10,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.save_edits,
        )
        btn_save.pack(pady=20)

    def save_edits(self):
        new_title = self.ent_title.get().strip()
        new_date = self.ent_date.get().strip()
        new_type = self.opt_type.get()
        new_cat = self.opt_cat.get()

        try:
            save_transaction(self.user_id,self.account_choices.get(self.opt_account.get()),new_title,
                             self.ent_amt.get(),new_date,new_cat,new_type,self.tx_id)
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror("Could not save transaction", str(error))
            return

        if new_type == "Expense":
            DatabaseManager.check_budget_alerts(self.user_id, new_cat)

        self.on_save_callback()
        self.destroy()


# --- MODERN SPLIT-CARD AUTHENTICATION FRAME ---
class AuthFrame(ctk.CTkFrame):

    def __init__(self, parent, on_login_success):
        super().__init__(parent, fg_color="transparent")
        self.on_login_success = on_login_success

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.card = ctk.CTkFrame(
            self,
            width=880,
            height=540,
            corner_radius=24,
            fg_color=("gray95", "#181825"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        self.card.grid(row=0, column=0, padx=20, pady=20)
        self.card.grid_propagate(False)
        self.card.grid_columnconfigure(0, weight=1)
        self.card.grid_columnconfigure(1, weight=1)
        self.card.grid_rowconfigure(0, weight=1)

        self.build_left_hero()
        self.build_right_form()

    def build_left_hero(self):
        hero = ctk.CTkFrame(
            self.card,
            corner_radius=20,
            fg_color=("#2563EB", "#1E293B"),
            border_width=0,
        )
        hero.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

        content = ctk.CTkFrame(hero, fg_color="transparent")
        content.place(relx=0.5, rely=0.5, anchor="center")

        badge = ctk.CTkLabel(
            content,
            text=f"💎 BUDGETLY PRO v{APP_VERSION}",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color="#93C5FD",
        )
        badge.pack(pady=(0, 10))

        title = ctk.CTkLabel(
            content,
            text="Master Your\nMoney Today",
            font=ctk.CTkFont(size=28, weight="bold"),
            text_color="white",
            justify="center",
        )
        title.pack(pady=(0, 15))

        subtitle = ctk.CTkLabel(
            content,
            text="Smart expense tracking, automated\nrecurring bills, and visual analytics.",
            font=ctk.CTkFont(size=12),
            text_color="#CBD5E1",
            justify="center",
        )
        subtitle.pack(pady=(0, 25))

        highlights = [
            "⚡ Instant Expense Logging",
            "🎯 Category Budget Limits",
            "📈 Visual Financial Charts",
        ]
        for h in highlights:
            pill = ctk.CTkLabel(
                content,
                text=h,
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color="white",
                fg_color=("#3B82F6", "#334155"),
                corner_radius=12,
                padx=14,
                pady=5,
            )
            pill.pack(pady=4)

    def build_right_form(self):
        self.form_panel = ctk.CTkFrame(self.card, fg_color="transparent")
        self.form_panel.grid(row=0, column=1, sticky="nsew", padx=30, pady=25)

        self.mode_switch = ctk.CTkSegmentedButton(
            self.form_panel,
            values=["Login", "Register", "Reset Password"],
            command=self.switch_mode,
            selected_color="#2563EB",
            selected_hover_color="#1D4ED8",
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.mode_switch.pack(fill="x", pady=(10, 20))
        self.mode_switch.set("Login")

        self.form_container = ctk.CTkFrame(
            self.form_panel, fg_color="transparent"
        )
        self.form_container.pack(fill="both", expand=True)

        self.render_login_fields()

    def switch_mode(self, mode):
        self.winfo_toplevel().focus_set()
        for w in self.form_container.winfo_children():
            w.destroy()

        if mode == "Login":
            self.render_login_fields()
        elif mode == "Register":
            self.render_register_fields()
        elif mode == "Reset Password":
            self.render_reset_fields()

    def render_login_fields(self):
        lbl_welcome = ctk.CTkLabel(
            self.form_container,
            text="Welcome back! 👋",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        lbl_welcome.pack(anchor="w", pady=(5, 15))

        self.login_user = ctk.CTkEntry(
            self.form_container,
            placeholder_text="👤 Username",
            height=42,
            corner_radius=10,
        )
        self.login_user.pack(fill="x", pady=8)

        self.login_pass = ctk.CTkEntry(
            self.form_container,
            placeholder_text="🔒 Password",
            show="*",
            height=42,
            corner_radius=10,
        )
        self.login_pass.pack(fill="x", pady=8)

        self.show_login_password = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.form_container, text="Show password",
            variable=self.show_login_password,
            command=lambda: self.login_pass.configure(
                show="" if self.show_login_password.get() else "*"
            ),
        ).pack(anchor="w", pady=4)

        self.lbl_login_err = ctk.CTkLabel(
            self.form_container, text="", text_color="#EF4444", font=ctk.CTkFont(size=12)
        )
        self.lbl_login_err.pack(pady=4)

        btn_login = ctk.CTkButton(
            self.form_container,
            text="Sign In",
            height=42,
            corner_radius=10,
            fg_color="#2563EB",
            hover_color="#1D4ED8",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.handle_login,
        )
        btn_login.pack(fill="x", pady=(10, 0))

    def render_register_fields(self):
        lbl_title = ctk.CTkLabel(
            self.form_container,
            text="Create Profile ✨",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        lbl_title.pack(anchor="w", pady=(5, 12))

        self.reg_user = ctk.CTkEntry(
            self.form_container,
            placeholder_text="👤 Choose Username",
            height=40,
            corner_radius=10,
        )
        self.reg_user.pack(fill="x", pady=6)

        self.reg_pass = ctk.CTkEntry(
            self.form_container,
            placeholder_text="🔒 Create Password",
            show="*",
            height=40,
            corner_radius=10,
        )
        self.reg_pass.pack(fill="x", pady=6)

        self.reg_pin = ctk.CTkEntry(
            self.form_container,
            placeholder_text="🔑 Security PIN (4 digits)",
            show="*",
            height=40,
            corner_radius=10,
        )
        self.reg_pin.pack(fill="x", pady=6)

        self.lbl_reg_err = ctk.CTkLabel(
            self.form_container, text="", text_color="#EF4444", font=ctk.CTkFont(size=12)
        )
        self.lbl_reg_err.pack(pady=2)

        btn_reg = ctk.CTkButton(
            self.form_container,
            text="Create Account",
            height=42,
            corner_radius=10,
            fg_color="#10B981",
            hover_color="#059669",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.handle_register,
        )
        btn_reg.pack(fill="x", pady=(8, 0))

    def render_reset_fields(self):
        lbl_title = ctk.CTkLabel(
            self.form_container,
            text="Reset Password 🔑",
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        lbl_title.pack(anchor="w", pady=(5, 12))

        self.rst_user = ctk.CTkEntry(
            self.form_container,
            placeholder_text="👤 Username",
            height=40,
            corner_radius=10,
        )
        self.rst_user.pack(fill="x", pady=6)

        self.rst_pin = ctk.CTkEntry(
            self.form_container,
            placeholder_text="🔑 Security PIN",
            show="*",
            height=40,
            corner_radius=10,
        )
        self.rst_pin.pack(fill="x", pady=6)

        self.rst_new_pass = ctk.CTkEntry(
            self.form_container,
            placeholder_text="🔒 New Password",
            show="*",
            height=40,
            corner_radius=10,
        )
        self.rst_new_pass.pack(fill="x", pady=6)

        self.show_reset_password = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.form_container, text="Show new password",
            variable=self.show_reset_password,
            command=lambda: self.rst_new_pass.configure(
                show="" if self.show_reset_password.get() else "*"
            ),
        ).pack(anchor="w", pady=4)

        self.lbl_rst_msg = ctk.CTkLabel(
            self.form_container, text="", text_color="#EF4444", font=ctk.CTkFont(size=12)
        )
        self.lbl_rst_msg.pack(pady=2)

        btn_rst = ctk.CTkButton(
            self.form_container,
            text="Confirm Password Reset",
            height=42,
            corner_radius=10,
            fg_color="#3B82F6",
            hover_color="#2563EB",
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.handle_reset_password,
        )
        btn_rst.pack(fill="x", pady=(8, 0))

    def handle_login(self):
        self.lbl_login_err.configure(text="", text_color="#EF4444")
        username = self.login_user.get().strip()
        pwd = self.login_pass.get().strip()

        if not username or not pwd:
            self.lbl_login_err.configure(text="Please fill in all fields.")
            return

        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, password_hash, theme, currency FROM users WHERE username = ?",
            (username,),
        )
        row = cursor.fetchone()
        conn.close()

        if row and verify_secret(pwd, row[1]):
            user_id, stored_password_hash, theme, currency = row

            # Automatically upgrade an old SHA-256 password
            # after the user successfully logs in.
            if not stored_password_hash.startswith("pbkdf2_sha256$"):
                conn = DatabaseManager.get_connection()
                cursor = conn.cursor()

                cursor.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (hash_secret(pwd), user_id)
                )

                conn.commit()
                conn.close()

            ctk.set_appearance_mode(theme or "Dark")

            try:
                DatabaseManager.process_recurring_transactions(user_id)
            except (ValueError, sqlite3.Error) as error:
                self.lbl_login_err.configure(text="Could not process recurring payments.")
                messagebox.showerror("Recurring payment error", str(error))
                return

            self.on_login_success(
                user_id,
                username,
                currency or "৳",
                theme or "Dark"
            )

        else:
            self.lbl_login_err.configure(
                text="Invalid username or password."
            )

    def handle_register(self):
        username = self.reg_user.get().strip()
        pwd = self.reg_pass.get().strip()
        pin = self.reg_pin.get().strip()

        if not username:
            self.lbl_reg_err.configure(text="Please enter a username.")
            return

        if len(pwd) < 8:
            self.lbl_reg_err.configure(text="Password must be at least 8 characters.")
            return

        if not pin.isdigit() or len(pin) != 4:
            self.lbl_reg_err.configure(text="Security PIN must be exactly 4 digits.")
            return

        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO users (username, password_hash, pin_hash) VALUES (?, ?, ?)",
                (username, hash_secret(pwd), hash_secret(pin)),
            )
            user_id = cursor.lastrowid
            cursor.execute(
                "INSERT INTO accounts (user_id, name, type, balance) VALUES (?, ?, ?, ?)",
                (user_id, "Cash Wallet", "Cash", Decimal("0.00")),
            )
            conn.commit()
            conn.close()

            self.on_login_success(user_id, username, "৳", "Dark")
        except sqlite3.IntegrityError:
            conn.close()
            self.lbl_reg_err.configure(text="Username already exists.")

    def handle_reset_password(self):
        username = self.rst_user.get().strip()
        pin = self.rst_pin.get().strip()
        new_pass = self.rst_new_pass.get().strip()
        self.lbl_rst_msg.configure(text="", text_color="#EF4444")

        if not username or not pin:
            self.lbl_rst_msg.configure(text="Enter your username and Security PIN.")
            return
        if len(new_pass) < 8:
            self.lbl_rst_msg.configure(text="New password must be at least 8 characters.")
            return

        conn = None
        try:
            conn = DatabaseManager.get_connection()
            row = conn.execute(
                "SELECT id, pin_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
            if not row or not verify_secret(pin, row[1]):
                self.lbl_rst_msg.configure(text="Invalid username or Security PIN.")
                return
            # Migrate a legacy PIN only after it has been verified.
            with conn:
                conn.execute(
                    "UPDATE users SET password_hash = ?, pin_hash = ? WHERE id = ?",
                    (hash_secret(new_pass), hash_secret(pin), row[0]),
                )
            # Check the committed record through a new connection, just as login does.
            check_conn = DatabaseManager.get_connection()
            try:
                saved = check_conn.execute(
                    "SELECT password_hash FROM users WHERE id = ? AND username = ?",
                    (row[0], username),
                ).fetchone()
            finally:
                check_conn.close()
            if not saved or not verify_secret(new_pass, saved[0]):
                self.lbl_rst_msg.configure(
                    text="Password could not be verified. Please reset it again."
                )
                return
        except sqlite3.Error:
            self.lbl_rst_msg.configure(
                text="Could not save password. Close other Budgetly windows and retry."
            )
            return
        finally:
            if conn is not None:
                conn.close()

        self.rst_pin.delete(0, "end")
        self.rst_new_pass.delete(0, "end")
        # Return to the verified account; never prefill or retain its new password.
        self.mode_switch.set("Login")
        self.switch_mode("Login")
        self.login_user.delete(0, "end")
        self.login_user.insert(0, username)
        self.login_pass.delete(0, "end")
        self.lbl_login_err.configure(
            text="Password saved and verified. Enter your new password.",
            text_color="#10B981",
        )
        self.login_pass.focus_set()


# --- MODERN MAIN CONTAINER FRAME ---
class DashboardFrame(ctk.CTkFrame):

    def __init__(
        self, parent, user_id, username, currency, theme, on_logout_callback
    ):
        super().__init__(parent, fg_color="transparent")
        self.user_id = user_id
        self.username = username
        self.currency = currency
        self.theme = theme
        self.on_logout_callback = on_logout_callback

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.build_sidebar()

        self.content_area = ctk.CTkFrame(self, fg_color="transparent")
        self.content_area.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.content_area.grid_columnconfigure(0, weight=1)
        self.content_area.grid_rowconfigure(0, weight=1)

        self.current_subview = None
        self._views = {}
        self._view_hosts = {}
        self._transition_job = None
        self._view_versions = {}
        self._scroll_positions = {}
        # Keep this connection open: data_version compares commits across visits.
        self._change_watch = DatabaseManager.get_connection()
        self.show_dashboard()

    def build_sidebar(self):
        sidebar = ctk.CTkFrame(
            self,
            width=220,
            corner_radius=0,
            fg_color=("gray90", "#11111B"),
            border_width=1,
            border_color=("gray80", "#1E1E2E"),
        )
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_rowconfigure(8, weight=1)

        logo = ctk.CTkLabel(
            sidebar,
            text=f"💎 Budgetly",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=("gray10", "#F3F4F6"),
        )
        logo.grid(row=0, column=0, padx=20, pady=(25, 30))

        nav_items = [
            ("📊 Dashboard", self.show_dashboard),
            ("📜 History", self.show_history),
            ("💳 Accounts", self.show_accounts),
            ("🎯 Budgets", self.show_budgets),
            ("🔄 Recurring", self.show_recurring),
            ("📈 Analytics", self.show_analytics),
        ]

        self.nav_buttons = {}
        for idx, (label, command_func) in enumerate(nav_items, start=1):
            btn = ctk.CTkButton(
                sidebar,
                text=f"  {label}",
                fg_color="transparent",
                text_color=("gray20", "#D1D5DB"),
                hover_color=("gray80", "#1E293B"),
                anchor="w",
                height=40,
                corner_radius=10,
                font=ctk.CTkFont(size=13, weight="bold"),
                command=command_func,
            )
            btn.grid(row=idx, column=0, padx=15, pady=3, sticky="ew")
            self.nav_buttons[command_func.__name__] = btn

        btn_logout = ctk.CTkButton(
            sidebar,
            text="  🚪 Logout",
            fg_color="transparent",
            text_color="#EF4444",
            hover_color=("gray80", "#331919"),
            anchor="w",
            height=40,
            corner_radius=10,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self.on_logout_callback,
        )
        btn_logout.grid(row=7, column=0, padx=15, pady=3, sticky="ew")

        opt_mode = ctk.CTkOptionMenu(
            sidebar,
            values=["Dark", "Light"],
            height=36,
            corner_radius=10,
            command=self.change_theme_preference,
        )
        opt_mode.set(self.theme)
        opt_mode.grid(row=9, column=0, padx=20, pady=(5, 25))

    def change_theme_preference(self, new_theme):
        self.theme = new_theme
        ctk.set_appearance_mode(new_theme)
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET theme = ? WHERE id = ?",
            (new_theme, self.user_id),
        )
        conn.commit()
        conn.close()

    def switch_view(self, new_view_class, **kwargs):
        if isinstance(self.current_subview, new_view_class):
            return
        version = self._change_watch.execute("PRAGMA data_version").fetchone()[0]
        view = self._views.get(new_view_class)
        if view is None:
            host = ctk.CTkFrame(self.content_area, corner_radius=0,
                                fg_color=("gray92", "gray14"))
            host.grid_rowconfigure(0, weight=1)
            host.grid_columnconfigure(0, weight=1)
            try:
                view = new_view_class(host, self.user_id, self.username,
                                      self.currency, **kwargs)
            except Exception:
                host.destroy()
                raise
            view.grid(row=0, column=0, sticky="nsew")
            self._views[new_view_class] = view
            self._view_hosts[new_view_class] = host
        elif self._view_versions.get(new_view_class) != version:
            # Refresh records only when another connection has committed changes.
            # Entry widgets, filters, and the page itself remain alive.
            self._refresh_view(view)
        self._view_versions[new_view_class] = version
        previous = self.current_subview
        if previous is not None:
            self._scroll_positions[type(previous)] = previous._parent_canvas.yview()[0]
        self._cancel_transition()
        self.current_subview = view
        host = self._view_hosts[new_view_class]
        for page, other in self._view_hosts.items():
            if page != new_view_class and (previous is None or page != type(previous)):
                other.place_forget()
        host.place(x=12 if previous is not None else 0, y=0, relwidth=1, relheight=1)
        host.tkraise()
        view.update_idletasks()
        view._parent_canvas.yview_moveto(self._scroll_positions.get(new_view_class, 0))
        if previous is not None:
            self._animate_page(host)
        active = {DashboardView: "show_dashboard", HistoryView: "show_history",
                  AccountsView: "show_accounts", BudgetsView: "show_budgets",
                  RecurringView: "show_recurring", AnalyticsView: "show_analytics"}[new_view_class]
        for name, button in self.nav_buttons.items():
            button.configure(fg_color="#2563EB" if name == active else "transparent",
                             text_color="white" if name == active else ("gray20", "#D1D5DB"))

    def _cancel_transition(self):
        if self._transition_job is not None:
            self.after_cancel(self._transition_job)
            self._transition_job = None
        if self.current_subview is not None:
            self._view_hosts[type(self.current_subview)].place(x=0, y=0, relwidth=1, relheight=1)

    def _animate_page(self, host):
        started = time.monotonic()

        def step():
            self._transition_job = None
            progress = min(1.0, (time.monotonic() - started) / 0.15)
            # Cubic ease-out: short motion that settles gently without bouncing.
            host.place(x=round(12 * (1 - progress) ** 3), y=0, relwidth=1, relheight=1)
            if progress < 1:
                self._transition_job = self.after(16, step)
            else:
                for other in self._view_hosts.values():
                    if other is not host:
                        other.place_forget()

        step()

    def _refresh_view(self, view):
        if hasattr(view, "opt_account"):
            selected = view.account_choices.get(view.opt_account.get())
            choices = {f"{name} (#{aid})": aid for aid, name in user_accounts(self.user_id)}
            view.account_choices = choices
            labels = list(choices) or ["Create an account first"]
            view.opt_account.configure(values=labels)
            view.opt_account.set(next((label for label, aid in choices.items() if aid == selected), labels[0]))
        refresh = {DashboardView: "refresh_data", HistoryView: "load_transactions",
                   AccountsView: "refresh_accounts", BudgetsView: "refresh_list",
                   RecurringView: "refresh_rules", AnalyticsView: "build_matplotlib_charts"}
        getattr(view, refresh[type(view)])()

    def destroy(self):
        if getattr(self, "_transition_job", None) is not None:
            self.after_cancel(self._transition_job)
            self._transition_job = None
        if hasattr(self, "_change_watch"):
            self._change_watch.close()
        super().destroy()

    def show_dashboard(self):
        self.switch_view(DashboardView)

    def show_history(self):
        self.switch_view(HistoryView)

    def show_accounts(self):
        self.switch_view(AccountsView)

    def show_budgets(self):
        self.switch_view(BudgetsView)

    def show_recurring(self):
        self.switch_view(RecurringView)

    def show_analytics(self):
        self.switch_view(AnalyticsView)


# --- MODERN DASHBOARD VIEW ---
class DashboardView(ctk.CTkScrollableFrame):

    def __init__(self, parent, user_id, username, currency):
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.user_id = user_id
        self.username = username
        self.currency = currency
        self.grid_columnconfigure((0, 1, 2), weight=1)

        header = ctk.CTkLabel(
            self,
            text=f"Welcome back, {self.username} 👋",
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        header.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 20))

        self.lbl_networth = self.create_card(
            self, "Net Worth", f"{self.currency} 0.00", 0, accent="#2563EB"
        )
        self.lbl_income = self.create_card(
            self, "Total Income", f"{self.currency} 0.00", 1, accent="#10B981"
        )
        self.lbl_expense = self.create_card(
            self, "Total Expenses", f"{self.currency} 0.00", 2, accent="#EF4444"
        )

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(20, 0))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=2)

        self.build_transaction_form(body)
        self.build_budgets_panel(body)
        self.build_transactions_list(self)
        self.refresh_data()

    def create_card(self, parent, title, initial_val, col, accent):
        card = ctk.CTkFrame(
            parent,
            corner_radius=20,
            fg_color=("gray95", "#1E1E2E"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        card.grid(row=1, column=col, padx=8, pady=5, sticky="ew")

        indicator = ctk.CTkFrame(
            card, height=4, corner_radius=2, fg_color=accent
        )
        indicator.pack(fill="x", padx=15, pady=(12, 0))

        t_lbl = ctk.CTkLabel(
            card,
            text=title,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="gray60",
        )
        t_lbl.pack(anchor="w", padx=15, pady=(8, 0))

        val_lbl = ctk.CTkLabel(
            card, text=initial_val, font=ctk.CTkFont(size=22, weight="bold")
        )
        val_lbl.pack(anchor="w", padx=15, pady=(2, 16))
        return val_lbl

    def build_transaction_form(self, parent):
        form = ctk.CTkFrame(
            parent,
            corner_radius=20,
            fg_color=("gray95", "#1E1E2E"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        form.grid(row=0, column=0, padx=(0, 10), sticky="nsew")

        lbl = ctk.CTkLabel(
            form,
            text="⚡ Add Transaction",
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        lbl.pack(anchor="w", padx=18, pady=(18, 12))

        self.entry_title = ctk.CTkEntry(
            form, placeholder_text="Description (e.g. Dinner)", height=40, corner_radius=10
        )
        self.entry_title.pack(fill="x", padx=18, pady=5)

        self.entry_amt = ctk.CTkEntry(
            form, placeholder_text="Amount (e.g. 500)", height=40, corner_radius=10
        )
        self.entry_amt.pack(fill="x", padx=18, pady=5)

        self.entry_date = ctk.CTkEntry(
            form, placeholder_text="Date (YYYY-MM-DD)", height=40, corner_radius=10
        )
        self.entry_date.insert(0, datetime.datetime.now().strftime("%Y-%m-%d"))
        self.entry_date.pack(fill="x", padx=18, pady=5)

        self.opt_type = ctk.CTkOptionMenu(
            form, values=["Expense", "Income"], height=40, corner_radius=10
        )
        self.opt_type.pack(fill="x", padx=18, pady=5)

        self.opt_cat = ctk.CTkOptionMenu(
            form,
            values=[
                "Food & Dining",
                "Bills & Utilities",
                "Shopping",
                "Salary",
                "Other",
            ],
            height=40,
            corner_radius=10,
        )
        self.opt_cat.pack(fill="x", padx=18, pady=5)

        ctk.CTkLabel(form, text="Account").pack(anchor="w", padx=18)
        self.opt_account, self.account_choices = account_picker(form, self.user_id)
        self.opt_account.pack(fill="x", padx=18, pady=5)

        btn_add = ctk.CTkButton(
            form,
            text="Save Record",
            fg_color="#10B981",
            hover_color="#059669",
            height=42,
            corner_radius=10,
            font=ctk.CTkFont(size=14, weight="bold"),
            command=self.add_transaction,
        )
        btn_add.pack(fill="x", padx=18, pady=(12, 18))

    def build_budgets_panel(self, parent):
        self.budget_card = ctk.CTkFrame(
            parent,
            corner_radius=20,
            fg_color=("gray95", "#1E1E2E"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        self.budget_card.grid(row=0, column=1, sticky="nsew")

        lbl = ctk.CTkLabel(
            self.budget_card,
            text="🎯 Category Budgets — This Month",
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        lbl.pack(anchor="w", padx=18, pady=(18, 12))

        self.budget_container = ctk.CTkFrame(
            self.budget_card, fg_color="transparent"
        )
        self.budget_container.pack(fill="both", expand=True, padx=18, pady=5)

    def build_transactions_list(self, parent):
        tx_card = ctk.CTkFrame(
            parent,
            corner_radius=20,
            fg_color=("gray95", "#1E1E2E"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        tx_card.grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=(20, 10)
        )

        lbl = ctk.CTkLabel(
            tx_card,
            text="📜 Recent Activity",
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        lbl.pack(anchor="w", padx=18, pady=(18, 12))

        self.tx_container = ctk.CTkFrame(tx_card, fg_color="transparent")
        self.tx_container.pack(fill="x", padx=18, pady=(0, 18))

    def add_transaction(self):
        title = self.entry_title.get().strip()
        t_type = self.opt_type.get()
        category = self.opt_cat.get()
        try:
            save_transaction(self.user_id,self.account_choices.get(self.opt_account.get()),title,
                             self.entry_amt.get(),self.entry_date.get().strip(),category,t_type)
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror("Could not save transaction", str(error))
            return

        if t_type == "Expense":
            DatabaseManager.check_budget_alerts(self.user_id, category)

        self.entry_title.delete(0, "end")
        self.entry_amt.delete(0, "end")
        self.refresh_data()

    def refresh_data(self):
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            'SELECT SUM(balance) AS "total [MONEY_CENTS]" FROM accounts WHERE user_id = ?',
            (self.user_id,),
        )
        net_worth = cursor.fetchone()[0] or Decimal("0.00")

        cursor.execute(
            'SELECT SUM(amount) AS "total [MONEY_CENTS]" FROM transactions WHERE user_id = ? AND type = \'Income\'',
            (self.user_id,),
        )
        tot_income = cursor.fetchone()[0] or Decimal("0.00")

        cursor.execute(
            'SELECT SUM(amount) AS "total [MONEY_CENTS]" FROM transactions WHERE user_id = ? AND type = \'Expense\'',
            (self.user_id,),
        )
        tot_expense = cursor.fetchone()[0] or Decimal("0.00")

        self.lbl_networth.configure(text=f"{self.currency} {net_worth:,.2f}")
        self.lbl_income.configure(text=f"{self.currency} {tot_income:,.2f}")
        self.lbl_expense.configure(text=f"{self.currency} {tot_expense:,.2f}")

        for widget in self.budget_container.winfo_children():
            widget.destroy()

        cursor.execute(
            "SELECT category, amount_limit FROM budgets WHERE user_id = ?",
            (self.user_id,),
        )
        budgets = cursor.fetchall()

        for cat, limit in budgets:
            spent = monthly_spending(conn, self.user_id, cat)
            pct = min(spent / limit if limit > 0 else Decimal("0"), Decimal("1"))

            row = ctk.CTkFrame(self.budget_container, fg_color="transparent")
            row.pack(fill="x", pady=4)

            lbl_name = ctk.CTkLabel(
                row, text=cat, font=ctk.CTkFont(size=12, weight="bold")
            )
            lbl_name.pack(side="left")

            lbl_stat = ctk.CTkLabel(
                row,
                text=f"{self.currency}{spent:,.0f} / {self.currency}{limit:,.0f}",
                font=ctk.CTkFont(size=11),
                text_color="gray60",
            )
            lbl_stat.pack(side="right")

            bar_color = "#10B981" if pct < Decimal("0.8") else "#EF4444"
            pbar = ctk.CTkProgressBar(
                self.budget_container, progress_color=bar_color, height=8, corner_radius=4
            )
            pbar.pack(fill="x", pady=(0, 10))
            pbar.set(float(pct))

        for widget in self.tx_container.winfo_children():
            widget.destroy()

        cursor.execute(
            "SELECT date, title, category, type, amount FROM transactions WHERE user_id = ? ORDER BY id DESC LIMIT 5",
            (self.user_id,),
        )
        recent_txs = cursor.fetchall()

        if not recent_txs:
            empty_lbl = ctk.CTkLabel(
                self.tx_container,
                text="No recent transactions recorded.",
                text_color="gray50",
            )
            empty_lbl.pack(pady=10)
        else:
            for dt, title, cat, t_type, amt in recent_txs:
                t_row = ctk.CTkFrame(self.tx_container, fg_color="transparent")
                t_row.pack(fill="x", pady=4)

                lbl_t = ctk.CTkLabel(
                    t_row,
                    text=f"{title} ({cat})",
                    font=ctk.CTkFont(weight="bold"),
                )
                lbl_t.pack(side="left")

                color = "#10B981" if t_type == "Income" else "#EF4444"
                prefix = "+" if t_type == "Income" else "-"
                lbl_a = ctk.CTkLabel(
                    t_row,
                    text=f"{prefix}{self.currency}{amt:,.2f}",
                    text_color=color,
                    font=ctk.CTkFont(weight="bold"),
                )
                lbl_a.pack(side="right")

        conn.close()


# --- HISTORY VIEW WITH MODAL EDIT & PDF EXPORT ---
class HistoryView(ctk.CTkScrollableFrame):

    def __init__(self, parent, user_id, username, currency):
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.user_id = user_id
        self.currency = currency

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", pady=(0, 15))

        lbl = ctk.CTkLabel(
            top,
            text="📜 Transaction History",
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        lbl.pack(side="left")

        btn_pdf = ctk.CTkButton(
            top,
            text="📄 Export PDF",
            fg_color="#10B981",
            hover_color="#059669",
            height=38,
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.export_pdf,
        )
        btn_pdf.pack(side="right", padx=(8, 0))

        btn_csv = ctk.CTkButton(
            top,
            text="📥 Export CSV",
            fg_color="#3B82F6",
            hover_color="#2563EB",
            height=38,
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.export_csv,
        )
        btn_csv.pack(side="right")

        filter_card = ctk.CTkFrame(
            self,
            corner_radius=20,
            fg_color=("gray95", "#1E1E2E"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        filter_card.pack(fill="x", pady=(0, 15))

        self.ent_search = ctk.CTkEntry(
            filter_card, placeholder_text="Search title...", height=40, corner_radius=10
        )
        self.ent_search.pack(
            side="left", padx=15, pady=12, expand=True, fill="x"
        )

        self.opt_cat_filter = ctk.CTkOptionMenu(
            filter_card,
            values=[
                "All Categories",
                "Food & Dining",
                "Bills & Utilities",
                "Shopping",
                "Salary",
                "Other",
            ],
            height=40,
            corner_radius=10,
            command=lambda _: self.load_transactions(),
        )
        self.opt_cat_filter.pack(side="left", padx=5, pady=12)

        btn_filter = ctk.CTkButton(
            filter_card,
            text="Search",
            height=40,
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.load_transactions,
        )
        btn_filter.pack(side="left", padx=(5, 15), pady=12)

        self.list_container = ctk.CTkFrame(
            self,
            corner_radius=20,
            fg_color=("gray95", "#1E1E2E"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        self.list_container.pack(fill="x")

        self.load_transactions()

    def load_transactions(self):
        for w in self.list_container.winfo_children():
            w.destroy()

        search_q = f"%{self.ent_search.get().strip()}%"
        cat_filter = self.opt_cat_filter.get()

        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()

        if cat_filter == "All Categories":
            cursor.execute(
                """
                SELECT id, date, title, category, type, amount, account_id 
                FROM transactions 
                WHERE user_id = ? AND title LIKE ?
                ORDER BY id DESC
            """,
                (self.user_id, search_q),
            )
        else:
            cursor.execute(
                """
                SELECT id, date, title, category, type, amount, account_id 
                FROM transactions 
                WHERE user_id = ? AND category = ? AND title LIKE ?
                ORDER BY id DESC
            """,
                (self.user_id, cat_filter, search_q),
            )

        records = cursor.fetchall()
        conn.close()

        if not records:
            empty = ctk.CTkLabel(
                self.list_container,
                text="No matching transactions found.",
                text_color="gray50",
            )
            empty.pack(pady=25)
            return

        account_names = dict(user_accounts(self.user_id))
        for tx_id, dt, title, cat, t_type, amt, acc_id in records:
            row = ctk.CTkFrame(self.list_container, fg_color="transparent")
            row.pack(fill="x", padx=15, pady=6)

            d_lbl = ctk.CTkLabel(
                row,
                text=dt,
                text_color="gray60",
                width=90,
                font=ctk.CTkFont(size=11),
            )
            d_lbl.pack(side="left")

            t_lbl = ctk.CTkLabel(
                row,
                text=f"{title} ({cat})\n{account_names.get(acc_id, 'Unknown account')}",
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            t_lbl.pack(side="left", padx=10)

            tx_tuple = (tx_id, dt, title, cat, t_type, amt, acc_id)

            btn_del = ctk.CTkButton(
                row,
                text="🗑️",
                width=32,
                height=32,
                corner_radius=8,
                fg_color="#EF4444",
                hover_color="#DC2626",
                command=lambda tid=tx_id, aid=acc_id, tt=t_type, a=amt: self.delete_transaction(
                    tid, aid, tt, a
                ),
            )
            btn_del.pack(side="right", padx=(5, 0))

            btn_edit = ctk.CTkButton(
                row,
                text="✏️",
                width=32,
                height=32,
                corner_radius=8,
                fg_color="#F59E0B",
                hover_color="#D97706",
                command=lambda txt=tx_tuple: EditTransactionModal(
                    self, self.user_id, txt, self.load_transactions
                ),
            )
            btn_edit.pack(side="right", padx=(10, 0))

            color = "#10B981" if t_type == "Income" else "#EF4444"
            prefix = "+" if t_type == "Income" else "-"
            a_lbl = ctk.CTkLabel(
                row,
                text=f"{prefix}{self.currency}{amt:,.2f}",
                text_color=color,
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            a_lbl.pack(side="right")

    def delete_transaction(self, tx_id, acc_id, t_type, amt):
        try:
            delete_transaction_record(self.user_id, tx_id)
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror("Could not delete transaction", str(error))
            return
        self.load_transactions()

    def export_csv(self):
        filePath = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV Files", "*.csv")]
        )
        if not filePath:
            return

        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT date, title, category, type, amount 
            FROM transactions 
            WHERE user_id = ? ORDER BY id DESC
        """,
            (self.user_id,),
        )
        txs = cursor.fetchall()
        conn.close()

        with open(filePath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Date", "Title", "Category", "Type", "Amount"])
            writer.writerows(txs)

    def export_pdf(self):
        path = filedialog.asksaveasfilename(defaultextension=".pdf",filetypes=[("PDF Documents","*.pdf")])
        if not path:
            return
        try:
            export_transactions_pdf(self.user_id,path)
        except (OSError, ValueError, sqlite3.Error) as error:
            messagebox.showerror("Could not export PDF",str(error))


# --- RECURRING TRANSACTIONS VIEW ---
class RecurringView(ctk.CTkScrollableFrame):

    def __init__(self, parent, user_id, username, currency):
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.user_id = user_id
        self.currency = currency

        lbl = ctk.CTkLabel(
            self,
            text="🔄 Recurring Payments",
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        lbl.pack(anchor="w", pady=(0, 15))

        ctk.CTkLabel(self,text="Account for new recurring payments").pack(anchor="w")
        self.opt_account, self.account_choices = account_picker(self,self.user_id)
        self.opt_account.pack(fill="x",pady=(4,12))
        card = ctk.CTkFrame(
            self,
            corner_radius=20,
            fg_color=("gray95", "#1E1E2E"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        card.pack(fill="x", pady=(0, 15))

        f_lbl = ctk.CTkLabel(
            card,
            text="Add Monthly Auto-Payment Rule",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        f_lbl.pack(anchor="w", padx=15, pady=(15, 5))

        self.ent_title = ctk.CTkEntry(
            card, placeholder_text="Name (e.g. Netflix)", height=40, corner_radius=10
        )
        self.ent_title.pack(side="left", padx=10, pady=12, expand=True, fill="x")

        self.ent_amt = ctk.CTkEntry(
            card, placeholder_text="Amount", width=100, height=40, corner_radius=10
        )
        self.ent_amt.pack(side="left", padx=5, pady=12)

        self.ent_dom = ctk.CTkEntry(
            card, placeholder_text="Due Day (1-28)", width=110, height=40, corner_radius=10
        )
        self.ent_dom.pack(side="left", padx=5, pady=12)

        self.opt_type = ctk.CTkOptionMenu(
            card, values=["Expense", "Income"], width=100, height=40, corner_radius=10
        )
        self.opt_type.pack(side="left", padx=5, pady=12)

        self.opt_cat = ctk.CTkOptionMenu(
            card,
            values=[
                "Bills & Utilities",
                "Food & Dining",
                "Shopping",
                "Salary",
                "Other",
            ],
            width=130,
            height=40,
            corner_radius=10,
        )
        self.opt_cat.pack(side="left", padx=5, pady=12)

        btn = ctk.CTkButton(
            card,
            text="+ Add Rule",
            fg_color="#10B981",
            hover_color="#059669",
            width=90,
            height=40,
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.add_rule,
        )
        btn.pack(side="left", padx=(5, 15), pady=12)

        self.list_card = ctk.CTkFrame(
            self,
            corner_radius=20,
            fg_color=("gray95", "#1E1E2E"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        self.list_card.pack(fill="x")
        self.refresh_rules()

    def add_rule(self):
        try:
            create_recurring_rule(self.user_id,self.account_choices.get(self.opt_account.get()),
                                  self.ent_title.get(),self.ent_amt.get(),self.ent_dom.get(),
                                  self.opt_type.get(),self.opt_cat.get())
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror("Could not save recurring rule", str(error))
            return
        self.ent_title.delete(0,"end")
        self.ent_amt.delete(0,"end")
        self.ent_dom.delete(0,"end")
        self.refresh_rules()

    def refresh_rules(self):
        for w in self.list_card.winfo_children():
            w.destroy()

        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, title, amount, type, category, day_of_month 
            FROM recurring_rules WHERE user_id = ?
        """,
            (self.user_id,),
        )
        rules = cursor.fetchall()
        conn.close()

        lbl = ctk.CTkLabel(
            self.list_card,
            text="Active Recurring Rules",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        lbl.pack(anchor="w", padx=15, pady=(15, 10))

        for rid, title, amt, t_type, cat, dom in rules:
            row = ctk.CTkFrame(self.list_card, fg_color="transparent")
            row.pack(fill="x", padx=15, pady=5)

            r_lbl = ctk.CTkLabel(
                row,
                text=f"{title} ({cat}) - Every {dom}th of the month",
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            r_lbl.pack(side="left")

            btn_del = ctk.CTkButton(
                row,
                text="🗑️",
                width=32,
                height=32,
                corner_radius=8,
                fg_color="#EF4444",
                hover_color="#DC2626",
                command=lambda rule_id=rid: self.delete_rule(rule_id),
            )
            btn_del.pack(side="right", padx=(10, 0))

            color = "#10B981" if t_type == "Income" else "#EF4444"
            a_lbl = ctk.CTkLabel(
                row,
                text=f"{self.currency}{amt:,.2f}",
                text_color=color,
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            a_lbl.pack(side="right")

    def delete_rule(self, rule_id):
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM recurring_rules WHERE id = ? AND user_id = ?", (rule_id, self.user_id))
        conn.commit()
        conn.close()
        self.refresh_rules()


# --- MATPLOTLIB VISUAL ANALYTICS VIEW ---
class AnalyticsView(ctk.CTkScrollableFrame):

    def __init__(self, parent, user_id, username, currency):
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.user_id = user_id
        self.currency = currency

        lbl = ctk.CTkLabel(
            self,
            text="📈 Visual Analytics & Charts",
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        lbl.pack(anchor="w", pady=(0, 15))

        self.build_matplotlib_charts()

    def build_matplotlib_charts(self):
        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()

        cursor.execute(
            'SELECT category, SUM(amount) AS "total [MONEY_CENTS]" FROM transactions WHERE user_id = ? AND type = \'Expense\' GROUP BY category',
            (self.user_id,),
        )
        cat_data = cursor.fetchall()

        cursor.execute(
            'SELECT type, SUM(amount) AS "total [MONEY_CENTS]" FROM transactions WHERE user_id = ? GROUP BY type',
            (self.user_id,),
        )
        type_data = dict(cursor.fetchall())
        conn.close()

        if hasattr(self, "_figure"):
            fig = self._figure
            ax1, ax2 = self._axes
            ax1.clear()
            ax2.clear()
            ax1.set_axis_on()
            ax2.set_axis_on()
        else:
            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9, 4.2), facecolor="#181825")
            self._figure, self._axes = fig, (ax1, ax2)

        # --- Chart 1: Expense Breakdown Pie ---
        if cat_data:
            labels = [row[0] for row in cat_data]
            values = [float(row[1]) for row in cat_data]
            ax1.pie(
                values,
                labels=labels,
                autopct="%1.1f%%",
                textprops={"color": "white", "fontsize": 8},
            )
            ax1.set_title(
                "Expense Breakdown", color="white", fontsize=11, fontweight="bold"
            )
        else:
            ax1.text(
                0.5,
                0.5,
                "No Expense Data",
                color="gray",
                ha="center",
                va="center",
            )
            ax1.axis("off")

        # --- Chart 2: Income vs Expense Bar Graph ---
        inc = float(type_data.get("Income", Decimal("0.00")))
        exp = float(type_data.get("Expense", Decimal("0.00")))

        bars = ax2.bar(
            ["Income", "Expense"],
            [inc, exp],
            color=["#10B981", "#EF4444"],
            width=0.45,
        )
        ax2.set_facecolor("#181825")
        ax2.tick_params(colors="white")
        ax2.spines["bottom"].set_color("white")
        ax2.spines["left"].set_color("white")
        ax2.spines["top"].set_visible(False)
        ax2.spines["right"].set_visible(False)
        ax2.set_title(
            "Income vs Expense", color="white", fontsize=11, fontweight="bold"
        )

        for bar in bars:
            yval = bar.get_height()
            ax2.text(
                bar.get_x() + bar.get_width() / 2,
                yval,
                f"{yval:,.0f}",
                ha="center",
                va="bottom",
                color="white",
                fontsize=8,
            )

        fig.tight_layout()

        if not hasattr(self, "_chart_canvas"):
            self._chart_canvas = FigureCanvasTkAgg(fig, master=self)
            self._chart_canvas.get_tk_widget().pack(fill="both", expand=True, pady=10)
        self._chart_canvas.draw()

    def destroy(self):
        if hasattr(self, "_figure"):
            plt.close(self._figure)
        super().destroy()


# --- ACCOUNTS VIEW ---
class AccountsView(ctk.CTkScrollableFrame):

    def __init__(self, parent, user_id, username, currency):
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.user_id = user_id
        self.currency = currency

        lbl = ctk.CTkLabel(
            self,
            text="💳 Payment Accounts",
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        lbl.pack(anchor="w", pady=(0, 15))

        add_card = ctk.CTkFrame(
            self,
            corner_radius=20,
            fg_color=("gray95", "#1E1E2E"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        add_card.pack(fill="x", pady=(0, 15))

        f_lbl = ctk.CTkLabel(
            add_card,
            text="Add New Account / Wallet",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        f_lbl.pack(anchor="w", padx=15, pady=(15, 5))

        self.ent_acc_name = ctk.CTkEntry(
            add_card, placeholder_text="Account Name (e.g. Bank Asia)", height=40, corner_radius=10
        )
        self.ent_acc_name.pack(side="left", padx=15, pady=12, expand=True, fill="x")

        self.opt_acc_type = ctk.CTkOptionMenu(
            add_card,
            values=["Cash", "Bank Account", "Mobile Wallet", "Credit Card"],
            height=40,
            corner_radius=10,
        )
        self.opt_acc_type.pack(side="left", padx=5, pady=12)

        self.ent_acc_bal = ctk.CTkEntry(
            add_card, placeholder_text="Initial Balance", width=120, height=40, corner_radius=10
        )
        self.ent_acc_bal.pack(side="left", padx=5, pady=12)

        btn_add = ctk.CTkButton(
            add_card,
            text="+ Add",
            fg_color="#10B981",
            hover_color="#059669",
            width=80,
            height=40,
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.add_account,
        )
        btn_add.pack(side="left", padx=(5, 15), pady=12)

        self.list_card = ctk.CTkFrame(
            self,
            corner_radius=20,
            fg_color=("gray95", "#1E1E2E"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        self.list_card.pack(fill="x")
        ctk.CTkButton(self, text="Transfer between accounts", command=self.open_transfer).pack(pady=12)
        self.transfer_history = ctk.CTkFrame(self)
        self.transfer_history.pack(fill="x",pady=8)
        self.refresh_accounts()

    def open_transfer(self):
        if len(user_accounts(self.user_id)) < 2:
            messagebox.showinfo("Add an account", "Create at least two accounts before transferring money.")
            return
        TransferModal(self,self.user_id,self.refresh_accounts)

    def add_account(self):
        try:
            create_account(self.user_id,self.ent_acc_name.get(),self.opt_acc_type.get(),self.ent_acc_bal.get())
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror("Could not save account", str(error))
            return
        self.ent_acc_name.delete(0,"end")
        self.ent_acc_bal.delete(0,"end")
        self.refresh_accounts()

    def refresh_accounts(self):
        for w in self.transfer_history.winfo_children():
            w.destroy()
        ctk.CTkLabel(self.transfer_history,text="Transfer History (latest 50)",font=ctk.CTkFont(size=14,weight="bold")).pack(pady=10)
        conn = DatabaseManager.get_connection()
        try:
            transfers = conn.execute("""SELECT t.date, a.name, b.name, t.amount, t.note
                FROM transfers t JOIN accounts a ON a.id=t.from_account_id
                JOIN accounts b ON b.id=t.to_account_id WHERE t.user_id=? ORDER BY t.date DESC,t.id DESC LIMIT 50""",(self.user_id,)).fetchall()
        finally:
            conn.close()
        for date, source, destination, amount, note in transfers:
            ctk.CTkLabel(self.transfer_history,text=f"{date}  |  {source} → {destination}  |  {self.currency}{amount:,.2f}\n{note}",wraplength=650).pack(anchor="w",padx=16,pady=6)
        if not transfers:
            ctk.CTkLabel(self.transfer_history,text="No transfers yet.").pack(pady=8)
        for w in self.list_card.winfo_children():
            w.destroy()

        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name, type, balance FROM accounts WHERE user_id = ?",
            (self.user_id,),
        )
        accs = cursor.fetchall()
        conn.close()

        lbl = ctk.CTkLabel(
            self.list_card,
            text="Active Accounts",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        lbl.pack(anchor="w", padx=15, pady=(15, 10))

        for name, a_type, bal in accs:
            row = ctk.CTkFrame(self.list_card, fg_color="transparent")
            row.pack(fill="x", padx=15, pady=5)

            n_lbl = ctk.CTkLabel(
                row,
                text=f"{name} ({a_type})",
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            n_lbl.pack(side="left")

            b_lbl = ctk.CTkLabel(
                row,
                text=f"{self.currency} {bal:,.2f}",
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            b_lbl.pack(side="right")


# --- BUDGETS VIEW ---
class BudgetsView(ctk.CTkScrollableFrame):

    def __init__(self, parent, user_id, username, currency):
        super().__init__(parent, fg_color="transparent", corner_radius=0)
        self.user_id = user_id
        self.currency = currency

        lbl = ctk.CTkLabel(
            self,
            text="🎯 Manage Category Budgets",
            font=ctk.CTkFont(size=24, weight="bold"),
        )
        lbl.pack(anchor="w", pady=(0, 15))

        card = ctk.CTkFrame(
            self,
            corner_radius=20,
            fg_color=("gray95", "#1E1E2E"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        card.pack(fill="x", pady=(0, 15))

        f_lbl = ctk.CTkLabel(
            card,
            text="Set Monthly Category Spending Limit",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        f_lbl.pack(anchor="w", padx=15, pady=(15, 5))

        self.opt_cat = ctk.CTkOptionMenu(
            card,
            values=[
                "Food & Dining",
                "Bills & Utilities",
                "Shopping",
                "Entertainment",
                "Other",
            ],
            height=40,
            corner_radius=10,
        )
        self.opt_cat.pack(side="left", padx=15, pady=12)

        self.ent_limit = ctk.CTkEntry(
            card, placeholder_text="Target Limit", width=150, height=40, corner_radius=10
        )
        self.ent_limit.pack(side="left", padx=5, pady=12)

        btn = ctk.CTkButton(
            card,
            text="Update Target",
            fg_color="#10B981",
            hover_color="#059669",
            height=40,
            corner_radius=10,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self.save_budget,
        )
        btn.pack(side="left", padx=(5, 15), pady=12)

        self.list_card = ctk.CTkFrame(
            self,
            corner_radius=20,
            fg_color=("gray95", "#1E1E2E"),
            border_width=1,
            border_color=("gray85", "#2D2D3F"),
        )
        self.list_card.pack(fill="x")
        self.refresh_list()

    def save_budget(self):
        try:
            set_budget(self.user_id,self.opt_cat.get(),self.ent_limit.get())
        except (ValueError, sqlite3.Error) as error:
            messagebox.showerror("Could not save budget", str(error))
            return
        self.ent_limit.delete(0,"end")
        self.refresh_list()

    def refresh_list(self):
        for w in self.list_card.winfo_children():
            w.destroy()

        conn = DatabaseManager.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT category, amount_limit FROM budgets WHERE user_id = ?",
            (self.user_id,),
        )
        budgets = cursor.fetchall()
        conn.close()

        lbl = ctk.CTkLabel(
            self.list_card,
            text="Active Budget Targets",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        lbl.pack(anchor="w", padx=15, pady=(15, 10))

        for cat, limit in budgets:
            row = ctk.CTkFrame(self.list_card, fg_color="transparent")
            row.pack(fill="x", padx=15, pady=5)

            c_lbl = ctk.CTkLabel(
                row, text=cat, font=ctk.CTkFont(size=12, weight="bold")
            )
            c_lbl.pack(side="left")

            l_lbl = ctk.CTkLabel(
                row,
                text=f"{self.currency} {limit:,.2f}",
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            l_lbl.pack(side="right")


# --- ROOT APPLICATION CLASS ---
class MainApplication(ctk.CTk):

    def after(self, ms, func=None, *args):
        """Guard delayed focus restoration when its target screen was destroyed.

        CustomTkinter can schedule a bound Tk focus method on the root during
        Windows theme changes. Canvas.focus also accesses the canvas command,
        so calling it after the canvas has been destroyed raises TclError.
        Other callbacks retain their usual behavior and error reporting.
        """
        if getattr(self, "_closing", False):
            return None
        target = getattr(func, "__self__", None)
        name = getattr(func, "__name__", "")
        if isinstance(target, tk.Misc) and name in {"focus", "focus_set", "focus_force"}:
            original = func

            def focus_if_alive(*callback_args):
                if target.winfo_exists():
                    return original(*callback_args)

            func = focus_if_alive
        return super().after(ms, func, *args)

    def __init__(self):
        super().__init__()
        self.title(f"Budgetly Pro {APP_VERSION}")
        self.geometry("1120x720")

        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.current_frame = None
        self.show_auth_view()

    def destroy(self):
        """Cancel interpreter timers before their widget commands disappear."""
        if getattr(self, "_closing", False):
            return
        self._closing = True
        # Use Tcl cancellation so each widget retains ownership of its Python
        # callback command and deletes that command in its own destroy method.
        for timer_id in self.tk.splitlist(self.tk.call("after", "info")):
            self.tk.call("after", "cancel", timer_id)
        super().destroy()

    def show_auth_view(self):
        self.focus_set()
        if self.current_frame:
            self.current_frame.destroy()

        self.current_frame = AuthFrame(
            self, on_login_success=self.show_dashboard_view
        )
        self.current_frame.pack(fill="both", expand=True)

    def show_dashboard_view(self, user_id, username, currency, theme):
        self.focus_set()
        if self.current_frame:
            self.current_frame.destroy()

        self.title(f"Budgetly Pro - {username}")
        self.current_frame = DashboardFrame(
            self,
            user_id,
            username,
            currency,
            theme,
            on_logout_callback=self.show_auth_view,
        )
        self.current_frame.pack(fill="both", expand=True)


if __name__ == "__main__":
    try:
        DatabaseManager.init_db()
    except (ValueError, sqlite3.Error, OSError) as error:
        error_window = tk.Tk()
        error_window.withdraw()
        messagebox.showerror("Budgetly could not open the database", str(error), parent=error_window)
        error_window.destroy()
        raise SystemExit(1)
    app = MainApplication()
    app.mainloop()
