CREATE INDEX retention_runs_cursor_idx ON retention_runs(created_at,id);
CREATE INDEX retention_runs_plan_cursor_idx ON retention_runs(plan_id,created_at,id);
