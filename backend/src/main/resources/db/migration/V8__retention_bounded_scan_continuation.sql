ALTER TABLE retention_plans ADD COLUMN scan_json TEXT;
CREATE INDEX retention_objects_file_key_idx ON retention_objects(file_id,object_key);
CREATE INDEX retention_objects_task_key_idx ON retention_objects(task_id,object_key);
