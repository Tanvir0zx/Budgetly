# Budgetly: personal-finance desktop prototype

## Purpose

Budgetly explores how a desktop application can help a user organize income, expenses, and money held in multiple accounts. Its main engineering challenge is keeping financial records and account balances consistent while supporting a usable interface.

## Technical design

Python provides the application logic, CustomTkinter provides the interface, and SQLite stores users, accounts, transactions, transfers, budgets, recurring rules, and recurring occurrences. Matplotlib supplies charts; ReportLab generates PDF reports.

Monetary values are stored as integer minor units. Decimal values are used at the input/output boundary. A transfer is a separate record, and its debit, credit, and log entry are committed in a single database transaction. A unique rule/month key prevents repeated recurring occurrences.

## Development examples

- An earlier version accepted non-finite amounts in some forms. Shared validators and database guards were added.
- Deletion previously trusted values captured by an older screen. It now reads the current transaction inside a transaction lock before reversing its effect.
- Simultaneous recurring processing could duplicate a charge. A locked operation and unique monthly occurrence record were introduced.
- PDF export previously stopped at 35 records. It was changed to paginate the complete user transaction set.
- Money was migrated from REAL values to integer minor units, with a backup, value checks, and rollback on failure.

## Contribution and AI assistance

Confirmed from the development conversation: the project owner chose requirements and design preferences, supplied error messages, reviewed revisions, and reported successful Windows checks. ChatGPT/Codex wrote and revised substantial portions of the code and produced automated tests and documentation.

Before submission, the owner should add only work and learning they can personally explain. Suggested wording, to use only if accurate:

> I directed and tested Budgetly, a Python desktop finance prototype, with substantial AI assistance. I used the project to study GUI development, relational data, authentication, and consistent financial updates. I investigated runtime errors with AI support and checked the revised application on Windows.

Do not claim a development duration, independent authorship, professional users, production deployment, or comprehensive testing unless those statements are true.

## Questions to prepare for

1. Why does the database store 100.50 as 10050?
2. Why is a transfer different from income or an expense?
3. What happens if a transfer's second database update fails?
4. How does the rule/month key prevent recurring duplicates?
5. What is password hashing, and what limitations remain in PIN recovery?
6. What can and cannot be concluded from automated tests versus manual GUI checks?
7. What would you improve next, and why?
