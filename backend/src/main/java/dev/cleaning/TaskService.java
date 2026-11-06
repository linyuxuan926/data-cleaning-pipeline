package dev.cleaning;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.transaction.annotation.Transactional;
import java.sql.Timestamp;
import java.time.Instant;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.*;
import static dev.cleaning.Database.*;

@Service
public class TaskService {
    private final Database db;private final Json json;private final FileService files;private final RuleService rules;private final ObjectStorage storage;private final SettingsService settings;private final AuthService auth;private final PythonGateway python;private final int executionTimeoutSeconds,cancelGraceSeconds;private final RetentionService retention;
    private static final Set<String> TERMINAL=Set.of("SUCCEEDED","FAILED","CANCELED","CANCELLED");
    @Value("${DISK_BATCH_ROWS:1000}") private int diskBatchRows;
    @Value("${MAX_SCRATCH_BYTES:1073741824}") private long maxScratchBytes;
    public TaskService(Database db,Json json,FileService files,RuleService rules,ObjectStorage storage,SettingsService settings,AuthService auth,PythonGateway python,RetentionService retention,@Value("${cleaning.execution-timeout-seconds:1800}") int executionTimeoutSeconds,@Value("${cleaning.cancel-grace-seconds:10}") int cancelGraceSeconds) {this.db=db;this.json=json;this.files=files;this.rules=rules;this.storage=storage;this.settings=settings;this.auth=auth;this.python=python;this.retention=retention;this.executionTimeoutSeconds=executionTimeoutSeconds;this.cancelGraceSeconds=cancelGraceSeconds;}
    public record Create(String fileId,String ruleSetId,Integer ruleVersion,String outputFormat,String engineVersion,Integer schemaVersion,JsonNode fieldBindings,Integer parseVersion) {
        public Create(String fileId,String ruleSetId,Integer ruleVersion,String outputFormat) {this(fileId,ruleSetId,ruleVersion,outputFormat,null,null,null,null);}
        public Create(String fileId,String ruleSetId,Integer ruleVersion,String outputFormat,String engineVersion) {this(fileId,ruleSetId,ruleVersion,outputFormat,engineVersion,null,null,null);}
    }
    public record Claim(Integer attempt,String attemptId) {public Claim(Integer attempt) {this(attempt,null);}}
    public record Retry(String expectedAttemptId) {}
    public record Event(Integer attempt,String leaseToken,String status,Integer progress,String message,JsonNode report,JsonNode artifacts,String attemptId,String eventId,Long eventSeq,String errorCode,String stage,Long stageProcessedRows,Long totalRows) {
        public Event(Integer attempt,String leaseToken,String status,Integer progress,String message,JsonNode report,JsonNode artifacts) {this(attempt,leaseToken,status,progress,message,report,artifacts,null,null,null,null,null,null,null);}
        public Event(Integer attempt,String leaseToken,String status,Integer progress,String message,JsonNode report,JsonNode artifacts,String attemptId) {this(attempt,leaseToken,status,progress,message,report,artifacts,attemptId,null,null,null,null,null,null);}
    }
    public record Created(Map<String,Object> body,boolean replayed) {}

    @Transactional
    public Map<String,Object> create(String user,Create input) {return createIdempotent(user,input,null).body();}

    @Transactional
    public Created createIdempotent(String user,Create input,String idempotencyKey) {
        if(input==null) throw ApiException.bad("请求体不能为空");
        if(input.fileId()==null||input.ruleSetId()==null||input.ruleVersion()==null||input.outputFormat()==null) throw ApiException.bad("fileId、ruleSetId、ruleVersion和outputFormat为必填项");
        FileService.format(input.outputFormat());
        String engine=input.engineVersion();
        if(engine!=null&&!Set.of("v2-phased","v3-sequential","v3-disk").contains(engine)) throw ApiException.bad("engineVersion无效");
        String keyHash=null,requestHash=null;
        // One lock protects both quota and key lookup/insert, including simultaneous identical POSTs.
        db.one("SELECT id FROM platform_lock WHERE id=1 FOR UPDATE");
        if(idempotencyKey!=null) {
            if(idempotencyKey.isBlank()||idempotencyKey.length()>200||!idempotencyKey.chars().allMatch(c->c>=33&&c<=126)) throw ApiException.bad("Idempotency-Key需为1到200位可见ASCII字符");
            keyHash=sha256(idempotencyKey);
            // Hash only the explicit request, never the mutable parse/settings snapshots.
            requestHash=sha256(json.write(createRequest(input)));
            var previous=db.jdbc.queryForList("SELECT * FROM task_idempotency WHERE user_id=? AND key_hash=? AND expires_at>?",user,keyHash,now());
            if(!previous.isEmpty()) {
                var row=previous.get(0);
                if(!requestHash.equals(str(row,"request_hash"))) throw ApiException.conflict("IDEMPOTENCY_KEY_REUSED: 同一个键不能用于不同请求");
                readable(user,str(row,"task_id"));
                Map<String,Object> response=new LinkedHashMap<>();json.read(row.get("response_json")).fields().forEachRemaining(e->response.put(e.getKey(),e.getValue().isNull()?null:e.getValue()));
                return new Created(response,true);
            }
            db.jdbc.update("DELETE FROM task_idempotency WHERE user_id=? AND key_hash=? AND expires_at<=?",user,keyHash,now());
        }
        limits(user);
        var file=files.owned(user,input.fileId(),false);
        if(!"READY".equals(str(file,"status"))||!"PARSED".equals(str(file,"parse_status"))) throw ApiException.conflict("文件必须上传并成功解析后才能执行");
        retention.assertAvailable(str(file,"object_key"),"INPUT_EXPIRED");
        if(input.parseVersion()!=null&&input.parseVersion()!=num(file,"parse_version")) throw ApiException.conflict("PARSE_VERSION_CHANGED: 文件已重解析，请刷新字段绑定");
        var rule=rules.version(user,input.ruleSetId(),input.ruleVersion());
        int schema=((Number)rule.get("schemaVersion")).intValue();
        if(engine==null) engine=schema==3?"v3-disk":"v3-sequential";
        if(input.schemaVersion()!=null&&input.schemaVersion()!=schema) throw ApiException.bad("schemaVersion与规则版本不一致");
        if((schema==3)!=engine.equals("v3-disk")) throw ApiException.bad("schema3使用v3-disk；旧规则须显式选择v2-phased/v3-sequential，或另存为schema3");
        Object frozenRules=rule.get("rules"),outputFields=null;
        if(schema==3) {
            var preflight=python.preflight(map("input",inputSnapshot(file),"schemaVersion",3,"engineVersion",engine,"logicalFields",rule.get("logicalFields"),"fieldBindings",input.fieldBindings(),"rules",frozenRules));
            if(preflight==null||!preflight.path("normalizedRules").isArray()||!preflight.path("outputFields").isArray()) throw new ApiException(HttpStatus.BAD_GATEWAY,"预检返回的规则/字段契约无效");
            frozenRules=preflight.get("normalizedRules");outputFields=preflight.get("outputFields");
        }
        String id=UUID.randomUUID().toString();var at=now();
        db.jdbc.update("INSERT INTO tasks(id,user_id,file_id,rule_set_id,rule_version,output_format,status,progress,attempt,engine_version,created_at,updated_at) VALUES(?,?,?,?,?,?,'QUEUED',0,1,?,?,?)",id,user,input.fileId(),input.ruleSetId(),input.ruleVersion(),input.outputFormat(),engine,at,at);
        var frozenLimits=settings.current().deepCopy();((com.fasterxml.jackson.databind.node.ObjectNode)frozenLimits).put("executionTimeoutSeconds",executionTimeoutSeconds).put("cancelGraceSeconds",cancelGraceSeconds);
        if(schema==3) ((com.fasterxml.jackson.databind.node.ObjectNode)frozenLimits).put("batchRows",diskBatchRows).put("maxScratchBytes",maxScratchBytes);
        db.jdbc.update("UPDATE tasks SET rules_snapshot=?,input_snapshot=?,limits_snapshot=?,schema_version=?,logical_fields_snapshot=?,bindings_snapshot=?,output_fields_snapshot=? WHERE id=?",json.write(frozenRules),json.write(inputSnapshot(file)),json.write(frozenLimits),schema,json.write(rule.get("logicalFields")),input.fieldBindings()==null?null:json.write(input.fieldBindings()),outputFields==null?null:json.write(outputFields),id);
        newAttempt(id);enqueue(id);log(id,"INFO","任务已排队，规则版本 "+input.ruleVersion());
        var result=detail(user,id);
        if(keyHash!=null) db.jdbc.update("INSERT INTO task_idempotency(user_id,key_hash,request_hash,task_id,response_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",user,keyHash,requestHash,id,json.write(result),at,Timestamp.from(Instant.now().plusSeconds(7*86400)));
        return new Created(result,false);
    }
    private String sha256(String text) {
        try {return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8)));}
        catch(java.security.NoSuchAlgorithmException e) {throw new IllegalStateException(e);}
    }
    private Map<String,Object> createRequest(Create input) {
        var request=map("fileId",input.fileId(),"ruleSetId",input.ruleSetId(),"ruleVersion",input.ruleVersion(),"outputFormat",input.outputFormat(),"engineVersion",input.engineVersion());
        if(input.schemaVersion()!=null) request.put("schemaVersion",input.schemaVersion());
        if(input.fieldBindings()!=null) {var sorted=new TreeMap<String,JsonNode>();input.fieldBindings().fields().forEachRemaining(e->sorted.put(e.getKey(),e.getValue()));request.put("fieldBindings",sorted);}
        if(input.parseVersion()!=null) request.put("parseVersion",input.parseVersion());return request;
    }
    public JsonNode preflight(String user,JsonNode request) {
        if(request==null||!request.isObject()||!request.path("fileId").isTextual()) throw ApiException.bad("fileId必填");
        for(var keys=request.fieldNames();keys.hasNext();) if(!Set.of("fileId","ruleSetId","ruleVersion","schemaVersion","engineVersion","logicalFields","rules","fieldBindings","parseVersion").contains(keys.next())) throw ApiException.bad("未知预检字段");
        var file=files.owned(user,request.path("fileId").asText(),false);
        if(!"PARSED".equals(str(file,"parse_status"))) throw ApiException.conflict("文件必须成功解析");
        retention.assertAvailable(str(file,"object_key"),"INPUT_EXPIRED");
        if(request.hasNonNull("parseVersion")&&request.path("parseVersion").asInt()!=num(file,"parse_version")) throw ApiException.conflict("PARSE_VERSION_CHANGED");
        Object ruleConfig=request.get("rules"),logical=request.get("logicalFields");int schema=request.path("schemaVersion").asInt(3);String engine=request.path("engineVersion").asText("v3-disk");
        if(request.hasNonNull("ruleSetId")) {var version=rules.version(user,request.path("ruleSetId").asText(),request.path("ruleVersion").asInt());ruleConfig=version.get("rules");logical=version.get("logicalFields");schema=((Number)version.get("schemaVersion")).intValue();}
        if(schema!=3||!"v3-disk".equals(engine)) throw ApiException.bad("预检只接受schema3/v3-disk；旧规则请另存转换");
        return python.preflight(map("input",inputSnapshot(file),"schemaVersion",schema,"engineVersion",engine,"logicalFields",logical,"fieldBindings",request.get("fieldBindings"),"rules",ruleConfig));
    }
    private void limits(String user) {
        db.one("SELECT id FROM platform_lock WHERE id=1 FOR UPDATE");var config=settings.current();
        if(db.count("SELECT COUNT(*) FROM tasks WHERE status IN ('QUEUED','RUNNING','CANCELING')")>=config.path("maxActiveGlobal").asInt()) throw new ApiException(HttpStatus.TOO_MANY_REQUESTS,"已达到平台最大活跃任务限制");
        if(db.count("SELECT COUNT(*) FROM tasks WHERE user_id=? AND status IN ('QUEUED','RUNNING','CANCELING')",user)>=config.path("maxActivePerUser").asInt()) throw new ApiException(HttpStatus.TOO_MANY_REQUESTS,"已达到每位用户最大活跃任务限制");
        if(db.count("SELECT COUNT(*) FROM submissions WHERE user_id=? AND created_at>?",user,Timestamp.from(Instant.now().minusSeconds(60)))>=config.path("submissionsPerMinute").asInt()) throw new ApiException(HttpStatus.TOO_MANY_REQUESTS,"已达到每分钟提交限制");
        db.jdbc.update("INSERT INTO submissions(user_id,created_at) VALUES(?,?)",user,now());
    }
    private Map<String,Object> currentAttempt(Map<String,Object> task) {return db.one("SELECT * FROM task_attempts WHERE task_id=? AND attempt_no=?",str(task,"id"),num(task,"attempt"));}
    private void newAttempt(String id) {
        var task=db.one("SELECT * FROM tasks WHERE id=?",id);var at=now();
        db.jdbc.update("INSERT INTO task_attempts(attempt_id,task_id,attempt_no,engine_version,status,progress,created_at,updated_at) VALUES(?,?,?,?,'QUEUED',0,?,?)",UUID.randomUUID().toString(),id,num(task,"attempt"),str(task,"engine_version"),at,at);
    }
    // Only the current nonterminal attempt is mirrored; old rows are never reset on retry.
    private void syncAttempt(String id) {
        var task=db.one("SELECT * FROM tasks WHERE id=?",id);var a=currentAttempt(task);
        if(TERMINAL.contains(str(a,"status"))) return;
        String status=str(task,"status");var at=now();
        Object started=a.get("started_at");if(started==null&&"RUNNING".equals(status)) started=at;
        db.jdbc.update("UPDATE task_attempts SET status=?,progress=?,lease_token=?,lease_at=?,error=?,report_json=?,artifacts_json=?,started_at=?,finished_at=?,updated_at=? WHERE attempt_id=?",status,num(task,"progress"),task.get("lease_token"),task.get("lease_at"),task.get("error"),task.get("report_json"),task.get("artifacts_json"),started,TERMINAL.contains(status)?at:null,at,str(a,"attempt_id"));
        String error=str(task,"error");if("FAILED".equals(status)&&error!=null&&error.matches("^[A-Z][A-Z0-9_]{0,79}:.*")) db.jdbc.update("UPDATE task_attempts SET error_code=? WHERE attempt_id=?",error.substring(0,error.indexOf(':')),str(a,"attempt_id"));
    }
    private void enqueue(String id) {
        var task=db.one("SELECT * FROM tasks WHERE id=?",id);
        if(task.get("input_snapshot")==null||task.get("rules_snapshot")==null||task.get("limits_snapshot")==null) {
            var file=db.one("SELECT * FROM files WHERE id=?",str(task,"file_id"));
            var rule=db.one("SELECT rules_json FROM rule_versions WHERE rule_set_id=? AND version=?",str(task,"rule_set_id"),num(task,"rule_version"));
            db.jdbc.update("UPDATE tasks SET rules_snapshot=?,input_snapshot=?,limits_snapshot=? WHERE id=?",str(rule,"rules_json"),json.write(inputSnapshot(file)),json.write(settings.current()),id);task=db.one("SELECT * FROM tasks WHERE id=?",id);
        }
        var attempt=currentAttempt(task);
        var body=map("taskId",id,"attempt",num(task,"attempt"),"attemptId",str(attempt,"attempt_id"),"engineVersion",str(task,"engine_version"),"input",json.read(task.get("input_snapshot")),"rules",json.read(task.get("rules_snapshot")),"limits",json.read(task.get("limits_snapshot")),"outputFormat",str(task,"output_format"));
        if(num(task,"schema_version")==3) {body.put("schemaVersion",3);body.put("logicalFields",json.read(task.get("logical_fields_snapshot")));body.put("fieldBindings",json.read(task.get("bindings_snapshot")));}
        body.put("requestId",RequestContext.id());
        String payload=json.write(body);if(payload.getBytes(StandardCharsets.UTF_8).length>128*1024) throw ApiException.bad("任务快照过大，请减少规则或字段配置");
        db.jdbc.update("INSERT INTO outbox(task_id,attempt,attempt_id,payload,published,created_at) VALUES(?,?,?,?,FALSE,?)",id,num(task,"attempt"),str(attempt,"attempt_id"),payload,now());
    }
    private Map<String,Object> inputSnapshot(Map<String,Object> file) {return map("fileId",str(file,"id"),"objectKey",str(file,"object_key"),"format",str(file,"format"),"name",str(file,"name"),"parseVersion",num(file,"parse_version"),"parseOptions",file.get("parse_options_json")==null?json.read("{}"):json.read(file.get("parse_options_json")),"fields",file.get("fields_json")==null?null:json.read(file.get("fields_json")));}
    Map<String,Object> owned(String user,String id,boolean lock) {return db.one("SELECT t.*,f.name AS file_name FROM tasks t JOIN files f ON f.id=t.file_id WHERE t.id=? AND t.user_id=?"+(lock?" FOR UPDATE":""),id,user);}
    private Map<String,Object> readable(String user,String id) {return auth.admin(user)?db.one("SELECT t.*,f.name AS file_name FROM tasks t JOIN files f ON f.id=t.file_id WHERE t.id=?",id):owned(user,id,false);}
    public Map<String,Object> detail(String user,String id) {
        var task=readable(user,id);var result=view(task);
        for(String field:List.of("rules","input","limits")) result.put(field+"Snapshot",task.get(field+"_snapshot")==null?null:json.read(task.get(field+"_snapshot")));
        for(String field:List.of("logical_fields","bindings","output_fields")) result.put(switch(field) {case "logical_fields" -> "logicalFieldsSnapshot";case "output_fields" -> "outputFieldsSnapshot";default -> "fieldBindingsSnapshot";},task.get(field+"_snapshot")==null?null:json.read(task.get(field+"_snapshot")));
        result.put("artifactStatus",artifactStatus(task.get("artifacts_json")));
        return result;
    }
    public List<Map<String,Object>> all() {return db.jdbc.queryForList("SELECT t.*,f.name AS file_name,u.username FROM tasks t JOIN files f ON f.id=t.file_id JOIN users u ON u.id=t.user_id ORDER BY t.created_at DESC LIMIT 500").stream().map(t->{var item=view(t);item.put("username",str(t,"username"));item.put("userId",str(t,"user_id"));return item;}).toList();}
    public List<Map<String,Object>> list(String user) {return db.jdbc.queryForList("SELECT t.*,f.name AS file_name FROM tasks t JOIN files f ON f.id=t.file_id WHERE t.user_id=? ORDER BY t.created_at DESC LIMIT 200",user).stream().map(this::view).toList();}
    Map<String,Object> view(Map<String,Object> t) {
        var attempt=currentAttempt(t);String attemptId=str(attempt,"attempt_id");
        return map("id",str(t,"id"),"ownerId",str(t,"user_id"),"username",str(t,"username"),"fileId",str(t,"file_id"),"fileName",str(t,"file_name"),"ruleSetId",str(t,"rule_set_id"),"ruleVersion",num(t,"rule_version"),"outputFormat",str(t,"output_format"),"engineVersion",str(t,"engine_version"),"schemaVersion",num(t,"schema_version"),"status",normalized(str(t,"status")),"progress",num(t,"progress"),"attempt",num(t,"attempt"),"attemptId",attemptId,"currentAttemptId",attemptId,"error",str(t,"error"),"errorCode",str(attempt,"error_code"),"stage",str(attempt,"stage"),"stageProcessedRows",attempt.get("stage_processed_rows"),"totalRows",attempt.get("total_rows"),"createdAt",time(t,"created_at"),"updatedAt",time(t,"updated_at"));
    }
    @Transactional
    public Map<String,Object> cancel(String user,String id) {
        var task=owned(user,id,true);String status=normalized(str(task,"status"));
        if(Set.of("CANCELING","CANCELED").contains(status)) return detail(user,id);
        if(TERMINAL.contains(status)) throw ApiException.conflict("任务已经结束");
        String next="QUEUED".equals(status)?"CANCELED":"CANCELING";
        db.jdbc.update("UPDATE tasks SET status=?,updated_at=? WHERE id=?",next,now(),id);syncAttempt(id);
        log(id,"WARN","CANCELING".equals(next)?"用户请求取消，等待Worker确认执行已停止":"用户取消排队任务");return detail(user,id);
    }
    @Transactional
    public Map<String,Object> retry(String user,String id) {
        return retryIdempotent(user,id,null).body();
    }
    @Transactional
    public Created retryIdempotent(String user,String id,String key) {
        return retryIdempotent(user,id,key,null);
    }
    @Transactional
    public Created retryIdempotent(String user,String id,String key,Retry input) {
        db.one("SELECT id FROM platform_lock WHERE id=1 FOR UPDATE");var task=owned(user,id,true);
        String operation="retry:"+id,keyHash=null,requestHash=sha256(json.write(input==null?new Retry(null):input));
        if(key!=null) {
            validateKey(key);keyHash=sha256(key);
            var previous=db.jdbc.queryForList("SELECT * FROM task_operation_keys WHERE user_id=? AND operation=? AND key_hash=? AND expires_at>?",user,operation,keyHash,now());
            if(!previous.isEmpty()) {var row=previous.get(0);if(!requestHash.equals(str(row,"request_hash"))) throw ApiException.conflict("IDEMPOTENCY_KEY_REUSED");return new Created(response(row.get("response_json")),true);}
            db.jdbc.update("DELETE FROM task_operation_keys WHERE user_id=? AND operation=? AND key_hash=? AND expires_at<=?",user,operation,keyHash,now());
        }
        if(input!=null&&input.expectedAttemptId()!=null&&!input.expectedAttemptId().equals(str(currentAttempt(task),"attempt_id"))) throw ApiException.conflict("ATTEMPT_CHANGED: 当前执行尝试已变化");
        if(!Set.of("FAILED","CANCELED","CANCELLED").contains(str(task,"status"))) throw ApiException.conflict("仅失败或取消的任务可重试");
        String rawKey=task.get("input_snapshot")==null?str(files.owned(user,str(task,"file_id"),false),"object_key"):json.read(task.get("input_snapshot")).path("objectKey").asText();
        retention.assertAvailable(rawKey,"INPUT_EXPIRED");
        limits(user);resetForNewAttempt(id);enqueue(id);log(id,"INFO","用户重试任务，执行尝试 "+(num(task,"attempt")+1));var result=detail(user,id);
        if(keyHash!=null) db.jdbc.update("INSERT INTO task_operation_keys(user_id,operation,key_hash,request_hash,task_id,response_json,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?)",user,operation,keyHash,requestHash,id,json.write(result),now(),Timestamp.from(Instant.now().plusSeconds(7*86400)));
        return new Created(result,false);
    }
    private void validateKey(String key) {if(key.isBlank()||key.length()>200||!key.chars().allMatch(c->c>=33&&c<=126)) throw ApiException.bad("Idempotency-Key需为1到200位可见ASCII字符");}
    private Map<String,Object> response(Object encoded) {Map<String,Object> result=new LinkedHashMap<>();json.read(encoded).fields().forEachRemaining(e->result.put(e.getKey(),e.getValue().isNull()?null:e.getValue()));return result;}
    private void resetForNewAttempt(String id) {
        db.jdbc.update("UPDATE tasks SET status='QUEUED',progress=0,attempt=attempt+1,error=NULL,lease_token=NULL,lease_at=NULL,report_json=NULL,artifacts_json=NULL,updated_at=? WHERE id=?",now(),id);newAttempt(id);
    }
    public List<Map<String,Object>> attempts(String user,String id) {readable(user,id);return db.jdbc.queryForList("SELECT * FROM task_attempts WHERE task_id=? ORDER BY attempt_no DESC",id).stream().map(this::attemptView).toList();}
    private Map<String,Object> readableAttempt(String user,String id,String attemptId) {readable(user,id);return db.one("SELECT * FROM task_attempts WHERE task_id=? AND attempt_id=?",id,attemptId);}
    public Map<String,Object> attemptDetail(String user,String id,String attemptId) {
        var row=readableAttempt(user,id,attemptId);var result=attemptView(row);result.put("report",row.get("report_json")==null?null:json.read(row.get("report_json")));result.put("artifacts",row.get("artifacts_json")==null?null:json.read(row.get("artifacts_json")));result.put("artifactStatus",artifactStatus(row.get("artifacts_json")));return result;
    }
    private Map<String,String> artifactStatus(Object artifacts) {Map<String,String> result=new LinkedHashMap<>();if(artifacts!=null) {var values=json.read(artifacts);for(String kind:List.of("result","dirty","report")) if(values.path(kind).isTextual()) result.put(kind,retention.status(values.path(kind).asText()));}return result;}
    public Map<String,Object> attempt(String user,String id,String attemptId) {return attemptDetail(user,id,attemptId);}
    Map<String,Object> attemptView(Map<String,Object> a) {return map("attemptId",str(a,"attempt_id"),"taskId",str(a,"task_id"),"attempt",num(a,"attempt_no"),"attemptNo",num(a,"attempt_no"),"engineVersion",str(a,"engine_version"),"status",normalized(str(a,"status")),"progress",num(a,"progress"),"error",str(a,"error"),"errorCode",str(a,"error_code"),"stage",str(a,"stage"),"stageProcessedRows",a.get("stage_processed_rows"),"totalRows",a.get("total_rows"),"lastEventSeq",a.get("last_event_seq"),"leaseAt",nullableTime(a,"lease_at"),"startedAt",nullableTime(a,"started_at"),"finishedAt",nullableTime(a,"finished_at"),"createdAt",time(a,"created_at"),"updatedAt",time(a,"updated_at"));}
    private String nullableTime(Map<String,Object> row,String key) {return row.get(key)==null?null:time(row,key);}
    public List<Map<String,Object>> logs(String user,String id,long after) {readable(user,id);return readLogs(id,null,after);}
    public List<Map<String,Object>> attemptLogs(String user,String id,String attemptId,long after) {readableAttempt(user,id,attemptId);return readLogs(id,attemptId,after);}
    private List<Map<String,Object>> readLogs(String id,String attemptId,long after) {
        if(after<0) throw ApiException.bad("after不能为负数");
        var rows=attemptId==null?db.jdbc.queryForList("SELECT * FROM task_logs WHERE task_id=? AND id>? ORDER BY id LIMIT 500",id,after):db.jdbc.queryForList("SELECT * FROM task_logs WHERE task_id=? AND attempt_id=? AND id>? ORDER BY id LIMIT 500",id,attemptId,after);
        return rows.stream().map(r->map("id",r.get("id"),"attemptId",str(r,"attempt_id"),"requestId",str(r,"request_id"),"level",str(r,"level"),"message",str(r,"message"),"createdAt",time(r,"created_at"))).toList();
    }
    public JsonNode report(String user,String id) {var task=readable(user,id);return reportRow(currentAttempt(task));}
    public JsonNode attemptReport(String user,String id,String attemptId) {return reportRow(readableAttempt(user,id,attemptId));}
    private JsonNode reportRow(Map<String,Object> row) {if(!"SUCCEEDED".equals(str(row,"status"))) throw ApiException.conflict("任务尚未成功完成");return json.read(row.get("report_json"));}
    public Map<String,String> artifact(String user,String id,String kind) {var task=readable(user,id);return artifactRow(id,currentAttempt(task),str(task,"output_format"),kind);}
    public Map<String,String> attemptArtifact(String user,String id,String attemptId,String kind) {var task=readable(user,id);return artifactRow(id,readableAttempt(user,id,attemptId),str(task,"output_format"),kind);}
    private Map<String,String> artifactRow(String id,Map<String,Object> row,String format,String kind) {
        if(!Set.of("result","dirty","report").contains(kind)) throw ApiException.missing();
        if(!"SUCCEEDED".equals(str(row,"status"))) throw ApiException.conflict("任务尚未成功完成");
        String key=json.read(row.get("artifacts_json")).path(kind).asText();retention.assertAvailable(key,"ARTIFACT_EXPIRED");
        String ext="result".equals(kind)?format:"dirty".equals(kind)?"csv":"json";return Map.of("key",key,"name",kind+"-"+id+"-"+num(row,"attempt_no")+"."+ext);
    }
    private boolean matches(Map<String,Object> task,Integer number,String attemptId) {
        if(number!=null&&num(task,"attempt")!=number) return false;
        if(attemptId!=null) return attemptId.equals(str(currentAttempt(task),"attempt_id"));
        if(!"v2-phased".equals(str(task,"engine_version"))) throw ApiException.bad("attemptId为V3执行的必填项");
        if(number==null) throw ApiException.bad("attempt必填");return true;
    }
    @Transactional
    public Map<String,Object> claim(String id,Claim input) {
        if(input==null) throw ApiException.bad("请求体不能为空");db.one("SELECT id FROM platform_lock WHERE id=1 FOR UPDATE");var task=db.one("SELECT * FROM tasks WHERE id=? FOR UPDATE",id);
        if(!matches(task,input.attempt(),input.attemptId())||TERMINAL.contains(str(task,"status"))||"CANCELING".equals(str(task,"status"))) return map("execute",false);
        if("RUNNING".equals(str(task,"status"))) throw ApiException.conflict("任务已被Worker领取；请等待租约释放");
        var config=settings.current();
        if(db.count("SELECT COUNT(*) FROM tasks WHERE status IN ('RUNNING','CANCELING')")>=config.path("maxRunningGlobal").asInt()||db.count("SELECT COUNT(*) FROM tasks WHERE user_id=? AND status IN ('RUNNING','CANCELING')",str(task,"user_id"))>=config.path("maxRunningPerUser").asInt()) throw ApiException.conflict("RUNNING_CAPACITY: 暂无可用执行槽，请延迟重新投递");
        String token=UUID.randomUUID().toString();var at=now();db.jdbc.update("UPDATE tasks SET status='RUNNING',progress=1,lease_token=?,lease_at=?,updated_at=? WHERE id=?",token,at,at,id);syncAttempt(id);
        var limit=json.read(task.get("limits_snapshot"));
        log(id,"INFO","Worker领取任务，执行尝试 "+num(task,"attempt"));return map("execute",true,"leaseToken",token,"attempt",num(task,"attempt"),"attemptId",str(currentAttempt(task),"attempt_id"),"executionTimeoutSeconds",limit.path("executionTimeoutSeconds").asInt(executionTimeoutSeconds),"cancelGraceSeconds",limit.path("cancelGraceSeconds").asInt(cancelGraceSeconds),"leaseSeconds",120);
    }
    public Map<String,Object> control(String id,int attempt,String leaseToken) {return control(id,attempt,null,leaseToken);}
    public Map<String,Object> control(String id,Integer attempt,String attemptId,String leaseToken) {
        var task=db.one("SELECT * FROM tasks WHERE id=?",id);boolean cancelled=!matches(task,attempt,attemptId)||!Objects.equals(str(task,"lease_token"),leaseToken)||!"RUNNING".equals(str(task,"status"));
        return map("cancelled",cancelled,"status",normalized(str(task,"status")),"attemptId",str(currentAttempt(task),"attempt_id"));
    }
    @Transactional
    public void event(String id,Event event) {
        if(event==null) throw ApiException.bad("请求体不能为空");String status=normalized(event.status());
        if(status==null||!Set.of("RUNNING","SUCCEEDED","FAILED","CANCELED").contains(status)) throw ApiException.bad("回调状态无效");
        if(event.progress()==null||event.progress()<0||event.progress()>100) throw ApiException.bad("progress需为0-100");
        var task=db.one("SELECT * FROM tasks WHERE id=? FOR UPDATE",id);
        boolean ledger=event.eventId()!=null||event.eventSeq()!=null;
        if("v3-disk".equals(str(task,"engine_version"))&&!ledger) throw ApiException.bad("v3-disk事件必须包含eventId和eventSeq");
        if(ledger) {
            try {UUID.fromString(event.eventId());UUID.fromString(event.attemptId());} catch(Exception e) {throw ApiException.bad("eventId与attemptId须为UUID");}
            if(event.eventSeq()==null||event.eventSeq()<1) throw ApiException.bad("eventSeq须为正整数");
            var duplicates=db.jdbc.queryForList("SELECT * FROM task_events WHERE event_id=? OR (attempt_id=? AND event_seq=?)",event.eventId(),event.attemptId(),event.eventSeq());
            String hash=eventHash(event);
            if(!duplicates.isEmpty()) {
                for(var duplicate:duplicates) if(!event.eventId().equals(str(duplicate,"event_id"))||!hash.equals(str(duplicate,"payload_hash"))) throw ApiException.conflict("EVENT_PAYLOAD_CONFLICT: eventId/seq不能复用于不同事件");
                return;
            }
        }
        if(!matches(task,event.attempt(),event.attemptId())) {if(ledger&&db.count("SELECT COUNT(*) FROM task_attempts WHERE task_id=? AND attempt_id=?",id,event.attemptId())>0) recordEvent(id,event,"STALE_ATTEMPT");return;}
        if(TERMINAL.contains(str(task,"status"))) {if(ledger) recordEvent(id,event,"TERMINAL_IGNORED");return;}
        if(event.leaseToken()==null||!Objects.equals(str(task,"lease_token"),event.leaseToken())||!Set.of("RUNNING","CANCELING").contains(str(task,"status"))) throw ApiException.conflict("Worker租约无效");
        var attempt=currentAttempt(task);
        if(ledger&&event.eventSeq()<=((Number)attempt.get("last_event_seq")).longValue()) {recordEvent(id,event,"OUT_OF_ORDER");return;}
        // Once cancellation wins the task lock, late successes and heartbeats cannot undo it.
        if("CANCELING".equals(str(task,"status"))&&Set.of("RUNNING","SUCCEEDED").contains(status)) {if(ledger) recordEvent(id,event,"CANCELING_IGNORED");return;}
        if("CANCELED".equals(status)&&!"CANCELING".equals(str(task,"status"))) throw ApiException.conflict("任务未请求取消");
        if(event.errorCode()!=null&&!event.errorCode().matches("[A-Z][A-Z0-9_]{0,79}")) throw ApiException.bad("errorCode无效");
        if(event.stage()!=null&&!event.stage().matches("[A-Za-z0-9_.:-]{1,80}")) throw ApiException.bad("stage无效");
        if(event.stageProcessedRows()!=null&&event.stageProcessedRows()<0||event.totalRows()!=null&&event.totalRows()<0) throw ApiException.bad("阶段行数不能为负数");
        if(event.stageProcessedRows()!=null&&event.totalRows()!=null&&event.stageProcessedRows()>event.totalRows()) throw ApiException.bad("阶段处理行数不能超过总数");
        String message=event.message()==null?"":event.message();if(message.length()>2000) message=message.substring(0,2000);
        String errorCode=event.errorCode();boolean deadlineExceeded=false;
        // Old frozen snapshots without a deadline retain their historical behavior.
        var limits=json.read(task.get("limits_snapshot"));
        if("SUCCEEDED".equals(status)&&limits.path("executionTimeoutSeconds").isIntegralNumber()&&attempt.get("started_at") instanceof Timestamp started&&Instant.now().isAfter(started.toInstant().plusSeconds(limits.path("executionTimeoutSeconds").asLong()))) {
            deadlineExceeded=true;status="FAILED";errorCode="TASK_TIMEOUT";message="TASK_TIMEOUT: 任务终态未在执行截止时间前被平台接受，迟到产物已拒收";
        }
        if("SUCCEEDED".equals(status)) {
            validateResult(id,task,event);db.jdbc.update("UPDATE tasks SET status='SUCCEEDED',progress=100,report_json=?,artifacts_json=?,error=NULL,lease_at=?,updated_at=? WHERE id=?",json.write(event.report()),json.write(event.artifacts()),now(),now(),id);
        } else if("RUNNING".equals(status)) {
            db.jdbc.update("UPDATE tasks SET progress=?,lease_at=?,updated_at=? WHERE id=?",Math.max(num(task,"progress"),Math.min(event.progress(),99)),now(),now(),id);
        } else {db.jdbc.update("UPDATE tasks SET status=?,error=?,lease_at=?,updated_at=? WHERE id=?",status,"FAILED".equals(status)?message:null,now(),now(),id);}
        syncAttempt(id);
        String stage=event.stage()==null?str(attempt,"stage"):event.stage();boolean sameStage=Objects.equals(stage,str(attempt,"stage"));
        Long processed=event.stageProcessedRows();if(sameStage&&attempt.get("stage_processed_rows") instanceof Number old) processed=processed==null?old.longValue():Math.max(old.longValue(),processed);
        Object total=event.totalRows()!=null?event.totalRows():sameStage?attempt.get("total_rows"):null;
        db.jdbc.update("UPDATE task_attempts SET error_code=COALESCE(?,error_code),stage=?,stage_processed_rows=?,total_rows=?,last_event_seq=? WHERE attempt_id=?",errorCode,stage,processed,total,ledger?event.eventSeq():attempt.get("last_event_seq"),str(attempt,"attempt_id"));
        if(ledger) recordEvent(id,event,deadlineExceeded?"DEADLINE_EXCEEDED":"APPLIED");if(!message.isBlank()) log(id,"FAILED".equals(status)?"ERROR":"INFO",message);
    }
    private void recordEvent(String id,Event event,String disposition) {db.jdbc.update("INSERT INTO task_events(event_id,task_id,attempt_id,event_seq,payload_hash,disposition,created_at) VALUES(?,?,?,?,?,?,?)",event.eventId(),id,event.attemptId(),event.eventSeq(),eventHash(event),disposition,now());}
    private String eventHash(Event event) {return sha256(json.write(canonical(json.read(json.write(event)))));}
    private Object canonical(JsonNode node) {if(node.isObject()) {var result=new TreeMap<String,Object>();node.fields().forEachRemaining(e->result.put(e.getKey(),canonical(e.getValue())));return result;}if(node.isArray()) {var result=new ArrayList<Object>();node.forEach(n->result.add(canonical(n)));return result;}return node;}
    private String normalized(String status) {return "CANCELLED".equals(status)?"CANCELED":status;}
    private void validateResult(String id,Map<String,Object> task,Event event) {
        JsonNode report=event.report(),artifacts=event.artifacts();
        if(report==null||!report.isObject()||artifacts==null||!artifacts.isObject()||json.write(report).length()>2_000_000) throw ApiException.bad("缺少报告或产物元数据，或报告过大");
        if(!id.equals(report.path("taskId").asText())||report.path("attempt").asInt()!=num(task,"attempt")) throw ApiException.bad("报告执行标识不一致");
        if(!"v2-phased".equals(str(task,"engine_version"))&&(!report.hasNonNull("attemptId")||!report.hasNonNull("engineVersion"))) throw ApiException.bad("V3报告必须包含attemptId和engineVersion");
        if(report.has("attemptId")&&!Objects.equals(event.attemptId(),report.path("attemptId").asText())) throw ApiException.bad("报告attemptId不一致");
        if(report.has("engineVersion")&&!Objects.equals(str(task,"engine_version"),report.path("engineVersion").asText())) throw ApiException.bad("报告engineVersion不一致");
        for(String field:List.of("inputRows","outputRows","dirtyRows","changedCells")) if(!report.path(field).isIntegralNumber()||!report.path(field).canConvertToLong()||report.path(field).asLong()<0) throw ApiException.bad("报告计数无效: "+field);
        for(String field:List.of("removedRows","flaggedRows","issueCount")) if(report.has(field)&&(!report.path(field).isIntegralNumber()||!report.path(field).canConvertToLong()||report.path(field).asLong()<0)) throw ApiException.bad("报告计数无效: "+field);
        String removed=report.has("removedRows")?"removedRows":"dirtyRows";
        if(report.path("inputRows").asLong()!=report.path("outputRows").asLong()+report.path(removed).asLong()) throw ApiException.bad("报告行数不守恒");
        for(String field:List.of("validOutputRows","flaggedOutputRows","finalRemovedRows","duplicateRemovedRows","outlierHandledRows","missingFilledCells","typeConversionFailedCells","issueRows")) {
            if(report.has(field)&&(!report.path(field).isIntegralNumber()||!report.path(field).canConvertToLong()||report.path(field).asLong()<0)) throw ApiException.bad("报告计数无效: "+field);
        }
        if(report.has("finalRemovedRows")&&report.path("finalRemovedRows").asLong()!=report.path(removed).asLong()) throw ApiException.bad("最终删除计数不一致");
        if(report.has("validOutputRows")&&report.has("flaggedOutputRows")&&report.path("outputRows").asLong()!=report.path("validOutputRows").asLong()+report.path("flaggedOutputRows").asLong()) throw ApiException.bad("有效与标记输出计数不守恒");
        if(report.has("finalRemovalByRule")) {
            if(!report.path("finalRemovalByRule").isObject()) throw ApiException.bad("最终删除归因格式错误");
            long sum=0;var counts=report.path("finalRemovalByRule").elements();
            while(counts.hasNext()) {var count=counts.next();if(!count.isIntegralNumber()||!count.canConvertToLong()||count.asLong()<0||sum>report.path("inputRows").asLong()-count.asLong()) throw ApiException.bad("最终删除归因计数无效");sum+=count.asLong();}
            if(sum!=report.path(removed).asLong()) throw ApiException.bad("最终删除归因不守恒");
        }
        String base=id+"/"+num(task,"attempt")+"/";
        if(!("results/"+base+"result."+str(task,"output_format")).equals(artifacts.path("result").asText())||!("dirty/"+base+"dirty.csv").equals(artifacts.path("dirty").asText())||!("reports/"+base+"report.json").equals(artifacts.path("report").asText())) throw ApiException.bad("产物路径与任务不一致");
        for(String kind:List.of("result","dirty","report")) storage.verify(artifacts.path(kind).asText());
    }
    void log(String id,String level,String message) {var task=db.one("SELECT * FROM tasks WHERE id=?",id);db.jdbc.update("INSERT INTO task_logs(task_id,attempt_id,request_id,level,message,created_at) VALUES(?,?,?,?,?,?)",id,str(currentAttempt(task),"attempt_id"),RequestContext.id(),level,message,now());}
    @Transactional
    public void recover() {
        db.one("SELECT id FROM platform_lock WHERE id=1 FOR UPDATE");var expiredAt=Timestamp.from(Instant.now().minusSeconds(120));
        var canceled=db.jdbc.queryForList("SELECT * FROM tasks WHERE status='CANCELING' AND lease_at<? AND updated_at<? FOR UPDATE",expiredAt,Timestamp.from(Instant.now().minusSeconds(30)));
        for(var task:canceled) {String id=str(task,"id");db.jdbc.update("UPDATE tasks SET status='FAILED',error=?,updated_at=? WHERE id=?","CANCEL_UNCONFIRMED: 无法确认Worker执行已停止，旧租约已失效",now(),id);syncAttempt(id);log(id,"ERROR","取消停止未确认；已隔离旧尝试，不能视为取消成功");}
        var stale=db.jdbc.queryForList("SELECT * FROM tasks WHERE status='RUNNING' AND lease_at<? FOR UPDATE",expiredAt);
        for(var task:stale) {
            String id=str(task,"id");var attempt=currentAttempt(task);var frozen=task.get("limits_snapshot")==null?null:json.read(task.get("limits_snapshot"));
            boolean timedOut=frozen!=null&&frozen.path("executionTimeoutSeconds").isIntegralNumber()&&attempt.get("started_at") instanceof Timestamp started&&!Instant.now().isBefore(started.toInstant().plusSeconds(frozen.path("executionTimeoutSeconds").asLong()));
            String error=timedOut?"TASK_TIMEOUT: 执行截止时间已到且Worker租约失联，不自动重试超时任务":"WORKER_LOST: Worker心跳超时";
            db.jdbc.update("UPDATE tasks SET status='FAILED',error=?,updated_at=? WHERE id=?",error,now(),id);syncAttempt(id);log(id,"ERROR",timedOut?"执行超时且Worker租约失联，当前尝试已失败，不自动重试":"Worker心跳超时，当前执行尝试已失败");
            if(!timedOut&&num(task,"automatic_retries")<2) {db.jdbc.update("UPDATE tasks SET automatic_retries=automatic_retries+1 WHERE id=?",id);resetForNewAttempt(id);enqueue(id);log(id,"WARN","Worker心跳超时，已使用新的执行尝试重新排队");}
        }
        var expired=db.jdbc.queryForList("SELECT id FROM tasks WHERE status='QUEUED' AND updated_at<? FOR UPDATE",Timestamp.from(Instant.now().minusSeconds(3600)));
        for(var task:expired) {String id=str(task,"id");db.jdbc.update("UPDATE tasks SET status='FAILED',error=?,updated_at=? WHERE id=?","QUEUE_TIMEOUT: 排队超过1小时，请检查Worker和RabbitMQ",now(),id);syncAttempt(id);log(id,"ERROR","排队超时，请检查Worker和RabbitMQ");}
        db.jdbc.update("DELETE FROM sessions WHERE expires_at<?",now());db.jdbc.update("DELETE FROM submissions WHERE created_at<?",Timestamp.from(Instant.now().minusSeconds(3600)));
        db.jdbc.update("DELETE FROM task_idempotency WHERE expires_at<?",now());
        db.jdbc.update("DELETE FROM task_operation_keys WHERE expires_at<?",now());
    }
}
