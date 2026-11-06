CREATE TABLE retention_policy (
 id INT PRIMARY KEY, policy_json TEXT NOT NULL, protected_before TIMESTAMP(6) NOT NULL, updated_at TIMESTAMP(6) NOT NULL
);
INSERT INTO retention_policy(id,policy_json,protected_before,updated_at) VALUES(1,'{"enabled":false,"includeExisting":false,"rawDays":90,"resultDays":30,"dirtyDays":30,"reportDays":30,"failedDays":7,"logDays":90,"orphanDays":7,"graceHours":24}',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP);
CREATE TABLE retention_objects (
 object_key VARCHAR(512) PRIMARY KEY, kind VARCHAR(20) NOT NULL, file_id VARCHAR(36), task_id VARCHAR(36), attempt_no INT,
 size_bytes BIGINT NOT NULL, object_created_at TIMESTAMP(6) NOT NULL, discovered_at TIMESTAMP(6) NOT NULL,
 state VARCHAR(20) NOT NULL, deleted_at TIMESTAMP(6), last_error VARCHAR(1000)
);
CREATE INDEX retention_objects_state_idx ON retention_objects(state,object_created_at,object_key);
CREATE TABLE retention_plans (
 id VARCHAR(36) PRIMARY KEY, created_by VARCHAR(36) NOT NULL, policy_json TEXT NOT NULL, scope_json TEXT NOT NULL,
 state VARCHAR(20) NOT NULL, truncated BOOLEAN NOT NULL, created_at TIMESTAMP(6) NOT NULL, updated_at TIMESTAMP(6) NOT NULL
);
CREATE INDEX retention_plans_created_idx ON retention_plans(created_at,id);
CREATE TABLE retention_items (
 id VARCHAR(36) PRIMARY KEY, plan_id VARCHAR(36) NOT NULL, object_key VARCHAR(512), log_task_id VARCHAR(36),
 kind VARCHAR(20) NOT NULL, state VARCHAR(20) NOT NULL, reason VARCHAR(1000) NOT NULL, attempts INT NOT NULL DEFAULT 0,
 updated_at TIMESTAMP(6) NOT NULL, FOREIGN KEY(plan_id) REFERENCES retention_plans(id)
);
CREATE INDEX retention_items_plan_idx ON retention_items(plan_id,state,id);
CREATE TABLE retention_runs (
 id BIGINT AUTO_INCREMENT PRIMARY KEY, plan_id VARCHAR(36) NOT NULL, item_id VARCHAR(36) NOT NULL,
 action VARCHAR(20) NOT NULL, message VARCHAR(1000) NOT NULL, request_id VARCHAR(128), created_at TIMESTAMP(6) NOT NULL
);
CREATE INDEX retention_runs_page_idx ON retention_runs(id);
CREATE TABLE download_leases (
 id VARCHAR(36) PRIMARY KEY, object_key VARCHAR(512) NOT NULL, expires_at TIMESTAMP(6) NOT NULL,
 FOREIGN KEY(object_key) REFERENCES retention_objects(object_key)
);
CREATE INDEX download_leases_object_idx ON download_leases(object_key,expires_at);
