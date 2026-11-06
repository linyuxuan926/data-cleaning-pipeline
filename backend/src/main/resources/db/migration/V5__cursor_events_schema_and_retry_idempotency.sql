ALTER TABLE rule_versions ADD COLUMN schema_version INT NOT NULL DEFAULT 1;
ALTER TABLE rule_versions ADD COLUMN engine_version VARCHAR(50) NOT NULL DEFAULT 'v2-phased';
ALTER TABLE rule_versions ADD COLUMN logical_fields_json MEDIUMTEXT;
ALTER TABLE files ADD COLUMN parse_version INT NOT NULL DEFAULT 0;
UPDATE files SET parse_version=1 WHERE parse_status='PARSED';
ALTER TABLE tasks ADD COLUMN schema_version INT NOT NULL DEFAULT 1;
ALTER TABLE tasks ADD COLUMN logical_fields_snapshot MEDIUMTEXT;
ALTER TABLE tasks ADD COLUMN bindings_snapshot MEDIUMTEXT;
ALTER TABLE tasks ADD COLUMN output_fields_snapshot MEDIUMTEXT;
ALTER TABLE tasks ADD COLUMN automatic_retries INT NOT NULL DEFAULT 0;
ALTER TABLE task_attempts ADD COLUMN error_code VARCHAR(80);
ALTER TABLE task_attempts ADD COLUMN stage VARCHAR(80);
ALTER TABLE task_attempts ADD COLUMN stage_processed_rows BIGINT;
ALTER TABLE task_attempts ADD COLUMN total_rows BIGINT;
ALTER TABLE task_attempts ADD COLUMN last_event_seq BIGINT NOT NULL DEFAULT 0;

CREATE TABLE task_operation_keys (
 user_id VARCHAR(36) NOT NULL,
 operation VARCHAR(100) NOT NULL,
 key_hash CHAR(64) NOT NULL,
 request_hash CHAR(64) NOT NULL,
 task_id VARCHAR(36) NOT NULL,
 response_json MEDIUMTEXT NOT NULL,
 created_at TIMESTAMP(6) NOT NULL,
 expires_at TIMESTAMP(6) NOT NULL,
 PRIMARY KEY(user_id,operation,key_hash),
 FOREIGN KEY(user_id) REFERENCES users(id),
 FOREIGN KEY(task_id) REFERENCES tasks(id)
);
CREATE INDEX task_operation_expiry_idx ON task_operation_keys(expires_at);

CREATE TABLE task_events (
 event_id VARCHAR(36) PRIMARY KEY,
 task_id VARCHAR(36) NOT NULL,
 attempt_id VARCHAR(36) NOT NULL,
 event_seq BIGINT NOT NULL,
 payload_hash CHAR(64) NOT NULL,
 disposition VARCHAR(30) NOT NULL,
 created_at TIMESTAMP(6) NOT NULL,
 UNIQUE(attempt_id,event_seq),
 FOREIGN KEY(task_id) REFERENCES tasks(id),
 FOREIGN KEY(attempt_id) REFERENCES task_attempts(attempt_id)
);
CREATE INDEX task_event_history_idx ON task_events(attempt_id,created_at,event_id);
CREATE INDEX tasks_page_owner_idx ON tasks(user_id,created_at,id);
CREATE INDEX tasks_page_admin_idx ON tasks(created_at,id);
CREATE INDEX tasks_page_status_idx ON tasks(user_id,status,created_at,id);
CREATE INDEX files_page_owner_idx ON files(user_id,created_at,id);
CREATE INDEX rules_page_owner_idx ON rule_sets(user_id,created_at,id);
CREATE INDEX versions_page_idx ON rule_versions(rule_set_id,created_at,version);
CREATE INDEX attempts_page_idx ON task_attempts(task_id,created_at,attempt_id);
CREATE INDEX logs_page_idx ON task_logs(task_id,created_at,id);
CREATE INDEX logs_attempt_page_idx ON task_logs(attempt_id,created_at,id);
CREATE INDEX users_page_idx ON users(created_at,id);
