"""Temporary-database regression tests; no visible window or personal data."""
import datetime, sqlite3, tempfile, threading, unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import main as app

class BudgetlySmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_db = app.DB_FILE
        app.DB_FILE = str(Path(self.temp.name) / 'test.db')
        app.DatabaseManager.init_db()
        with closing(app.DatabaseManager.get_connection()) as conn, conn:
            conn.execute("INSERT INTO users(id,username,password_hash,pin_hash) VALUES(1,'test','unused','unused'),(2,'other','unused','unused')")
        for user, name, amount in [(1,'Cash','1000'),(1,'Bank','2000'),(2,'Other','100')]:
            app.create_account(user,name,'Cash',amount)
        self.today = datetime.date.today().isoformat()
    def tearDown(self):
        app.DB_FILE = self.previous_db
        self.temp.cleanup()
    def query(self,sql,args=()):
        with closing(app.DatabaseManager.get_connection()) as conn, conn:
            return conn.execute(sql,args).fetchall()
    def test_password_hashing(self):
        stored=app.hash_secret('demonstration-password')
        self.assertTrue(app.verify_secret('demonstration-password',stored))
        self.assertFalse(app.verify_secret('incorrect',stored))
        self.assertNotEqual(stored,app.hash_secret('demonstration-password'))
        self.assertTrue(app.verify_secret('legacy',app.legacy_hash_str('legacy')))
    def test_exact_cents(self):
        for value in ('0.10','0.20'):
            app.save_transaction(1,1,'Sample',value,self.today,'Salary','Income')
        self.assertEqual(self.query('SELECT balance FROM accounts WHERE id=1'),[(Decimal('1000.30'),)])
        with closing(sqlite3.connect(app.DB_FILE)) as conn, conn:
            self.assertEqual(conn.execute('SELECT balance,typeof(balance) FROM accounts WHERE id=1').fetchone(),(100030,'integer'))
    def test_transfer_totals(self):
        app.transfer_money(1,1,2,'300',self.today)
        self.assertEqual(self.query('SELECT balance FROM accounts WHERE user_id=1 ORDER BY id'),[(Decimal('700'),),(Decimal('2300'),)])
        self.assertEqual(self.query('SELECT COUNT(*) FROM transactions'),[(0,)])
    def test_transfer_rollback(self):
        with closing(app.DatabaseManager.get_connection()) as conn, conn:
            conn.execute("CREATE TRIGGER fail BEFORE INSERT ON transfers BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):app.transfer_money(1,1,2,'1',self.today)
        self.assertEqual(self.query('SELECT balance FROM accounts WHERE user_id=1 ORDER BY id'),[(Decimal('1000'),),(Decimal('2000'),)])
    def test_invalid_money_and_ownership(self):
        for value in ('nan','inf','-inf','1.001','1000000000000'):
            with self.subTest(value=value),self.assertRaises(ValueError):app.create_account(1,'Bad','Cash',value)
        for source,target,value in [(1,1,'1'),(1,3,'1'),(1,2,'1001')]:
            with self.assertRaises(ValueError):app.transfer_money(1,source,target,value,self.today)
    def test_edit_and_repeat_delete(self):
        app.save_transaction(1,1,'Expense','100',self.today,'Food','Expense')
        tx=self.query('SELECT id FROM transactions')[0][0]
        app.save_transaction(1,2,'Moved','150',self.today,'Food','Expense',tx)
        self.assertFalse(app.delete_transaction_record(2,tx))
        self.assertTrue(app.delete_transaction_record(1,tx))
        self.assertFalse(app.delete_transaction_record(1,tx))
        self.assertEqual(self.query('SELECT balance FROM accounts WHERE user_id=1 ORDER BY id'),[(Decimal('1000'),),(Decimal('2000'),)])
    def test_recurring_concurrency(self):
        app.create_recurring_rule(1,1,'Monthly','0.01',1,'Expense','Bills')
        barrier=threading.Barrier(2); failures=[]
        def run():
            try:barrier.wait(timeout=5);app.DatabaseManager.process_recurring_transactions(1)
            except Exception as error:failures.append(error)
        threads=[threading.Thread(target=run) for _ in range(2)]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(failures,[])
        self.assertEqual(self.query('SELECT COUNT(*) FROM transactions'),[(1,)])
        self.assertEqual(self.query('SELECT balance FROM accounts WHERE id=1'),[(Decimal('999.99'),)])
    def test_monthly_budget_calculation(self):
        app.save_transaction(1,1,'Older','100','2025-01-01','Food','Expense')
        app.save_transaction(1,1,'Current','50','2026-09-01','Food','Expense')
        with closing(app.DatabaseManager.get_connection()) as conn, conn:
            self.assertEqual(app.monthly_spending(conn,1,'Food','2026-09'),Decimal('50'))
    def test_pdf_export(self):
        for number in range(80):app.save_transaction(1,1,f'Sample {number}','1',self.today,'Food','Expense')
        target=Path(self.temp.name)/'report.pdf'
        self.assertEqual(app.export_transactions_pdf(1,str(target)),80)
        self.assertTrue(target.read_bytes().startswith(b'%PDF-'))
    def test_legacy_migration(self):
        legacy=Path(self.temp.name)/'legacy.db';app.DB_FILE=str(legacy)
        with closing(sqlite3.connect(legacy)) as conn, conn:
            conn.executescript("""CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, pin_hash TEXT NOT NULL);
                CREATE TABLE accounts(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,name TEXT NOT NULL,type TEXT NOT NULL,balance REAL DEFAULT 0.0,FOREIGN KEY(user_id) REFERENCES users(id));
                INSERT INTO users VALUES(1,'legacy','unused','unused'); INSERT INTO accounts VALUES(1,1,'Cash','Cash',100.50);""")
        app.DatabaseManager.init_db();app.DatabaseManager.init_db()
        self.assertEqual(self.query('SELECT balance FROM accounts'),[(Decimal('100.50'),)])
        with closing(sqlite3.connect(legacy)) as conn, conn:self.assertEqual(conn.execute('SELECT balance,typeof(balance) FROM accounts').fetchone(),(10050,'integer'))
        self.assertEqual(len(list(legacy.parent.glob('legacy.db.before_2_3_0_*.bak'))),1)
    def test_demo_does_not_overwrite(self):
        import demo
        target=Path(self.temp.name)/'fictional'/'demo.db'
        with patch.object(demo,'DEMO_DB',target):
            demo.create_demo();before=target.read_bytes()
            with self.assertRaises(FileExistsError):demo.create_demo()
            self.assertEqual(target.read_bytes(),before)
            self.assertEqual(self.query('SELECT username FROM users'),[('demo',)])

if __name__=='__main__':unittest.main()
