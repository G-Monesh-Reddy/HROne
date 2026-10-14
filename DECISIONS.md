# DECISIONS.md

1. **Indexes.** Unique `employees.emp_code` enforces employee code uniqueness and fast lookups. Unique compound `attendance_logs(emp_code, date)` guarantees the natural key constraint, preventing concurrent duplicate punches. Compound `attendance_logs(date, emp_code)` powers paginated attendance listing and trend matching using index scans (`IXSCAN`). Compound `attendance_logs(emp_code, punch_in)` optimizes punch-out lookups. Rejected index: `(date, department)` on attendance logs, because department belongs to employees; indexing absent fields is redundant.

2. **Punch-in race.** Both concurrent requests validate the employee and compute identical attendance dates and late minutes. Both issue `insert_one`. The storage engine enforces the unique `(emp_code, date)` constraint: the first write succeeds (HTTP 201), while the second triggers `DuplicateKeyError`, which our exception handler catches to return HTTP 409, eliminating check-then-insert race windows.

3. **Ties.** The pipeline applies `$setWindowFields` using `$rank` (standard competition ranking: 1, 2, 2, 4) and filters `rank <= limit`. If employees tie at the cutoff, all tied rows are returned (potentially exceeding `limit`), ordered by late minutes descending then `emp_code` ascending, strictly adhering to competition ranking rules.

4. **Headcount.** The aggregation roots on `employees` (filtering `joined_on <= period_end`), joins logs via `$lookup`, and preserves employees with empty logs using `preserveNullAndEmptyArrays: True`. Headcount counts unique department employees (`$addToSet: "$emp_code"`, `$size`), while attendance totals treat missing logs as zero.

5. **100x data.** At ~10M attendance records, I would implement asynchronous event-driven materialized rollups via MongoDB change streams, storing daily department metrics in a dedicated summary collection to eliminate expensive cross-collection `$lookup` joins on read paths.
