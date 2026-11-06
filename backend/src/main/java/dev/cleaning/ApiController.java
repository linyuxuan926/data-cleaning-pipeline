package dev.cleaning;

import jakarta.servlet.http.HttpServletRequest;
import org.springframework.core.io.InputStreamResource;
import org.springframework.http.*;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;
import java.util.*;

@RestController
public class ApiController {
    private final AuthService auth;private final FileService files;private final RuleService rules;private final TaskService tasks;private final ObjectStorage storage;private final SettingsService settings;private final AdminService admin;private final PythonGateway python;private final RetentionService retention;
    public ApiController(AuthService auth,FileService files,RuleService rules,TaskService tasks,ObjectStorage storage,SettingsService settings,AdminService admin,PythonGateway python,RetentionService retention) {this.auth=auth;this.files=files;this.rules=rules;this.tasks=tasks;this.storage=storage;this.settings=settings;this.admin=admin;this.python=python;this.retention=retention;}
    private String user(Authentication authentication) { return authentication.getName(); }
    @PostMapping("/api/auth/register") @ResponseStatus(HttpStatus.CREATED)
    Object register(@RequestBody AuthService.Credentials body) {return auth.register(body);}
    @PostMapping("/api/auth/login") Object login(@RequestBody AuthService.Credentials body) {return auth.login(body);}
    @GetMapping("/api/auth/me") Object me(Authentication a) {return auth.me(user(a));}
    @PostMapping("/api/auth/logout") Object logout(HttpServletRequest req) {auth.logout(req.getHeader("Authorization").substring(7));return Map.of("message","已退出登录");}
    @GetMapping("/api/files") Object files(Authentication a) {return files.list(user(a));}
    @PostMapping("/api/files/init") @ResponseStatus(HttpStatus.CREATED)
    Object init(Authentication a,@RequestBody FileService.Init body) {return files.init(user(a),body);}
    @PutMapping("/api/files/{id}/chunks/{index}")
    Object chunk(Authentication a,@PathVariable String id,@PathVariable int index,HttpServletRequest req) throws Exception {files.chunk(user(a),id,index,req.getInputStream());return Map.of("message","分片已保存");}
    @PostMapping("/api/files/{id}/complete") Object complete(Authentication a,@PathVariable String id) throws Exception {return files.complete(user(a),id);}
    @GetMapping("/api/files/{id}/preview") Object preview(Authentication a,@PathVariable String id,@RequestParam(required=false) Integer limit) {return files.preview(user(a),id,limit==null?settings.current().path("previewRows").asInt():limit);}
    @PutMapping("/api/uploads/{uploadId}/chunks/{index}") Object uploadChunk(Authentication a,@PathVariable String uploadId,@PathVariable int index,HttpServletRequest req) throws Exception {files.chunk(user(a),files.fileIdByUpload(user(a),uploadId),index,req.getInputStream());return Map.of("message","分片已保存");}
    @PostMapping("/api/uploads/{uploadId}/complete") Object uploadComplete(Authentication a,@PathVariable String uploadId) throws Exception {return files.complete(user(a),files.fileIdByUpload(user(a),uploadId));}
    @PostMapping("/api/files/{id}/parse") Object parse(Authentication a,@PathVariable String id,@RequestBody(required=false) com.fasterxml.jackson.databind.JsonNode options) {return files.parse(user(a),id,options);}
    @GetMapping("/api/settings") Object settings() {return settings.current();}
    @GetMapping("/api/admin/users") Object users() {return admin.users();}
    @PutMapping("/api/admin/users/{id}") Object updateUser(@PathVariable String id,@RequestBody AdminService.Update input) {return admin.update(id,input);}
    @GetMapping("/api/admin/tasks") Object allTasks() {return tasks.all();}
    @PutMapping("/api/admin/settings") Object updateSettings(@RequestBody com.fasterxml.jackson.databind.JsonNode input) {return settings.update(input);}
    @GetMapping("/api/rule-sets") Object rules(Authentication a) {return rules.list(user(a));}
    @PostMapping("/api/rule-sets") @ResponseStatus(HttpStatus.CREATED)
    Object createRules(Authentication a,@RequestBody RuleService.Input body) {return rules.create(user(a),body);}
    @PutMapping("/api/rule-sets/{id}") Object updateRules(Authentication a,@PathVariable String id,@RequestBody RuleService.Input body) {return rules.update(user(a),id,body);}
    @GetMapping("/api/rule-sets/{id}/versions") Object versions(Authentication a,@PathVariable String id) {return rules.versions(user(a),id);}
    @PostMapping("/api/rules/preflight") Object preflight(Authentication a,@RequestBody com.fasterxml.jackson.databind.JsonNode input) {return tasks.preflight(user(a),input);}
    @GetMapping("/api/rule-schemas/{version}") Object ruleSchema(@PathVariable int version) {if(version!=3) throw ApiException.missing();return python.ruleSchema(version);}
    @GetMapping("/api/tasks") Object tasks(Authentication a) {return tasks.list(user(a));}
    @PostMapping("/api/tasks")
    ResponseEntity<Object> createTask(Authentication a,@RequestBody TaskService.Create body,@RequestHeader(value="Idempotency-Key",required=false) String key) {
        var created=tasks.createIdempotent(user(a),body,key);
        return ResponseEntity.status(created.replayed()?HttpStatus.OK:HttpStatus.CREATED).body(created.body());
    }
    @GetMapping("/api/tasks/{id}") Object detail(Authentication a,@PathVariable String id) {return tasks.detail(user(a),id);}
    @GetMapping("/api/tasks/{id}/attempts") Object attempts(Authentication a,@PathVariable String id) {return tasks.attempts(user(a),id);}
    @GetMapping("/api/tasks/{id}/attempts/{attemptId}") Object attempt(Authentication a,@PathVariable String id,@PathVariable String attemptId) {return tasks.attempt(user(a),id,attemptId);}
    @GetMapping("/api/tasks/{id}/attempts/{attemptId}/logs") Object attemptLogs(Authentication a,@PathVariable String id,@PathVariable String attemptId,@RequestParam(defaultValue="0") long after) {return tasks.attemptLogs(user(a),id,attemptId,after);}
    @GetMapping("/api/tasks/{id}/attempts/{attemptId}/report") Object attemptReport(Authentication a,@PathVariable String id,@PathVariable String attemptId) {return tasks.attemptReport(user(a),id,attemptId);}
    @PostMapping("/api/tasks/{id}/cancel") Object cancel(Authentication a,@PathVariable String id) {return tasks.cancel(user(a),id);}
    @PostMapping("/api/tasks/{id}/retry") ResponseEntity<Object> retry(Authentication a,@PathVariable String id,@RequestHeader(value="Idempotency-Key",required=false) String key,@RequestBody(required=false) TaskService.Retry input) {var response=tasks.retryIdempotent(user(a),id,key,input);return ResponseEntity.ok(response.body());}
    @GetMapping("/api/tasks/{id}/logs") Object logs(Authentication a,@PathVariable String id,@RequestParam(defaultValue="0") long after) {return tasks.logs(user(a),id,after);}
    @GetMapping("/api/tasks/{id}/report") Object report(Authentication a,@PathVariable String id) {return tasks.report(user(a),id);}
    @GetMapping("/api/tasks/{id}/artifacts/{kind}") ResponseEntity<InputStreamResource> artifact(Authentication a,@PathVariable String id,@PathVariable String kind) throws Exception {
        var artifact=tasks.artifact(user(a),id,kind);
        return ResponseEntity.ok().header(HttpHeaders.CONTENT_DISPOSITION,ContentDisposition.attachment().filename(artifact.get("name")).build().toString())
            .header(HttpHeaders.CACHE_CONTROL,"no-store").contentType(MediaType.APPLICATION_OCTET_STREAM).body(new InputStreamResource(retention.open(artifact.get("key"))));
    }
    @GetMapping("/api/tasks/{id}/attempts/{attemptId}/artifacts/{kind}") ResponseEntity<InputStreamResource> attemptArtifact(Authentication a,@PathVariable String id,@PathVariable String attemptId,@PathVariable String kind) throws Exception {
        var artifact=tasks.attemptArtifact(user(a),id,attemptId,kind);
        return ResponseEntity.ok().header(HttpHeaders.CONTENT_DISPOSITION,ContentDisposition.attachment().filename(artifact.get("name")).build().toString())
            .header(HttpHeaders.CACHE_CONTROL,"no-store").contentType(MediaType.APPLICATION_OCTET_STREAM).body(new InputStreamResource(retention.open(artifact.get("key"))));
    }
    @PostMapping("/internal/tasks/{id}/claim") Object claim(@PathVariable String id,@RequestBody TaskService.Claim body) {return tasks.claim(id,body);}
    @GetMapping("/internal/tasks/{id}/control") Object control(@PathVariable String id,@RequestParam int attempt,@RequestParam(required=false) String attemptId,@RequestParam String leaseToken) {return tasks.control(id,attempt,attemptId,leaseToken);}
    @PostMapping("/internal/tasks/{id}/events") Object event(@PathVariable String id,@RequestBody TaskService.Event body) {tasks.event(id,body);return Map.of("accepted",true);}
}
