CREATE TABLE users (
 id VARCHAR(36) PRIMARY KEY, username VARCHAR(64) NOT NULL UNIQUE,
 password_hash VARCHAR(100) NOT NULL, created_at TIMESTAMP(6) NOT NULL
);
CREATE TABLE sessions (
 token_hash VARCHAR(64) PRIMARY KEY, user_id VARCHAR(36) NOT NULL,
 expires_at TIMESTAMP(6) NOT NULL, FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX sessions_expiry_idx ON sessions(expires_at);
CREATE TABLE files (
 id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, name VARCHAR(255) NOT NULL,
 format VARCHAR(10) NOT NULL, size BIGINT NOT NULL, chunk_size INT NOT NULL, total_chunks INT NOT NULL,
 status VARCHAR(20) NOT NULL, object_key VARCHAR(512), created_at TIMESTAMP(6) NOT NULL,
 FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE INDEX files_owner_idx ON files(user_id,created_at);
CREATE TABLE rule_sets (
 id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, name VARCHAR(100) NOT NULL,
 current_version INT NOT NULL, created_at TIMESTAMP(6) NOT NULL,
 FOREIGN KEY (user_id) REFERENCES users(id)
);
CREATE TABLE rule_versions (
 rule_set_id VARCHAR(36) NOT NULL, version INT NOT NULL, name VARCHAR(100) NOT NULL,
 rules_json MEDIUMTEXT NOT NULL, created_at TIMESTAMP(6) NOT NULL,
 PRIMARY KEY (rule_set_id,version), FOREIGN KEY (rule_set_id) REFERENCES rule_sets(id)
);
CREATE TABLE tasks (
 id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, file_id VARCHAR(36) NOT NULL,
 rule_set_id VARCHAR(36) NOT NULL, rule_version INT NOT NULL, output_format VARCHAR(10) NOT NULL,
 status VARCHAR(20) NOT NULL, progress INT NOT NULL, attempt INT NOT NULL,
 error VARCHAR(2000), lease_token VARCHAR(36), lease_at TIMESTAMP(6),
 report_json MEDIUMTEXT, artifacts_json TEXT, created_at TIMESTAMP(6) NOT NULL, updated_at TIMESTAMP(6) NOT NULL,
 FOREIGN KEY (user_id) REFERENCES users(id), FOREIGN KEY (file_id) REFERENCES files(id),
 FOREIGN KEY (rule_set_id,rule_version) REFERENCES rule_versions(rule_set_id,version)
);
CREATE INDEX tasks_owner_idx ON tasks(user_id,created_at);
CREATE INDEX tasks_status_idx ON tasks(status,updated_at);
CREATE TABLE task_logs (
 id BIGINT AUTO_INCREMENT PRIMARY KEY, task_id VARCHAR(36) NOT NULL,
 level VARCHAR(10) NOT NULL, message VARCHAR(2000) NOT NULL, created_at TIMESTAMP(6) NOT NULL,
 FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX logs_task_idx ON task_logs(task_id,id);
CREATE TABLE outbox (
 id BIGINT AUTO_INCREMENT PRIMARY KEY, task_id VARCHAR(36) NOT NULL, attempt INT NOT NULL,
 payload MEDIUMTEXT NOT NULL, published BOOLEAN NOT NULL DEFAULT FALSE,
 created_at TIMESTAMP(6) NOT NULL, UNIQUE(task_id,attempt), FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX outbox_pending_idx ON outbox(published,id);
CREATE TABLE submissions (
 id BIGINT AUTO_INCREMENT PRIMARY KEY, user_id VARCHAR(36) NOT NULL, created_at TIMESTAMP(6) NOT NULL
);
CREATE INDEX submissions_rate_idx ON submissions(user_id,created_at);
CREATE TABLE platform_lock (id INT PRIMARY KEY);
INSERT INTO platform_lock(id) VALUES(1);
