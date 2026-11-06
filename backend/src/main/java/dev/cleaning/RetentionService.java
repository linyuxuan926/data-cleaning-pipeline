package dev.cleaning;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.http.HttpStatus;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.io.*;
import java.sql.Timestamp;
import java.time.*;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;
import static dev.cleaning.Database.*;

/** Explicit, audited, reference-aware deletion. Policy defaults to dry-run. */
@Service
public class RetentionService {
    private final Database db;private final Json json;private final AuthService auth;private final ObjectStorage storage;
    private final TransactionTemplate tx;
    @Value("${cleaning.internal-token}") private String cursorSecret;
    @Value("${cleaning.scheduler-enabled:true}") private boolean schedulerEnabled;
    private final Map<String,ObjectLease> activeLeases=new ConcurrentHashMap<>();
    private static final int SCAN_LIMIT=1000;
    private static final long SCAN_NANOS=5_000_000_000L;
    public RetentionService(Database db,Json json,AuthService auth,ObjectStorage storage,PlatformTransactionManager manager) {
        this.db=db;this.json=json;this.auth=auth;this.storage=storage;this.tx=new TransactionTemplate(manager);
    }
    private void admin(String user) {if(!auth.admin(user)) throw new ApiException(HttpStatus.FORBIDDEN,"仅管理员可配置清理");}
    private void lock() {db.one("SELECT id FROM platform_lock WHERE id=1 FOR UPDATE");}
    public JsonNode policy(String user) {admin(user);return policy();}
    private ObjectNode policy() {
        var row=db.one("SELECT * FROM retention_policy WHERE id=1");
        var value=(ObjectNode)json.read(row.get("policy_json"));value.put("protectedBefore",time(row,"protected_before"));return value;
    }
    public Object update(String user,JsonNode input) {
        admin(user);if(input==null||!input.isObject())throw ApiException.bad("清理配置必须为对象");
        return tx.execute(status->{lock();var value=policy();
            Set<String> keys=Set.of("enabled","includeExisting","rawDays","resultDays","dirtyDays","reportDays","failedDays","logDays","orphanDays","graceHours","confirmation");
            input.fieldNames().forEachRemaining(key->{if(!keys.contains(key))throw ApiException.bad("未知清理配置: "+key);});
            for(String key:List.of("enabled","includeExisting")) if(input.has(key)) {
                if(!input.get(key).isBoolean())throw ApiException.bad(key+"必须为布尔值");
                if(input.path(key).asBoolean()&&!value.path(key).asBoolean()) {
                    String expected=key.equals("enabled")?"ENABLE_RETENTION":"INCLUDE_EXISTING_DATA";
                    if(!expected.equals(input.path("confirmation").asText()))throw ApiException.bad("启用此删除范围需要显式确认: "+expected);
                }
                value.set(key,input.get(key));
            }
            for(String key:keys) if(key.endsWith("Days")||key.equals("graceHours")) if(input.has(key)) {
                if(!input.get(key).isIntegralNumber()||input.get(key).asLong()<0||input.get(key).asLong()>36500)throw ApiException.bad("保留时长必须为0至36500的整数");
                value.set(key,input.get(key));
            }
            value.remove("protectedBefore");db.jdbc.update("UPDATE retention_policy SET policy_json=?,updated_at=? WHERE id=1",json.write(value),now());return policy();
        });
    }
    private boolean validKey(String key) {
        if(key==null||key.length()>512||key.contains("..")||key.contains("\\"))return false;
        return key.matches("(?:raw|results|dirty|reports)/[0-9a-fA-F-]{36}/[A-Za-z0-9._/-]+");
    }
    private void discover(ObjectStorage.StoredObject object) {
        if(!validKey(object.key()))return;
        String kind=object.key().substring(0,object.key().indexOf('/'));String file=null,task=null;Integer attempt=null;
        if(kind.equals("raw")) {
            var files=db.jdbc.queryForList("SELECT id FROM files WHERE object_key=?",object.key());
            if(!files.isEmpty())file=str(files.get(0),"id");
        } else {
            var parts=object.key().split("/");task=parts[1];try{attempt=Integer.valueOf(parts[2]);}catch(Exception e){return;}
        }
        if(db.count("SELECT COUNT(*) FROM retention_objects WHERE object_key=?",object.key())>0) {
            db.jdbc.update("UPDATE retention_objects SET file_id=?,task_id=?,attempt_no=?,size_bytes=?,object_created_at=? WHERE object_key=?",file,task,attempt,object.size(),Timestamp.from(object.createdAt()),object.key());
        } else db.jdbc.update("INSERT INTO retention_objects(object_key,kind,file_id,task_id,attempt_no,size_bytes,object_created_at,discovered_at,state) VALUES(?,?,?,?,?,?,?,?,'AVAILABLE')",object.key(),kind,file,task,attempt,object.size(),Timestamp.from(object.createdAt()),now());
    }
    private boolean referenced(String key) {
        if(db.count("SELECT COUNT(*) FROM files WHERE object_key=?",key)>0)return true;
        // Controlled keys have no quotes/backslashes; search a whole JSON string value.
        String pattern="%\""+key.replace("!","!!").replace("%","!%").replace("_","!_")+"\"%";
        return db.count("SELECT COUNT(*) FROM task_attempts WHERE artifacts_json LIKE ? ESCAPE '!'",pattern)>0;
    }
    /** Null means eligible. Every destructive attempt re-runs this under the submission lock. */
    private String protection(Map<String,Object> object,JsonNode p,boolean includeOrphans) {
        String key=str(object,"object_key");
        if("DELETED".equals(str(object,"state")))return "ALREADY_DELETED";
        Instant created=((Timestamp)object.get("object_created_at")).toInstant();
        if(!p.path("includeExisting").asBoolean()&&created.isBefore(Instant.parse(p.path("protectedBefore").asText())))return "PRE_UPGRADE_PROTECTED";
        if(activeLeases.values().stream().anyMatch(lease->!lease.closed&&lease.key.equals(key))||db.count("SELECT COUNT(*) FROM download_leases WHERE object_key=? AND expires_at>?",key,now())>0)return "ACTIVE_DOWNLOAD";
        String file=str(object,"file_id"),task=str(object,"task_id"),kind=str(object,"kind");
        if(file==null&&"raw".equals(kind)) {
            var files=db.jdbc.queryForList("SELECT id FROM files WHERE object_key=?",key);if(!files.isEmpty())file=str(files.get(0),"id");
        }
        if(task==null&&!"raw".equals(kind))task=key.split("/")[1];
        boolean referenced=referenced(key);
        if(!referenced&&!includeOrphans)return "ORPHAN_NOT_SELECTED";
        if(file!=null) {
            if(db.count("SELECT COUNT(*) FROM files WHERE id=? AND status IN ('UPLOADING','COMPLETING')",file)>0)return "ACTIVE_UPLOAD";
            if(db.count("SELECT COUNT(*) FROM tasks WHERE file_id=? AND status IN ('QUEUED','RUNNING','CANCELING')",file)>0)return "ACTIVE_INPUT_REFERENCE";
        }
        if(task!=null&&db.count("SELECT COUNT(*) FROM tasks WHERE id=? AND status IN ('QUEUED','RUNNING','CANCELING')",task)>0)return "ACTIVE_ATTEMPT";
        Instant origin=created;String policyKey=kind.equals("raw")?"rawDays":kind.equals("results")?"resultDays":kind.equals("dirty")?"dirtyDays":"reportDays";
        if(task!=null) {
            var attempts=db.jdbc.queryForList("SELECT status,finished_at FROM task_attempts WHERE task_id=? AND attempt_no=?",task,object.get("attempt_no"));
            if(!attempts.isEmpty()) {
                String state=str(attempts.get(0),"status");if(Set.of("QUEUED","RUNNING","CANCELING").contains(state))return "ACTIVE_ATTEMPT";
                if(attempts.get(0).get("finished_at") instanceof Timestamp stamp)origin=stamp.toInstant();
                if(!"SUCCEEDED".equals(state))policyKey="failedDays";
            }
        }
        if(!referenced)policyKey="orphanDays";
        Instant expiry=origin.plusSeconds(p.path(policyKey).asLong()*86400+p.path("graceHours").asLong()*3600);
        return Instant.now().isBefore(expiry)?"WITHIN_RETENTION":null;
    }
    public record Scope(List<String> fileIds,List<String> taskIds,Boolean includeOrphans,String afterObjectKey,String continuation) {
        public Scope(List<String> fileIds,List<String> taskIds,Boolean includeOrphans) {this(fileIds,taskIds,includeOrphans,null,null);}
    }
    public Object plan(String user,Scope scope) throws Exception {
        admin(user);scope=scope==null?new Scope(null,null,false):scope;
        if((scope.fileIds()!=null&&scope.fileIds().size()>100)||(scope.taskIds()!=null&&scope.taskIds().size()>100))throw ApiException.bad("清理范围最多100个文件/任务");
        for(var values:List.of(scope.fileIds()==null?List.<String>of():scope.fileIds(),scope.taskIds()==null?List.<String>of():scope.taskIds())) for(String value:values) try {UUID.fromString(value);} catch(Exception e) {throw ApiException.bad("清理范围ID须为UUID");}
        if(scope.afterObjectKey()!=null&&(scope.afterObjectKey().length()>512||scope.afterObjectKey().contains("..")||scope.afterObjectKey().contains("\\"))) throw ApiException.bad("afterObjectKey无效");
        String id=UUID.randomUUID().toString();Scope selected=scope;
        var p=policy();var scan=scanState(user,scope);int scanned=0,matched=0;
        String discoveryAfter=scan.path("discoveryAfter").asText("");
        if(!scan.path("discoveryDone").asBoolean()) {
            long stopAt=System.nanoTime()+SCAN_NANOS;
            var iterator=storage.objectsAfter(discoveryAfter).iterator();boolean complete=false;
            while(scanned<SCAN_LIMIT&&System.nanoTime()<stopAt) {
                if(!iterator.hasNext()) {complete=true;break;}
                var object=iterator.next();scanned++;discoveryAfter=object.key();
                if(!validKey(object.key()))continue;
                String[] parts=object.key().split("/");String rawFile=parts[0].equals("raw")&&parts.length>=4?parts[2]:parts[1];
                boolean all=(selected.fileIds()==null||selected.fileIds().isEmpty())&&(selected.taskIds()==null||selected.taskIds().isEmpty());
                if(!all&&!(parts[0].equals("raw")&&selected.fileIds()!=null&&selected.fileIds().contains(rawFile))&&!(selected.taskIds()!=null&&selected.taskIds().contains(parts[1])))continue;
                matched++;tx.executeWithoutResult(status->{lock();discover(object);});
            }
            scan.put("discoveryAfter",discoveryAfter);scan.put("discoveryDone",complete);
        }
        final int scannedObjects=scanned,matchedObjects=matched;
        tx.executeWithoutResult(status->{lock();
            purgeExpiredLeases();
            db.jdbc.update("INSERT INTO retention_plans(id,created_by,policy_json,scope_json,state,truncated,created_at,updated_at) VALUES(?,?,?,?,'DRY_RUN',FALSE,?,?)",id,user,json.write(p),json.write(selected),now(),now());
            int count=0;var scopeArgs=new ArrayList<Object>();String scopeSql="";
            if((selected.fileIds()!=null&&!selected.fileIds().isEmpty())||(selected.taskIds()!=null&&!selected.taskIds().isEmpty())) {
                var conditions=new ArrayList<String>();
                if(selected.fileIds()!=null&&!selected.fileIds().isEmpty()){conditions.add("file_id IN ("+String.join(",",Collections.nCopies(selected.fileIds().size(),"?"))+")");scopeArgs.addAll(selected.fileIds());}
                if(selected.taskIds()!=null&&!selected.taskIds().isEmpty()){conditions.add("task_id IN ("+String.join(",",Collections.nCopies(selected.taskIds().size(),"?"))+")");scopeArgs.addAll(selected.taskIds());}
                scopeSql=" AND ("+String.join(" OR ",conditions)+")";
            }
            scopeArgs.add(scan.path("catalogAfter").asText(""));
            String catalogUpper="";if(!scan.path("discoveryDone").asBoolean()) {catalogUpper=" AND object_key<=?";scopeArgs.add(scan.path("discoveryAfter").asText(""));}
            var objects=db.jdbc.queryForList("SELECT * FROM retention_objects WHERE state<>'DELETED'"+scopeSql+" AND object_key>?"+catalogUpper+" ORDER BY object_key LIMIT 1001",scopeArgs.toArray());
            for(var object:objects) {
                if(count>=SCAN_LIMIT)break;count++;scan.put("catalogAfter",str(object,"object_key"));
                String reason=protection(object,p,Boolean.TRUE.equals(selected.includeOrphans()));
                item(id,str(object,"object_key"),null,str(object,"kind"),reason==null?"PLANNED":"PROTECTED",reason==null?"RETENTION_EXPIRED":reason);
            }
            scan.put("catalogDone",objects.size()<=SCAN_LIMIT&&scan.path("discoveryDone").asBoolean());
            var logArgs=new ArrayList<Object>();String logScope="";
            logArgs.add(Timestamp.from(Instant.now().minusSeconds(p.path("logDays").asLong()*86400+p.path("graceHours").asLong()*3600)));
            if(selected.taskIds()!=null&&!selected.taskIds().isEmpty()) {logScope+=" AND id IN ("+String.join(",",Collections.nCopies(selected.taskIds().size(),"?"))+")";logArgs.addAll(selected.taskIds());}
            if(!p.path("includeExisting").asBoolean()) {logScope+=" AND updated_at>=?";logArgs.add(Timestamp.from(Instant.parse(p.path("protectedBefore").asText())));}
            if(selected.fileIds()!=null&&!selected.fileIds().isEmpty()&&(selected.taskIds()==null||selected.taskIds().isEmpty())) logScope+=" AND 1=0";
            logArgs.add(scan.path("logsAfter").asText(""));
            var logTasks=scan.path("logsDone").asBoolean()?List.<Map<String,Object>>of():db.jdbc.queryForList("SELECT id,updated_at FROM tasks WHERE status IN ('SUCCEEDED','FAILED','CANCELED','CANCELLED') AND updated_at<?"+logScope+" AND id>? ORDER BY id LIMIT 1001",logArgs.toArray());
            int logCount=0;
            for(var task:logTasks) {
                if(logCount>=SCAN_LIMIT)break;logCount++;String taskId=str(task,"id");scan.put("logsAfter",taskId);
                item(id,null,taskId,"logs","PLANNED","LOG_RETENTION_EXPIRED");
            }
            scan.put("logsDone",logTasks.size()<=SCAN_LIMIT);
            boolean truncated=!scan.path("discoveryDone").asBoolean()||!scan.path("catalogDone").asBoolean()||!scan.path("logsDone").asBoolean();
            var stats=map("scannedObjects",scannedObjects,"matchedObjects",matchedObjects,"catalogInspected",count,"logTasksInspected",logCount,"nextContinuation",truncated?encodeScan(user,selected,scan):null,"afterObjectKey",scan.path("discoveryAfter").asText(""),"warnings",truncated?List.of("SCAN_INCOMPLETE: 使用同一范围和nextContinuation继续生成只读计划；前页受保护对象不会阻挡后续范围。单次对象存储请求可能超过5秒检查间隔。"):List.of());
            db.jdbc.update("UPDATE retention_plans SET truncated=?,scan_json=? WHERE id=?",truncated,json.write(stats),id);
        });return detail(user,id);
    }
    private String scopeHash(Scope scope) {
        var files=new TreeSet<>(scope.fileIds()==null?List.of():scope.fileIds());var tasks=new TreeSet<>(scope.taskIds()==null?List.of():scope.taskIds());
        return sign(json.write(map("fileIds",files,"taskIds",tasks,"includeOrphans",Boolean.TRUE.equals(scope.includeOrphans()),"afterObjectKey",Objects.toString(scope.afterObjectKey(),""))));
    }
    private ObjectNode scanState(String user,Scope scope) {
        if(scope.continuation()==null) return (ObjectNode)json.read(json.write(map("v",1,"owner",user,"scopeHash",scopeHash(scope),"issuedAt",Instant.now().toString(),"discoveryAfter",Objects.toString(scope.afterObjectKey(),""),"catalogAfter",Objects.toString(scope.afterObjectKey(),""),"logsAfter","","discoveryDone",false,"catalogDone",false,"logsDone",false)));
        try {
            String token=scope.continuation();if(token.length()>8192)throw new IllegalArgumentException();String[] parts=token.split("\\.",-1);
            if(parts.length!=2||!MessageDigest.isEqual(sign(parts[0]).getBytes(StandardCharsets.US_ASCII),parts[1].getBytes(StandardCharsets.US_ASCII))) throw new IllegalArgumentException();
            var result=(ObjectNode)json.read(new String(Base64.getUrlDecoder().decode(parts[0]),StandardCharsets.UTF_8));
            Instant issued=Instant.parse(result.path("issuedAt").asText());
            if(result.path("v").asInt()!=1||!user.equals(result.path("owner").asText())||!scopeHash(scope).equals(result.path("scopeHash").asText())||issued.isBefore(Instant.now().minusSeconds(86400))||issued.isAfter(Instant.now().plusSeconds(60))) throw new IllegalArgumentException();
            return result;
        } catch(Exception e) {throw ApiException.bad("RETENTION_CURSOR_INVALID: continuation已过期、范围变化或签名无效");}
    }
    private String encodeScan(String user,Scope scope,ObjectNode scan) {String payload=Base64.getUrlEncoder().withoutPadding().encodeToString(json.write(scan).getBytes(StandardCharsets.UTF_8));return payload+"."+sign(payload);}
    private String sign(String value) {try {Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(cursorSecret.getBytes(StandardCharsets.UTF_8),"HmacSHA256"));return Base64.getUrlEncoder().withoutPadding().encodeToString(mac.doFinal(value.getBytes(StandardCharsets.UTF_8)));} catch(Exception e) {throw new IllegalStateException(e);}}
    private void item(String plan,String key,String task,String kind,String state,String reason) {
        db.jdbc.update("INSERT INTO retention_items(id,plan_id,object_key,log_task_id,kind,state,reason,updated_at) VALUES(?,?,?,?,?,?,?,?)",UUID.randomUUID().toString(),plan,key,task,kind,state,reason,now());
    }
    public Object detail(String user,String id) {
        admin(user);var plan=db.one("SELECT * FROM retention_plans WHERE id=?",id);
        var items=db.jdbc.queryForList("SELECT * FROM retention_items WHERE plan_id=? ORDER BY kind,id",id).stream().map(r->map("id",str(r,"id"),"objectKey",str(r,"object_key"),"taskId",str(r,"log_task_id"),"kind",str(r,"kind"),"state",str(r,"state"),"reason",str(r,"reason"),"attempts",num(r,"attempts"))).toList();
        return map("id",id,"state",str(plan,"state"),"policy",json.read(plan.get("policy_json")),"scope",json.read(plan.get("scope_json")),"truncated",plan.get("truncated"),"scan",plan.get("scan_json")==null?null:json.read(plan.get("scan_json")),"createdAt",time(plan,"created_at"),"items",items);
    }
    public Object execute(String user,String id,boolean retry) {
        admin(user);if(!policy().path("enabled").asBoolean())throw ApiException.conflict("清理仍为dry-run；管理员需先显式启用enabled");
        db.one("SELECT id FROM retention_plans WHERE id=?",id);
        tx.executeWithoutResult(status->{lock();purgeExpiredLeases();db.jdbc.update("UPDATE retention_plans SET state='RUNNING',updated_at=? WHERE id=?",now(),id);});
        for(var entry:db.jdbc.queryForList("SELECT id FROM retention_items WHERE plan_id=? AND state IN ('PLANNED','FAILED','DELETING') ORDER BY id",id)) {
            var intent=tx.execute(status->{lock();var item=db.one("SELECT * FROM retention_items WHERE id=? FOR UPDATE",entry.get("id"));
                if(!Set.of("PLANNED","FAILED","DELETING").contains(str(item,"state")))return null;
                var current=policy();if(!current.path("enabled").asBoolean())throw ApiException.conflict("正式清理已关闭");
                String key=str(item,"object_key"),reason=null;
                if(key!=null) {
                    var object=db.one("SELECT * FROM retention_objects WHERE object_key=? FOR UPDATE",key);
                    var scope=json.read(db.one("SELECT scope_json FROM retention_plans WHERE id=?",id).get("scope_json"));
                    reason=protection(object,current,scope.path("includeOrphans").asBoolean());
                } else {
                    var task=db.one("SELECT * FROM tasks WHERE id=? FOR UPDATE",item.get("log_task_id"));
                    if(Set.of("QUEUED","RUNNING","CANCELING").contains(str(task,"status")))reason="ACTIVE_TASK";
                    else if(!current.path("includeExisting").asBoolean()&&((Timestamp)task.get("updated_at")).toInstant().isBefore(Instant.parse(current.path("protectedBefore").asText())))reason="PRE_UPGRADE_PROTECTED";
                    else if(((Timestamp)task.get("updated_at")).toInstant().plusSeconds(current.path("logDays").asLong()*86400+current.path("graceHours").asLong()*3600).isAfter(Instant.now()))reason="WITHIN_RETENTION";
                }
                if(reason!=null){updateItem(item,"PROTECTED",reason);audit(item,"SKIPPED",reason);return null;}
                if(key==null) {
                    db.jdbc.update("DELETE FROM task_logs WHERE task_id=?",item.get("log_task_id"));
                    updateItem(item,"DELETED","DELETE_CONFIRMED");audit(item,"DELETED","Log deletion confirmed");return null;
                }
                // Commit the tombstone before the nontransactional object-store call.
                // Submissions/download leases acquire the same lock and now see HTTP 410.
                db.jdbc.update("UPDATE retention_objects SET state='DELETING' WHERE object_key=?",key);
                updateItem(item,"DELETING","DELETE_INTENT_RECORDED");audit(item,"INTENT","Deletion intent committed before object removal");return item;
            });
            if(intent==null)continue;
            boolean removed;try{storage.remove(str(intent,"object_key"));removed=true;}catch(Exception e){removed=false;}
            final boolean success=removed;
            tx.executeWithoutResult(status->{lock();var item=db.one("SELECT * FROM retention_items WHERE id=? FOR UPDATE",intent.get("id"));
                if("DELETED".equals(str(item,"state")))return;
                if(success) {
                    db.jdbc.update("UPDATE retention_objects SET state='DELETED',deleted_at=?,last_error=NULL WHERE object_key=?",now(),item.get("object_key"));
                    db.jdbc.update("UPDATE retention_items SET state='DELETED',reason='DELETE_CONFIRMED',updated_at=? WHERE id=?",now(),item.get("id"));
                    audit(item,"DELETED","Object deletion confirmed");
                } else {
                    db.jdbc.update("UPDATE retention_objects SET last_error='Object deletion failed; retry required' WHERE object_key=?",item.get("object_key"));
                    db.jdbc.update("UPDATE retention_items SET state='FAILED',reason='DELETE_RETRY_REQUIRED',updated_at=? WHERE id=?",now(),item.get("id"));
                    audit(item,"FAILED","Object deletion failed; retry required");
                }
            });
        }
        tx.executeWithoutResult(status->{lock();long failed=db.count("SELECT COUNT(*) FROM retention_items WHERE plan_id=? AND state IN ('FAILED','DELETING')",id);db.jdbc.update("UPDATE retention_plans SET state=?,updated_at=? WHERE id=?",failed>0?"PARTIAL_FAILURE":"COMPLETED",now(),id);});
        return detail(user,id);
    }
    private void updateItem(Map<String,Object> item,String state,String reason) {db.jdbc.update("UPDATE retention_items SET state=?,reason=?,attempts=attempts+1,updated_at=? WHERE id=?",state,reason,now(),item.get("id"));}
    private void audit(Map<String,Object> item,String action,String message) {db.jdbc.update("INSERT INTO retention_runs(plan_id,item_id,action,message,request_id,created_at) VALUES(?,?,?,?,?,?)",item.get("plan_id"),item.get("id"),action,message,RequestContext.id(),now());}
    public Object runs(String user,long after,int limit) {admin(user);if(after<0||limit<1||limit>100)throw ApiException.bad("分页参数无效");var rows=db.jdbc.queryForList("SELECT * FROM retention_runs WHERE id>? ORDER BY id LIMIT ?",after,limit+1);boolean more=rows.size()>limit;var items=more?rows.subList(0,limit):rows;return map("items",items,"hasMore",more,"nextCursor",more?String.valueOf(items.get(items.size()-1).get("id")):null);}
    public void assertAvailable(String key,String code) {
        var rows=db.jdbc.queryForList("SELECT state FROM retention_objects WHERE object_key=?",key);
        if(!rows.isEmpty()&&!"AVAILABLE".equals(str(rows.get(0),"state")))throw new ApiException(HttpStatus.GONE,code+": 对象已过期或正在清理");
    }
    public String status(String key) {
        var rows=db.jdbc.queryForList("SELECT state FROM retention_objects WHERE object_key=?",key);
        return rows.isEmpty()?"AVAILABLE":"DELETED".equals(str(rows.get(0),"state"))?"EXPIRED":str(rows.get(0),"state");
    }
    public ObjectLease pinInput(String key) {return lease(key,"INPUT_EXPIRED");}
    public ObjectLease pinArtifact(String key) {return lease(key,"ARTIFACT_EXPIRED");}
    private ObjectLease lease(String key,String code) {
        String id=tx.execute(status->{lock();purgeExpiredLeases();assertAvailable(key,code);
            if(db.count("SELECT COUNT(*) FROM retention_objects WHERE object_key=?",key)==0) {
                if(!referenced(key))throw ApiException.missing();
                try {discover(storage.metadata(key));}
                catch(Exception e){throw new IllegalStateException("Cannot acquire object lease",e);}
            }
            String lease=UUID.randomUUID().toString();db.jdbc.update("INSERT INTO download_leases(id,object_key,expires_at) VALUES(?,?,?)",lease,key,Timestamp.from(Instant.now().plusSeconds(3600)));return lease;
        });
        var lease=new ObjectLease(id,key,code);activeLeases.put(id,lease);return lease;
    }
    /** Renewal and close synchronize on the same lease; closed leases can never be reinserted. */
    public final class ObjectLease implements AutoCloseable {
        private final String id,key,code;private volatile boolean closed;private volatile long nextReadRenewal=System.nanoTime()+60_000_000_000L;
        private ObjectLease(String id,String key,String code) {this.id=id;this.key=key;this.code=code;}
        public void renew() {
            if(closed)return;
            // Take the database lock before the lease monitor. Preview callers can
            // already own the lock in an outer transaction when they close a pin.
            tx.executeWithoutResult(status->{lock();synchronized(this) {if(closed)return;assertAvailable(key,code);
                int updated=db.jdbc.update("UPDATE download_leases SET expires_at=? WHERE id=?",Timestamp.from(Instant.now().plusSeconds(3600)),id);
                if(updated==0) db.jdbc.update("INSERT INTO download_leases(id,object_key,expires_at) VALUES(?,?,?)",id,key,Timestamp.from(Instant.now().plusSeconds(3600)));
                nextReadRenewal=System.nanoTime()+60_000_000_000L;
            }});
        }
        private void beforeRead() {if(closed)throw new IllegalStateException("Object stream is closed");if(System.nanoTime()>=nextReadRenewal)renew();}
        public synchronized void close() {if(closed)return;closed=true;activeLeases.remove(id,this);db.jdbc.update("DELETE FROM download_leases WHERE id=?",id);}
    }
    @Scheduled(fixedDelay=30000,initialDelay=30000)
    void scheduledLeaseRenewal() {if(schedulerEnabled)renewActiveLeases();}
    void renewActiveLeases() {
        for(var lease:activeLeases.values()) try {lease.renew();} catch(RuntimeException e) {org.slf4j.LoggerFactory.getLogger(RetentionService.class).warn("Active object lease renewal deferred; local reference remains protected");}
        tx.executeWithoutResult(status->{lock();purgeExpiredLeases();});
    }
    private void purgeExpiredLeases() {
        for(var row:db.jdbc.queryForList("SELECT id FROM download_leases WHERE expires_at<=? ORDER BY expires_at,id LIMIT 1000",now())) {
            String id=str(row,"id");if(!activeLeases.containsKey(id))db.jdbc.update("DELETE FROM download_leases WHERE id=? AND expires_at<=?",id,now());
        }
    }
    public InputStream open(String key) throws Exception {
        ObjectLease lease=lease(key,"ARTIFACT_EXPIRED");
        try {return new FilterInputStream(storage.get(key)) {
            private void pin() throws IOException {try{lease.beforeRead();}catch(RuntimeException e){throw new IOException("Cannot renew object access lease",e);}}
            @Override public int read() throws IOException {pin();return in.read();}
            @Override public int read(byte[] bytes,int offset,int length) throws IOException {pin();return in.read(bytes,offset,length);}
            @Override public long skip(long count) throws IOException {pin();return in.skip(count);}
            @Override public void close() throws IOException {try{super.close();}finally{try{lease.close();}catch(Exception e){throw new IOException(e);}}}
        };}
        catch(Exception e){lease.close();throw e;}
    }
}
