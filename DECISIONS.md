# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code` (race-safe creates, lookups) and `department + joined_on` / `joined_on` (headcount, trend, summary). `attendance_logs`: unique `emp_code + date` (one record per day; race-safe punch-in, monthly, `$lookup`s), `emp_code + punch_in` (punch-out finds the latest punch-in), `date + emp_code` (list sort, month ranges for the leaderboard), `status + date + emp_code` (status-filtered list). I rejected an index on `late_minutes`: the leaderboard already narrows by date first.

2. **Punch-in race.** Both requests compute the same `(emp_code, date)` and call `insert_one` - there is no check-then-insert. MongoDB enforces the unique index atomically: one insert succeeds (201), the other raises `DuplicateKeyError`, which I return as 409.

3. **Ties.** `$setWindowFields` with `$rank` gives standard competition ranks (1, 2, 2, 4). I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned - the list can be longer than `limit`. Tied rows are ordered by `emp_code`.

4. **Headcount.** The pipeline starts from `employees` (`joined_on <= month end`) and `$lookup`s the month's logs, so an employee with zero logs still contributes 1 to headcount with zero totals.

5. **100x data.** Pre-aggregate daily per-department counters on each write (or nightly), so analytics read a few hundred summary rows instead of scanning millions of logs.
