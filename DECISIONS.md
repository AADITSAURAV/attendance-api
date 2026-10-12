# DECISIONS.md

1. **Indexes.** Indexes make MongoDB find records fast without scanning the whole collection. The most important ones are the unique `emp_code` index on employees and the unique `emp_code + date` index on attendance_logs, which prevents duplicate daily attendance. I also added date-based indexes (`date + emp_code`, `status + date + emp_code`) for the attendance list and the month-range analytics, `emp_code + punch_in` so punch-out can find the latest punch-in quickly, and `department + joined_on` for headcount and the trend. I considered an index on `late_minutes` for the leaderboard and rejected it: the query already narrows by date first, so it would only slow down writes.

2. **Punch-in race.** If two identical punch-ins arrive at the same time, MongoDB's unique `(emp_code, date)` index allows only one insert to succeed. The other gets a duplicate-key error, which I return as 409, so we never create duplicate attendance records. I do not check first and then insert, because two requests could both pass the check.

3. **Ties.** The leaderboard ranks by total late minutes. If two employees are tied at the cutoff, I return everyone tied at that rank rather than arbitrarily dropping one, so the list can be longer than `limit`. Ranks follow standard competition ranking (1, 2, 2, 4) using `$rank`, and tied rows are ordered by `emp_code`.

4. **Headcount.** Headcount comes from the employees collection, not from attendance records. The pipeline starts from employees who joined by the end of the month and uses `$lookup` into attendance, so an employee with zero records is still counted, just with zero totals.

5. **100x data.** I would first review the aggregation pipelines and indexes with explain, then pre-aggregate / materialize analytics (for example daily attendance summaries per department) so we don't repeatedly scan huge attendance data on every request.
