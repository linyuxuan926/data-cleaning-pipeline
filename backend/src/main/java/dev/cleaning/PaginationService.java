package dev.cleaning;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;
import java.util.function.Function;
import static dev.cleaning.Database.*;

/** Keyset pages bind authorization scope, query and insertion cutoff in a signed cursor. */
@Service
public class PaginationService {
    private final Database db;private final Json json;private final AuthService auth;private final TaskService tasks;private final FileService files;private final RuleService rules;private final byte[] secret;
    public PaginationService(Database db,Json json,AuthService auth,TaskService tasks,FileService files,RuleService rules,@Value("${cleaning.internal-token}") String secret) {this.db=db;this.json=json;this.auth=auth;this.tasks=tasks;this.files=files;this.rules=rules;this.secret=secret.getBytes(StandardCharsets.UTF_8);}
    public Map<String,Object> page(String user,String resource,String taskId,String attemptId,Map<String,String> query) {
        var filters=new TreeMap<>(query);String cursor=filters.remove("cursor"),rawSize=filters.remove("pageSize");
        int size=20;try {if(rawSize!=null) size=Integer.parseInt(rawSize);} catch(NumberFormatException e) {throw ApiException.bad("pageSize无效");}
        if(size<1||size>100) throw ApiException.bad("pageSize范围为1-100");
        for(String key:filters.keySet()) if(!Set.of("sort","q","status","format","ownerId","createdFrom","createdTo","role","enabled","planId").contains(key)) throw ApiException.bad("未知分页参数: "+key);
        filters.entrySet().removeIf(e->e.getValue()==null||e.getValue().isBlank());
        String sort=filters.getOrDefault("sort","createdAt.desc");if(!Set.of("createdAt.desc","createdAt.asc").contains(sort)) throw ApiException.bad("sort仅支持createdAt.desc/createdAt.asc");
        filters.put("sort",sort);boolean ascending=sort.endsWith("asc"),adminResource=resource.startsWith("admin-");
        if(adminResource&&!auth.admin(user)) throw new ApiException(org.springframework.http.HttpStatus.FORBIDDEN,"需要管理员权限");
        String scope=user+":"+resource+":"+Objects.toString(taskId,"")+":"+Objects.toString(attemptId,"");
        String binding=signature(scope+"|"+json.write(filters));Instant asOf=Instant.now();JsonNode decoded=null;
        if(cursor!=null&&!cursor.isBlank()) {
            decoded=decode(cursor);if(!binding.equals(decoded.path("binding").asText())) throw ApiException.bad("cursor不属于当前用户、资源或筛选条件");
            try {asOf=Instant.parse(decoded.path("asOf").asText());} catch(Exception e) {throw ApiException.bad("cursor截止时间无效");}
        }
        String select,from,created,id,owner,name=null;Function<Map<String,Object>,Map<String,Object>> mapper;List<Object> args=new ArrayList<>();List<String> where=new ArrayList<>();
        switch(resource) {
            case "files","uploads" -> {select="f.*,u.username AS uploader_name";from="files f JOIN users u ON u.id=f.user_id";created="f.created_at";id="f.id";owner="f.user_id";name="f.name";mapper=files::view;}
            case "rules" -> {select="v.*,r.id AS page_id,r.created_at AS page_created";from="rule_sets r JOIN rule_versions v ON v.rule_set_id=r.id AND v.version=r.current_version";created="r.created_at";id="r.id";owner="r.user_id";name="r.name";mapper=rules::view;}
            case "versions" -> {rules.versionOwner(user,taskId);select="v.*";from="rule_versions v";created="v.created_at";id="v.version";owner=null;name="v.name";mapper=rules::view;where.add("v.rule_set_id=?");args.add(taskId);}
            case "tasks","admin-tasks","reports" -> {select="t.*,f.name AS file_name,u.username";from="tasks t JOIN files f ON f.id=t.file_id JOIN users u ON u.id=t.user_id";created="t.created_at";id="t.id";owner="t.user_id";name="f.name";mapper=tasks::view;if(resource.equals("reports")) where.add("t.status='SUCCEEDED'");}
            case "attempts" -> {tasks.detail(user,taskId);select="a.*";from="task_attempts a";created="a.created_at";id="a.attempt_id";owner=null;mapper=tasks::attemptView;where.add("a.task_id=?");args.add(taskId);}
            case "logs" -> {tasks.detail(user,taskId);if(attemptId!=null) tasks.attempt(user,taskId,attemptId);select="l.*";from="task_logs l";created="l.created_at";id="l.id";owner=null;mapper=r->map("id",r.get("id"),"attemptId",str(r,"attempt_id"),"requestId",str(r,"request_id"),"level",str(r,"level"),"message",str(r,"message"),"createdAt",time(r,"created_at"));where.add("l.task_id=?");args.add(taskId);if(attemptId!=null) {where.add("l.attempt_id=?");args.add(attemptId);}}
            case "admin-users" -> {select="u.*";from="users u";created="u.created_at";id="u.id";owner=null;name="u.username";mapper=r->map("id",str(r,"id"),"username",str(r,"username"),"role",str(r,"role"),"enabled",r.get("enabled"),"createdAt",time(r,"created_at"));}
            case "admin-retention-plans" -> {select="p.*";from="retention_plans p";created="p.created_at";id="p.id";owner="p.created_by";mapper=r->map("id",str(r,"id"),"state",str(r,"state"),"createdBy",str(r,"created_by"),"truncated",r.get("truncated"),"createdAt",time(r,"created_at"),"updatedAt",time(r,"updated_at"));}
            case "admin-retention-runs" -> {select="r.*";from="retention_runs r";created="r.created_at";id="r.id";owner=null;mapper=r->map("id",r.get("id"),"planId",str(r,"plan_id"),"itemId",str(r,"item_id"),"action",str(r,"action"),"message",str(r,"message"),"requestId",str(r,"request_id"),"createdAt",time(r,"created_at"),"plan_id",str(r,"plan_id"),"item_id",str(r,"item_id"),"request_id",str(r,"request_id"),"created_at",time(r,"created_at"));}
            default -> throw ApiException.missing();
        }
        if(owner!=null) {where.add(owner+"=?");args.add(adminResource?filters.getOrDefault("ownerId",""):user);if(adminResource&&!filters.containsKey("ownerId")) {where.remove(where.size()-1);args.remove(args.size()-1);}}
        if(filters.containsKey("ownerId")&&!adminResource) throw ApiException.bad("ownerId仅供管理员跨用户列表使用");
        if(filters.containsKey("ownerId")&&owner==null) throw ApiException.bad("当前列表不支持ownerId");
        if(filters.containsKey("q")) {if(name==null) throw ApiException.bad("当前列表不支持q");if(filters.get("q").length()>255) throw ApiException.bad("q过长");where.add(name+" LIKE ? ESCAPE '!'");args.add("%"+filters.get("q").replace("!","!!").replace("%","!%").replace("_","!_")+"%");}
        if(filters.containsKey("status")) {String column=switch(resource) {case "files","uploads" -> "f.status";case "tasks","admin-tasks","reports" -> "t.status";case "attempts" -> "a.status";case "logs" -> "l.level";case "admin-retention-plans" -> "p.state";case "admin-retention-runs" -> "r.action";default -> throw ApiException.bad("当前列表不支持status");};var statuses=filters.get("status").split(",");if(statuses.length>8) throw ApiException.bad("status过多");where.add(column+" IN ("+String.join(",",Collections.nCopies(statuses.length,"?"))+")");args.addAll(Arrays.asList(statuses));}
        if(filters.containsKey("planId")) {if(!resource.equals("admin-retention-runs")) throw ApiException.bad("当前列表不支持planId");where.add("r.plan_id=?");args.add(filters.get("planId"));}
        if(filters.containsKey("format")) {if(!Set.of("files","uploads").contains(resource)) throw ApiException.bad("当前列表不支持format");FileService.format(filters.get("format"));where.add("f.format=?");args.add(filters.get("format"));}
        for(String key:List.of("createdFrom","createdTo")) if(filters.containsKey(key)) {where.add(created+(key.equals("createdFrom")?">=?":"<?"));args.add(timestamp(filters.get(key)));}
        if(filters.containsKey("role")) {if(!resource.equals("admin-users")||!Set.of("USER","ADMIN").contains(filters.get("role"))) throw ApiException.bad("role无效");where.add("u.role=?");args.add(filters.get("role"));}
        if(filters.containsKey("enabled")) {if(!resource.equals("admin-users")||!Set.of("true","false").contains(filters.get("enabled"))) throw ApiException.bad("enabled无效");where.add("u.enabled=?");args.add(Boolean.parseBoolean(filters.get("enabled")));}
        where.add(created+"<=?");args.add(Timestamp.from(asOf));
        if(decoded!=null) {String op=ascending?">":"<";where.add("("+created+op+"? OR ("+created+"=? AND "+id+op+"?))");Timestamp before=timestamp(decoded.path("createdAt").asText());args.add(before);args.add(before);args.add(Set.of("versions","logs","admin-retention-runs").contains(resource)?decoded.path("id").asLong():decoded.path("id").asText());}
        String direction=ascending?" ASC":" DESC";String sql="SELECT "+select+","+created+" AS cursor_created,"+id+" AS cursor_id FROM "+from+" WHERE "+String.join(" AND ",where)+" ORDER BY "+created+direction+","+id+direction+" LIMIT "+(size+1);
        var rows=db.jdbc.queryForList(sql,args.toArray());boolean hasMore=rows.size()>size;if(hasMore) rows=new ArrayList<>(rows.subList(0,size));
        String next=null;if(hasMore&&!rows.isEmpty()) {var last=rows.get(rows.size()-1);next=encode(map("v",1,"binding",binding,"asOf",asOf.toString(),"createdAt",time(last,"cursor_created"),"id",last.get("cursor_id")));}
        return map("items",rows.stream().map(mapper).toList(),"nextCursor",next,"hasMore",hasMore,"asOf",asOf.toString());
    }
    private Timestamp timestamp(String value) {try {return Timestamp.from(Instant.parse(value));} catch(Exception e) {throw ApiException.bad("时间须为ISO-8601 UTC格式");}}
    private String encode(Object value) {String body=Base64.getUrlEncoder().withoutPadding().encodeToString(json.write(value).getBytes(StandardCharsets.UTF_8));return body+"."+signature(body);}
    private JsonNode decode(String cursor) {try {if(cursor.length()>8192) throw new IllegalArgumentException();String[] parts=cursor.split("\\.",-1);if(parts.length!=2||!MessageDigest.isEqual(signature(parts[0]).getBytes(StandardCharsets.US_ASCII),parts[1].getBytes(StandardCharsets.US_ASCII))) throw new IllegalArgumentException();var result=json.read(new String(Base64.getUrlDecoder().decode(parts[0]),StandardCharsets.UTF_8));if(result.path("v").asInt()!=1) throw new IllegalArgumentException();return result;} catch(Exception e) {throw ApiException.bad("cursor无效或签名不匹配");}}
    private String signature(String body) {try {Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(secret,"HmacSHA256"));return Base64.getUrlEncoder().withoutPadding().encodeToString(mac.doFinal(body.getBytes(StandardCharsets.UTF_8)));} catch(Exception e) {throw new IllegalStateException(e);}}
}
