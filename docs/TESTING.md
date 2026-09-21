# Testing evidence

## Packaged smoke-test run

A new Python virtual environment was created for this package and dependencies were installed from `requirements.txt`. The actual `main.py` module was imported; no source extraction or GUI stub was used for these packaged tests.

Environment: Linux, Python 3.12.14. Installed direct dependencies: CustomTkinter 5.2.2, Matplotlib 3.11.2, ReportLab 4.5.1. These are the versions observed in the test environment, not a statement that every supported version has been tested.

Command:

```text
python -m unittest discover -s tests -v
```

Result: **11 tests passed**.

| Test | Scope |
|---|---|
| Password hashing | Correct/wrong passwords, random salts, legacy verification |
| Exact cents | Decimal input, exact 0.10 + 0.20 balance update, integer SQL storage |
| Transfer totals | Correct debit/credit and no income/expense record |
| Transfer rollback | A forced log insertion failure rolls back both balances |
| Validation and ownership | Non-finite/over-precision amounts, same account, foreign account, insufficient funds |
| Edit/delete | Move account, restore balances, repeated deletion, user ownership |
| Recurring concurrency | Two concurrent calls generate only one monthly charge |
| Monthly budgets | A selected month's total excludes older transactions |
| PDF export | 80 records are passed to the report and a PDF file is created |
| Legacy migration | REAL to integer storage, preserved displayed amount, repeated startup and backup |
| Demo safety | Fictional data generation and refusal to overwrite an existing demo |

The test suite does not use your personal database. It creates temporary databases and cleans them up. The included PDF test is not a page-by-page visual or text-completeness check.

## Earlier development evidence

During prior development, separate tests exercised password-reset handlers, legacy migration with IDs and recurring markers, tiny floating-point residue, fractional-cent rollback, CSV unit display, and dashboard values. An 80-record PDF was text-checked and its first/last pages visually inspected. These earlier checks are not all reproduced by the smaller packaged smoke suite.

The owner reported v2.3.0 working on Windows. This is user-reported manual verification, not automated Windows GUI coverage.

## Still verify manually before submission

- [ ] Install and launch in a fresh Windows environment.
- [ ] Register, log in, reset password, log out, and reopen.
- [ ] Navigate rapidly, close during transitions, and check for background errors.
- [ ] Check normal laptop display scaling and both themes.
- [ ] Inspect reports with the names, languages, and long descriptions you intend to demonstrate.
- [ ] Check that sample-data screenshots contain no personal information.
- [ ] Verify the README installation commands from the final ZIP.

Passing these tests is evidence for specific behaviours, not a security audit or a claim of production readiness.
