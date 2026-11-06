ALTER TABLE tasks ADD COLUMN engine_version VARCHAR(50) NOT NULL DEFAULT 'v2-phased';

UPDATE tasks SET status='CANCELED' WHERE status='CANCELLED';

CREATE TABLE task_attempts (
 attempt_id VARCHAR(36) PRIMARY KEY,
 task_id VARCHAR(36) NOT NULL,
 attempt_no INT NOT NULL,
 engine_version VARCHAR(50) NOT NULL DEFAULT 'v2-phased',
 status VARCHAR(20) NOT NULL,
 progress INT NOT NULL DEFAULT 0,
 lease_token VARCHAR(36),
 lease_at TIMESTAMP(6),
 error VARCHAR(2000),
 report_json MEDIUMTEXT,
 artifacts_json TEXT,
 started_at TIMESTAMP(6),
 finished_at TIMESTAMP(6),
 created_at TIMESTAMP(6) NOT NULL,
 updated_at TIMESTAMP(6) NOT NULL,
 CONSTRAINT task_attempt_task_fk FOREIGN KEY (task_id) REFERENCES tasks(id),
 CONSTRAINT task_attempt_no_uq UNIQUE (task_id,attempt_no)
);
CREATE INDEX task_attempts_history_idx ON task_attempts(task_id,attempt_no);

INSERT INTO task_attempts(attempt_id,task_id,attempt_no,status,progress,lease_token,lease_at,error,report_json,artifacts_json,started_at,finished_at,created_at,updated_at)
SELECT UUID(),id,attempt,status,progress,lease_token,lease_at,error,report_json,artifacts_json,
 CASE WHEN status IN ('RUNNING','CANCELING') THEN lease_at ELSE NULL END,
 CASE WHEN status IN ('SUCCEEDED','FAILED','CANCELED') THEN updated_at ELSE NULL END,
 created_at,updated_at FROM tasks;

ALTER TABLE outbox ADD COLUMN attempt_id VARCHAR(36);
UPDATE outbox SET attempt_id=(SELECT a.attempt_id FROM task_attempts a WHERE a.task_id=outbox.task_id AND a.attempt_no=outbox.attempt);
CREATE UNIQUE INDEX outbox_attempt_id_uq ON outbox(attempt_id);
ALTER TABLE outbox ADD CONSTRAINT outbox_attempt_fk FOREIGN KEY (attempt_id) REFERENCES task_attempts(attempt_id);

ALTER TABLE task_logs ADD COLUMN attempt_id VARCHAR(36);
CREATE INDEX task_logs_attempt_idx ON task_logs(attempt_id,id);

CREATE TABLE task_idempotency (
 user_id VARCHAR(36) NOT NULL,
 key_hash CHAR(64) NOT NULL,
 request_hash CHAR(64) NOT NULL,
 task_id VARCHAR(36) NOT NULL,
 response_json MEDIUMTEXT NOT NULL,
 created_at TIMESTAMP(6) NOT NULL,
 expires_at TIMESTAMP(6) NOT NULL,
 PRIMARY KEY (user_id,key_hash),
 CONSTRAINT task_idempotency_user_fk FOREIGN KEY (user_id) REFERENCES users(id),
 CONSTRAINT task_idempotency_task_fk FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX task_idempotency_expiry_idx ON task_idempotency(expires_at);
