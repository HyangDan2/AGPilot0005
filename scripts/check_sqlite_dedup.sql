-- NG-05: Read-Only Operator Preflight SQL for Trajectory Feedback Dedup Integrity
-- Safe to run with `sqlite3 -readonly /var/lib/hd2lab-agcore/trajectory.db < scripts/check_sqlite_dedup.sql`
-- Outputs aggregate metadata/counts ONLY (no raw user/prompt data).

.headers on
.mode column

SELECT '1. Tables Present' AS section;
SELECT name, type FROM sqlite_master WHERE type IN ('table', 'index') AND name IN ('trajectory_events', 'incomplete_trajectories', 'uq_trajectory_feedback_dedup');

SELECT '2. UNIQUE Index Definition' AS section;
SELECT name, sql FROM sqlite_master WHERE type = 'index' AND name = 'uq_trajectory_feedback_dedup';

SELECT '3. Feedback NULL Dedup Key Count (expect 0)' AS section;
SELECT COUNT(*) AS null_dedup_key_feedback_rows
FROM trajectory_events
WHERE event_type = 'FeedbackEvent' AND feedback_dedup_key IS NULL;

SELECT '4. Logical Duplicate Feedback Groups (expect 0)' AS section;
SELECT COUNT(*) AS logical_duplicate_groups
FROM (
    SELECT run_id, COALESCE(owner_id, ''), COALESCE(decision_id, '__RUN__'), COUNT(*) AS cnt
    FROM trajectory_events
    WHERE event_type = 'FeedbackEvent'
    GROUP BY run_id, COALESCE(owner_id, ''), COALESCE(decision_id, '__RUN__')
    HAVING cnt > 1
);

SELECT '5. Incomplete Trajectories Count' AS section;
SELECT COUNT(*) AS incomplete_trajectory_count FROM incomplete_trajectories;
