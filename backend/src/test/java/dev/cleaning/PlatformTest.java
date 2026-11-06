package dev.cleaning;

import com.fasterxml.jackson.databind.*;
import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;
import java.io.*;
import java.nio.file.*;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;
import static dev.cleaning.Database.*;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.*;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.*;

@SpringBootTest(properties={
    "spring.datasource.url=jdbc:h2:mem:cleaning;MODE=MySQL;DB_CLOSE_DELAY=-1;DATABASE_TO_LOWER=TRUE",
    "spring.datasource.username=sa","spring.datasource.password=",
    "spring.rabbitmq.dynamic=false","cleaning.scheduler-enabled=false",
    "cleaning.internal-token=test-internal-token-with-at-least-32-characters",
    "cleaning.minio-endpoint=http://localhost:9000","cleaning.minio-access-key=test-access",
    "cleaning.minio-secret-key=test-secret","cleaning.minio-bucket=test-bucket",
    "cleaning.upload-dir=${java.io.tmpdir}/data-cleaning-platform-tests",
    "cleaning.python-api-url=http://localhost:8000"
})
@AutoConfigureMockMvc
@Transactional
class PlatformTest {
    @Autowired AuthService auth;@Autowired FileService files;@Autowired RuleService rules;
    @Autowired TaskService tasks;@Autowired Database db;@Autowired Json json;@Autowired ObjectMapper mapper;@Autowired MockMvc mvc;
    @MockBean ObjectStorage storage;
    @MockBean PythonGateway python;
    @Autowired SettingsService settings;@Autowired AdminService admin;
    @Autowired PaginationService pages;
    String user,other,token;
    @BeforeEach void users() throws Exception {
        when(storage.metadata(anyString())).thenAnswer(inv->new ObjectStorage.StoredObject(inv.getArgument(0),1,Instant.now()));
        when(python.preview(any())).thenReturn(json.read("{\"columns\":[{\"id\":\"f0\",\"name\":\"id\",\"dtype\":\"string\",\"inferredType\":\"string\",\"nullCount\":0},{\"id\":\"f1\",\"name\":\"name\",\"dtype\":\"string\",\"inferredType\":\"string\",\"nullCount\":0}],\"parseOptions\":{\"encoding\":\"utf-8-sig\",\"delimiter\":\",\",\"missingValues\":[\"\"]},\"rows\":[],\"rowCount\":null,\"previewRowCount\":0}"));
        var session=auth.register(new AuthService.Credentials("user_"+UUID.randomUUID().toString().substring(0,8),"strong-password-123"));
        token=session.get("token").toString();user=((Map<?,?>)session.get("user")).get("id").toString();
        other=((Map<?,?>)auth.register(new AuthService.Credentials("other_"+UUID.randomUUID().toString().substring(0,8),"strong-password-456")).get("user")).get("id").toString();
    }
    String file() throws Exception {
        byte[] bytes="id,name\n1, Alice \n2,Bob\n".getBytes(java.nio.charset.StandardCharsets.UTF_8);
        String id=files.init(user,new FileService.Init("customers.csv",(long)bytes.length,262144)).get("id").toString();
        files.chunk(user,id,0,new ByteArrayInputStream(bytes));files.complete(user,id);return id;
    }
    String rule() {return rules.create(user,new RuleService.Input("Trim",json.read("[{\"type\":\"trim\",\"columns\":[\"name\"]}]"))).get("id").toString();}
    String task() throws Exception {return tasks.create(user,new TaskService.Create(file(),rule(),1,"csv","v2-phased")).get("id").toString();}
    TaskService.Event event(int attempt,String lease,String status,int progress,String message) {return new TaskService.Event(attempt,lease,status,progress,message,null,null);}

    @Test void bearerAuthenticationAndLogout() throws Exception {
        mvc.perform(get("/api/files")).andExpect(status().isUnauthorized()).andExpect(jsonPath("$.message").exists());
        mvc.perform(get("/api/auth/me").header("Authorization","Bearer "+token)).andExpect(status().isOk()).andExpect(jsonPath("$.id").value(user));
        mvc.perform(post("/api/auth/logout").header("Authorization","Bearer "+token)).andExpect(status().isOk());
        mvc.perform(get("/api/files").header("Authorization","Bearer "+token)).andExpect(status().isUnauthorized());
        mvc.perform(post("/internal/tasks/missing/claim").contentType(MediaType.APPLICATION_JSON).content("{\"attempt\":1}")).andExpect(status().isUnauthorized());
    }
    @Test void uploadChecksLengthsOwnershipAndIdempotentMerge() throws Exception {
        byte[] content="a,b\n1,2\n".getBytes();
        String id=files.init(user,new FileService.Init("data.csv",(long)content.length,262144)).get("id").toString();
        assertThatThrownBy(()->files.chunk(other,id,0,new ByteArrayInputStream(content))).isInstanceOf(ApiException.class);
        assertThatThrownBy(()->files.chunk(user,id,0,new ByteArrayInputStream(new byte[1]))).hasMessageContaining("分片长度");
        assertThatThrownBy(()->files.complete(user,id)).hasMessageContaining("缺少分片");
        files.chunk(user,id,0,new ByteArrayInputStream(content));
        doAnswer(invocation->{assertThat(Files.readAllBytes(invocation.getArgument(1,Path.class))).isEqualTo(content);return null;}).when(storage).put(anyString(),any(Path.class),anyString());
        assertThat(files.complete(user,id)).containsEntry("status","READY");files.complete(user,id);
        verify(storage,times(1)).put(anyString(),any(Path.class),anyString());
        assertThat(files.list(other)).isEmpty();
    }
    @Test void ruleVersionsAreImmutableAndValidated() {
        String id=rule();
        rules.update(user,id,new RuleService.Input("Upper",json.read("[{\"type\":\"case\",\"columns\":[\"name\"],\"mode\":\"upper\"}]")));
        assertThat(((JsonNode)rules.version(user,id,1).get("rules")).get(0).path("type").asText()).isEqualTo("trim");
        assertThat(rules.versions(user,id)).hasSize(2);
        assertThatThrownBy(()->rules.version(other,id,1)).isInstanceOf(ApiException.class);
        for(String invalid:List.of("[null]","[{\"columns\":[\"name\"]}]","[{\"type\":null}]","{}")) {
            assertThatThrownBy(()->rules.validateRules(json.read(invalid))).isInstanceOf(ApiException.class);
        }
        doThrow(ApiException.bad("authoritative engine validation failure")).when(python).validateRules(any());
        assertThatThrownBy(()->rules.create(user,new RuleService.Input("Invalid",json.read("[{\"type\":\"unknown\"}]")))).hasMessageContaining("authoritative engine");
    }
    @Test void transactionallyCreatesOutboxAndEnforcesActiveLimit() throws Exception {
        String file=file(),rule=rule();
        for(int i=0;i<3;i++) tasks.create(user,new TaskService.Create(file,rule,1,"csv"));
        assertThat(db.count("SELECT COUNT(*) FROM outbox")).isEqualTo(3);
        assertThatThrownBy(()->tasks.create(user,new TaskService.Create(file,rule,1,"csv"))).hasMessageContaining("活跃任务");
        assertThat(db.count("SELECT COUNT(*) FROM outbox")).isEqualTo(3);
        assertThatThrownBy(()->tasks.create(other,new TaskService.Create(file,rule,1,"csv"))).isInstanceOf(ApiException.class);
    }
    @Test void leasePreventsDuplicateExecutionAndProgressRegression() throws Exception {
        String id=task();var claim=tasks.claim(id,new TaskService.Claim(1));String lease=claim.get("leaseToken").toString();
        assertThat(claim.get("execute")).isEqualTo(true);
        assertThatThrownBy(()->tasks.claim(id,new TaskService.Claim(1))).hasMessageContaining("已被Worker领取");
        assertThatThrownBy(()->tasks.event(id,event(1,"wrong","RUNNING",10,""))).hasMessageContaining("租约无效");
        tasks.event(id,event(1,lease,"RUNNING",50,"Half way"));tasks.event(id,event(1,lease,"RUNNING",20,""));
        assertThat(tasks.detail(user,id)).containsEntry("progress",50);
        assertThat(tasks.control(id,1,lease)).containsEntry("cancelled",false);
        assertThat(tasks.control(id,1,"wrong")).containsEntry("cancelled",true);
    }
    @Test void cancellationAndRetryRejectStaleCallbacks() throws Exception {
        String id=task();String lease=tasks.claim(id,new TaskService.Claim(1)).get("leaseToken").toString();
        assertThat(tasks.cancel(user,id)).containsEntry("status","CANCELING");tasks.event(id,event(1,lease,"RUNNING",80,"late"));
        assertThat(tasks.detail(user,id)).containsEntry("status","CANCELING");
        tasks.event(id,event(1,lease,"CANCELLED",80,"Stopped"));
        assertThat(tasks.detail(user,id)).containsEntry("status","CANCELED");
        assertThat(tasks.claim(id,new TaskService.Claim(1))).containsEntry("execute",false);
        tasks.retry(user,id);tasks.event(id,event(1,lease,"FAILED",80,"late"));
        assertThat(tasks.detail(user,id)).containsEntry("status","QUEUED").containsEntry("attempt",2);
        assertThat(db.count("SELECT COUNT(*) FROM outbox WHERE task_id=?",id)).isEqualTo(2);
    }
    @Test void leaseTimeoutRecoversWithNewAttempt() throws Exception {
        String id=task();String lease=tasks.claim(id,new TaskService.Claim(1)).get("leaseToken").toString();
        db.jdbc.update("UPDATE tasks SET lease_at=? WHERE id=?",Timestamp.from(Instant.now().minusSeconds(121)),id);
        tasks.recover();
        assertThat(tasks.detail(user,id)).containsEntry("status","QUEUED").containsEntry("attempt",2);
        tasks.event(id,event(1,lease,"SUCCEEDED",100,"late"));
        assertThat(tasks.detail(user,id)).containsEntry("status","QUEUED");
    }
    @Test void successPersistsReportAndArtifactsAndIsTerminal() throws Exception {
        String id=task();String lease=tasks.claim(id,new TaskService.Claim(1)).get("leaseToken").toString();
        var report=mapper.valueToTree(map("taskId",id,"attempt",1,"inputRows",2,"outputRows",1,"dirtyRows",1,"changedCells",1));
        var artifacts=mapper.valueToTree(map("result","results/"+id+"/1/result.csv","dirty","dirty/"+id+"/1/dirty.csv","report","reports/"+id+"/1/report.json"));
        tasks.event(id,new TaskService.Event(1,lease,"SUCCEEDED",100,"Done",report,artifacts));
        tasks.event(id,event(1,lease,"FAILED",100,"late error"));
        assertThat(tasks.detail(user,id)).containsEntry("status","SUCCEEDED").containsEntry("progress",100).containsEntry("error",null);
        assertThat(tasks.report(user,id).path("dirtyRows").asInt()).isEqualTo(1);
        assertThat(tasks.artifact(user,id,"result").get("key")).isEqualTo("results/"+id+"/1/result.csv");
        assertThatThrownBy(()->tasks.artifact(other,id,"result")).isInstanceOf(ApiException.class);
        assertThatThrownBy(()->tasks.cancel(user,id)).isInstanceOf(ApiException.class);
    }
    @Test void malformedPayloadsOversizeAndInjectionCannotReachBusinessState() throws Exception {
        doThrow(ApiException.bad("engine rejected invalid options")).when(python).validateRules(any());
        for(String body:List.of(
            "null",
            "{\"name\":\"Invalid\",\"rules\":null}",
            "{\"name\":\"Invalid\",\"rules\":[null]}",
            "{\"name\":\"Invalid\",\"rules\":[],\"extra\":true}",
            "{\"name\":\"Invalid\",\"rules\":[{\"type\":\"trim\",\"columns\":[null]}]}",
            "{\"name\":\"Invalid\",\"rules\":[{\"type\":\"range\",\"column\":\"age\",\"min\":null}]}",
            "{\"name\":\"Invalid\",\"rules\":[{\"type\":\"fill_null\",\"columns\":[\"x\"],\"value\":{}}]}",
            "{\"name\":\"Invalid\",\"rules\":[{\"type\":\"trim\",\"columns\":[\"x\"],\"extra\":true}]}"
        )) {
            mvc.perform(post("/api/rule-sets").header("Authorization","Bearer "+token).contentType(MediaType.APPLICATION_JSON).content(body))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.message").isString());
        }
        assertThat(db.count("SELECT COUNT(*) FROM rule_sets WHERE user_id=?",user)).isZero();
        mvc.perform(post("/api/tasks").header("Authorization","Bearer "+token).contentType(MediaType.APPLICATION_JSON).content("null")).andExpect(status().isBadRequest());
        mvc.perform(post("/api/files/init").header("Authorization","Bearer "+token).contentType(MediaType.APPLICATION_JSON).content("null")).andExpect(status().isBadRequest());
        mvc.perform(post("/api/auth/login").contentType(MediaType.APPLICATION_JSON).content("null")).andExpect(status().isBadRequest());
        mvc.perform(post("/api/rule-sets").header("Authorization","Bearer "+token).contentType(MediaType.APPLICATION_JSON).content("x".repeat(3*1024*1024+1)))
            .andExpect(status().isPayloadTooLarge());
        String id=files.init(user,new FileService.Init("tiny.csv",1L,262144)).get("id").toString();
        mvc.perform(put("/api/files/{id}/chunks/0",id).header("Authorization","Bearer "+token).contentType(MediaType.APPLICATION_OCTET_STREAM).content(new byte[]{1,2}))
            .andExpect(status().isBadRequest());
        String otherName=auth.me(other).get("username").toString();
        String otherToken=auth.login(new AuthService.Credentials(otherName,"strong-password-456")).get("token").toString();
        mvc.perform(post("/api/files/{id}/complete",id).header("Authorization","Bearer "+otherToken)).andExpect(status().isNotFound());
        assertThatThrownBy(()->files.owned(user,"' OR 1=1 --",false)).isInstanceOfSatisfying(ApiException.class,e->assertThat(e.status.value()).isEqualTo(404));
        assertThatThrownBy(()->rules.version(user,"' OR 1=1 --",1)).isInstanceOfSatisfying(ApiException.class,e->assertThat(e.status.value()).isEqualTo(404));
        assertThatThrownBy(()->tasks.detail(user,"' OR 1=1 --")).isInstanceOfSatisfying(ApiException.class,e->assertThat(e.status.value()).isEqualTo(404));
    }
    @Test void roleIsolationDisabledSessionsAndFinalAdministratorProtection() throws Exception {
        assertThat(auth.me(user)).containsEntry("role","USER").containsEntry("enabled",true);
        mvc.perform(get("/api/admin/users").header("Authorization","Bearer "+token)).andExpect(status().isForbidden());
        auth.bootstrap("bootstrap_admin","bootstrap-strong-password");
        String adminId=db.one("SELECT id FROM users WHERE username='bootstrap_admin'").get("id").toString();
        String adminToken=auth.login(new AuthService.Credentials("bootstrap_admin","bootstrap-strong-password")).get("token").toString();
        mvc.perform(get("/api/admin/users").header("Authorization","Bearer "+adminToken)).andExpect(status().isOk());
        assertThatThrownBy(()->admin.update(adminId,new AdminService.Update("USER",true))).hasMessageContaining("最后");
        assertThatThrownBy(()->admin.update(adminId,new AdminService.Update("ADMIN",false))).hasMessageContaining("最后");
        admin.update(user,new AdminService.Update("ADMIN",true));
        assertThat(auth.admin(user)).isTrue();
        admin.update(adminId,new AdminService.Update("USER",true));
        admin.update(other,new AdminService.Update("USER",false));
        assertThatThrownBy(()->admin.update(user,new AdminService.Update("ADMIN",false))).hasMessageContaining("最后");
        // Last active administrator protection applies after demoting the bootstrap account as well.
    }
    @Test void disablingAccountInvalidatesCurrentSessionAndBootstrapNeverPromotesExistingUser() throws Exception {
        admin.update(user,new AdminService.Update("USER",false));
        assertThat(auth.authenticate(token)).isNull();
        mvc.perform(get("/api/auth/me").header("Authorization","Bearer "+token)).andExpect(status().isUnauthorized());
        assertThatThrownBy(()->auth.login(new AuthService.Credentials(auth.me(user).get("username").toString(),"strong-password-123"))).isInstanceOf(ApiException.class);
        assertThatThrownBy(()->auth.bootstrap(auth.me(other).get("username").toString(),"strong-password-456")).isInstanceOf(IllegalStateException.class).hasMessageContaining("refusing promotion");
        assertThat(auth.me(other)).containsEntry("role","USER");
    }
    @Test void settingsHardCapsAndImmutableExecutionSnapshots() throws Exception {
        var baseline=settings.current().deepCopy();
        var invalid=baseline.deepCopy();((com.fasterxml.jackson.databind.node.ObjectNode)invalid).put("maxRows",500001);
        assertThatThrownBy(()->settings.update(invalid)).hasMessageContaining("maxRows");
        var changed=baseline.deepCopy();((com.fasterxml.jackson.databind.node.ObjectNode)changed).put("maxRows",1000);settings.update(changed);
        String id=task();var initial=tasks.detail(user,id);
        String fileId=initial.get("fileId").toString();
        var parsed=json.read("{\"columns\":[{\"id\":\"f0\",\"name\":\"other\",\"dtype\":\"string\",\"inferredType\":\"string\",\"nullCount\":0}],\"parseOptions\":{\"encoding\":\"gb18030\",\"delimiter\":\";\",\"missingValues\":[\"NA\"]},\"rows\":[],\"previewRowCount\":0}");
        when(python.preview(any())).thenReturn(parsed);files.parse(user,fileId,json.read("{\"delimiter\":\";\"}"));
        settings.update(baseline);tasks.cancel(user,id);tasks.retry(user,id);
        var retried=tasks.detail(user,id);
        assertThat(retried.get("inputSnapshot")).isEqualTo(initial.get("inputSnapshot"));
        assertThat(retried.get("limitsSnapshot")).isEqualTo(initial.get("limitsSnapshot"));
        assertThat(((JsonNode)retried.get("limitsSnapshot")).path("maxRows").asInt()).isEqualTo(1000);
        var message=json.read(db.one("SELECT payload FROM outbox WHERE task_id=? AND attempt=2",id).get("payload"));
        assertThat(message.path("input").path("parseOptions").path("delimiter").asText()).isEqualTo(",");
        assertThat(message.path("limits").path("maxRows").asInt()).isEqualTo(1000);
    }
    @Test void independentUploadChecksumAndParseFailureRecovery() throws Exception {
        byte[] bytes="id,name\n1,Alice\n".getBytes(java.nio.charset.StandardCharsets.UTF_8);
        String sha=java.util.HexFormat.of().formatHex(java.security.MessageDigest.getInstance("SHA-256").digest(bytes));
        var init=files.init(user,new FileService.Init("data.csv",(long)bytes.length,262144,sha,json.read("{}")));
        String id=init.get("id").toString(),upload=init.get("uploadId").toString();
        assertThat(upload).isNotEqualTo(id);assertThat(files.fileIdByUpload(user,upload)).isEqualTo(id);
        assertThatThrownBy(()->files.fileIdByUpload(other,upload)).isInstanceOf(ApiException.class);
        files.chunk(user,id,0,new ByteArrayInputStream(bytes));
        when(python.preview(any())).thenThrow(ApiException.bad("invalid CSV encoding"));
        var failed=files.complete(user,id);assertThat(failed).containsEntry("status","READY").containsEntry("parseStatus","FAILED").containsEntry("sha256",sha);
        assertThatThrownBy(()->tasks.create(user,new TaskService.Create(id,rule(),1,"csv"))).hasMessageContaining("解析");
        doReturn(json.read("{\"columns\":[{\"id\":\"f0\",\"name\":\"id\"}],\"parseOptions\":{\"encoding\":\"utf-8\",\"delimiter\":\",\"}}")).when(python).preview(any());
        assertThat(files.parse(user,id,json.read("{\"encoding\":\"utf-8\"}"))).containsEntry("parseStatus","PARSED").containsEntry("parseError",null);
        var bad=files.init(user,new FileService.Init("bad.csv",(long)bytes.length,262144,"0".repeat(64),null));
        String badId=bad.get("id").toString();files.chunk(user,badId,0,new ByteArrayInputStream(bytes));
        assertThatThrownBy(()->files.complete(user,badId)).hasMessageContaining("SHA-256");
        assertThat(files.owned(user,badId,false)).containsEntry("status","UPLOADING");
    }
    @Test void newReportAllowsMarkedRecordsWithoutBreakingConservation() throws Exception {
        String id=task();String lease=tasks.claim(id,new TaskService.Claim(1)).get("leaseToken").toString();
        var report=mapper.valueToTree(map("taskId",id,"attempt",1,"inputRows",2,"outputRows",2,"dirtyRows",1,"removedRows",0,"flaggedRows",1,"issueCount",2,"changedCells",0));
        var artifacts=mapper.valueToTree(map("result","results/"+id+"/1/result.csv","dirty","dirty/"+id+"/1/dirty.csv","report","reports/"+id+"/1/report.json"));
        tasks.event(id,new TaskService.Event(1,lease,"SUCCEEDED",100,"Marked",report,artifacts));
        assertThat(tasks.detail(user,id)).containsEntry("status","SUCCEEDED");
        assertThat(tasks.report(user,id).path("removedRows").asInt()).isZero();
        assertThatThrownBy(()->tasks.detail(other,id)).isInstanceOf(ApiException.class);
        admin.update(other,new AdminService.Update("ADMIN",true));
        assertThat(tasks.detail(other,id)).containsEntry("ownerId",user);
        assertThat(tasks.logs(other,id,0)).isNotEmpty();
        assertThat(tasks.report(other,id)).isEqualTo(tasks.report(user,id));
        assertThat(tasks.artifact(other,id,"result")).isEqualTo(tasks.artifact(user,id,"result"));
        assertThatThrownBy(()->tasks.cancel(other,id)).isInstanceOfSatisfying(ApiException.class,e->assertThat(e.status.value()).isEqualTo(404));
        assertThatThrownBy(()->tasks.retry(other,id)).isInstanceOfSatisfying(ApiException.class,e->assertThat(e.status.value()).isEqualTo(404));
    }
    @Test void legacyRetryOmitsUnavailableFieldSchemaInsteadOfFreezingEmptyColumns() throws Exception {
        String id=task();var initial=tasks.detail(user,id);tasks.cancel(user,id);
        db.jdbc.update("UPDATE tasks SET rules_snapshot=NULL,input_snapshot=NULL,limits_snapshot=NULL WHERE id=?",id);
        db.jdbc.update("UPDATE files SET fields_json=NULL,parse_options_json=NULL,parse_status='UNPARSED' WHERE id=?",initial.get("fileId"));
        tasks.retry(user,id);
        var payload=json.read(db.one("SELECT payload FROM outbox WHERE task_id=? AND attempt=2",id).get("payload"));
        assertThat(payload.path("input").path("fields").isNull()).isTrue();
        assertThat(payload.path("rules").isArray()).isTrue();
        assertThat(payload.path("limits").path("maxRows").asInt()).isEqualTo(500000);
    }

    @Test void v3AttemptIdsFenceCallbacksAndPreserveHistoryAcrossRetry() throws Exception {
        String id=tasks.create(user,new TaskService.Create(file(),rule(),1,"csv","v3-sequential")).get("id").toString();
        String firstId=tasks.detail(user,id).get("attemptId").toString();
        assertThat(tasks.detail(user,id)).containsEntry("engineVersion","v3-sequential");
        var claim=tasks.claim(id,new TaskService.Claim(1,firstId));String lease=claim.get("leaseToken").toString();
        assertThat(claim).containsEntry("attemptId",firstId).containsEntry("execute",true);
        assertThat(tasks.claim(id,new TaskService.Claim(1,UUID.randomUUID().toString()))).containsEntry("execute",false);
        assertThat(tasks.control(id,1,firstId,lease)).containsEntry("cancelled",false);
        assertThat(tasks.cancel(user,id)).containsEntry("status","CANCELING");
        tasks.event(id,new TaskService.Event(1,lease,"RUNNING",80,"late progress",null,null,firstId));
        assertThat(tasks.detail(user,id)).containsEntry("status","CANCELING");
        tasks.event(id,new TaskService.Event(1,lease,"CANCELED",80,"Stopped",null,null,firstId));
        assertThat(tasks.detail(user,id)).containsEntry("status","CANCELED");
        tasks.retry(user,id);String secondId=tasks.detail(user,id).get("attemptId").toString();
        assertThat(secondId).isNotEqualTo(firstId);
        var history=tasks.attempts(user,id);
        assertThat(history).hasSize(2);
        assertThat(history.get(0)).containsEntry("attemptId",secondId).containsEntry("status","QUEUED");
        assertThat(history.get(1)).containsEntry("attemptId",firstId).containsEntry("status","CANCELED");
        assertThat(tasks.attempt(user,id,firstId)).containsEntry("attemptNo",1).containsEntry("status","CANCELED");
        assertThatThrownBy(()->tasks.attempt(other,id,firstId)).isInstanceOfSatisfying(ApiException.class,e->assertThat(e.status.value()).isEqualTo(404));
        assertThatThrownBy(()->tasks.claim(id,new TaskService.Claim(2))).isInstanceOf(ApiException.class);
    }

    @Test void idempotencyKeyReplaysOneTaskAndRejectsChangedRequest() throws Exception {
        String file=file(),rule=rule();String body=json.write(new TaskService.Create(file,rule,1,"csv"));
        var first=mvc.perform(post("/api/tasks").header("Authorization","Bearer "+token).header("Idempotency-Key","request-123")
                .contentType(MediaType.APPLICATION_JSON).content(body)).andExpect(status().isCreated()).andReturn();
        String id=mapper.readTree(first.getResponse().getContentAsString()).path("id").asText();
        mvc.perform(post("/api/tasks").header("Authorization","Bearer "+token).header("Idempotency-Key","request-123")
                .contentType(MediaType.APPLICATION_JSON).content(body)).andExpect(status().isOk()).andExpect(jsonPath("$.id").value(id));
        assertThat(db.count("SELECT COUNT(*) FROM tasks WHERE user_id=?",user)).isEqualTo(1);
        assertThat(db.count("SELECT COUNT(*) FROM outbox WHERE task_id=?",id)).isEqualTo(1);
        String changed=json.write(new TaskService.Create(file,rule,1,"xlsx"));
        mvc.perform(post("/api/tasks").header("Authorization","Bearer "+token).header("Idempotency-Key","request-123")
                .contentType(MediaType.APPLICATION_JSON).content(changed)).andExpect(status().isConflict());
        assertThat(db.count("SELECT COUNT(*) FROM task_idempotency WHERE user_id=?",user)).isEqualTo(1);
    }

    @Test void unconfirmedCancellationFailsWithoutClaimingWorkerStopped() throws Exception {
        String id=task();String attemptId=tasks.detail(user,id).get("attemptId").toString();
        tasks.claim(id,new TaskService.Claim(1));tasks.cancel(user,id);
        db.jdbc.update("UPDATE tasks SET lease_at=?,updated_at=? WHERE id=?",Timestamp.from(Instant.now().minusSeconds(121)),Timestamp.from(Instant.now().minusSeconds(31)),id);
        tasks.recover();
        assertThat(tasks.detail(user,id)).containsEntry("status","FAILED");
        assertThat(tasks.detail(user,id).get("error").toString()).startsWith("CANCEL_UNCONFIRMED");
        assertThat(tasks.attempt(user,id,attemptId)).containsEntry("status","FAILED");
        assertThat(tasks.attempts(user,id)).hasSize(1);
    }

    @Test void reportRejectsInconsistentValidAndFlaggedOutputCounts() throws Exception {
        String id=task();String lease=tasks.claim(id,new TaskService.Claim(1)).get("leaseToken").toString();
        var report=mapper.valueToTree(map("taskId",id,"attempt",1,"inputRows",2,"outputRows",2,"dirtyRows",1,"removedRows",0,"flaggedRows",1,"issueCount",1,"changedCells",0,"validOutputRows",2,"flaggedOutputRows",1,"finalRemovedRows",0,"finalRemovalByRule",Map.of()));
        var artifacts=mapper.valueToTree(map("result","results/"+id+"/1/result.csv","dirty","dirty/"+id+"/1/dirty.csv","report","reports/"+id+"/1/report.json"));
        assertThatThrownBy(()->tasks.event(id,new TaskService.Event(1,lease,"SUCCEEDED",100,"bad counters",report,artifacts))).hasMessageContaining("不守恒");
        assertThat(tasks.detail(user,id)).containsEntry("status","RUNNING");
    }

    @Test void idempotentReplayIgnoresLaterParseAndSettingsChanges() throws Exception {
        var input=new TaskService.Create(file(),rule(),1,"csv");
        var first=tasks.createIdempotent(user,input,"stable-original-request");
        String id=first.body().get("id").toString();
        var changed=settings.current().deepCopy();((com.fasterxml.jackson.databind.node.ObjectNode)changed).put("maxRows",1000);settings.update(changed);
        db.jdbc.update("UPDATE files SET parse_status='FAILED',parse_error='changed after original submission' WHERE id=?",input.fileId());
        var replay=tasks.createIdempotent(user,input,"stable-original-request");
        assertThat(replay.replayed()).isTrue();
        assertThat(json.write(replay.body())).isEqualTo(json.write(first.body()));
        assertThat(db.count("SELECT COUNT(*) FROM submissions WHERE user_id=?",user)).isEqualTo(1);
        assertThat(db.count("SELECT COUNT(*) FROM task_attempts WHERE task_id=?",id)).isEqualTo(1);
    }

    @Test void cursorPagesHandleEqualTimestampsInsertionCutoffAndAuthorization() {
        var created=new ArrayList<String>();
        for(int i=0;i<5;i++) created.add(files.init(user,new FileService.Init("page_"+i+".csv",1L,262144)).get("id").toString());
        var timestamp=Timestamp.from(Instant.now().minusSeconds(10));db.jdbc.update("UPDATE files SET created_at=? WHERE user_id=?",timestamp,user);
        var query=new HashMap<>(Map.of("pageSize","2","sort","createdAt.asc"));
        var first=pages.page(user,"files",null,null,query);
        assertThat((List<?>)first.get("items")).hasSize(2);assertThat(first.get("hasMore")).isEqualTo(true);
        var inserted=files.init(user,new FileService.Init("page_new.csv",1L,262144)).get("id").toString();
        db.jdbc.update("UPDATE files SET created_at=? WHERE id=?",Timestamp.from(Instant.now().plusSeconds(1)),inserted);
        var seen=new HashSet<String>();for(Object row:(List<?>)first.get("items")) seen.add(((Map<?,?>)row).get("id").toString());
        query.put("cursor",first.get("nextCursor").toString());
        assertThatThrownBy(()->pages.page(other,"files",null,null,query)).hasMessageContaining("cursor");
        var changed=new HashMap<>(query);changed.put("q","different");assertThatThrownBy(()->pages.page(user,"files",null,null,changed)).hasMessageContaining("cursor");
        var next=pages.page(user,"files",null,null,query);for(Object row:(List<?>)next.get("items")) assertThat(seen.add(((Map<?,?>)row).get("id").toString())).isTrue();
        query.put("cursor",next.get("nextCursor").toString());var last=pages.page(user,"files",null,null,query);for(Object row:(List<?>)last.get("items")) assertThat(seen.add(((Map<?,?>)row).get("id").toString())).isTrue();
        assertThat(seen).containsExactlyInAnyOrderElementsOf(created);assertThat(last.get("hasMore")).isEqualTo(false);assertThat(seen).doesNotContain(inserted);
        assertThat(pages.page(other,"files",null,null,Map.of()).get("items")).isEqualTo(List.of());
    }

    @Test void cursorNameFilterIsLiteralAndLegacyArrayIsDeprecated() throws Exception {
        files.init(user,new FileService.Init("literal_100%.csv",1L,262144));files.init(user,new FileService.Init("literalX100other.csv",1L,262144));
        assertThat((List<?>)pages.page(user,"files",null,null,Map.of("q","_100%" )).get("items")).hasSize(1);
        mvc.perform(get("/api/files").header("Authorization","Bearer "+token)).andExpect(status().isOk()).andExpect(header().string("Deprecation","true"));
        mvc.perform(get("/api/files/page").header("Authorization","Bearer "+token).param("pageSize","101")).andExpect(status().isBadRequest());
    }

    @Test void retryKeysAreScopedToOperationAndReplayBeforeStateOrQuotaChecks() throws Exception {
        String id=task();String old=tasks.detail(user,id).get("attemptId").toString();tasks.cancel(user,id);
        var first=tasks.retryIdempotent(user,id,"retry-key",new TaskService.Retry(old));
        var replay=tasks.retryIdempotent(user,id,"retry-key",new TaskService.Retry(old));
        assertThat(replay.replayed()).isTrue();assertThat(json.write(replay.body())).isEqualTo(json.write(first.body()));
        assertThat(db.count("SELECT COUNT(*) FROM outbox WHERE task_id=?",id)).isEqualTo(2);
        assertThatThrownBy(()->tasks.retryIdempotent(user,id,"retry-key",new TaskService.Retry(UUID.randomUUID().toString()))).hasMessageContaining("IDEMPOTENCY_KEY_REUSED");
        assertThatThrownBy(()->tasks.retryIdempotent(other,id,"retry-key")).isInstanceOfSatisfying(ApiException.class,e->assertThat(e.status.value()).isEqualTo(404));
    }

    @Test void eventLedgerRejectsChangedDuplicatesAndDoesNotRenewOutOfOrderEvents() throws Exception {
        String id=task(),attemptId=tasks.detail(user,id).get("attemptId").toString();String lease=tasks.claim(id,new TaskService.Claim(1)).get("leaseToken").toString();
        String eventId=UUID.randomUUID().toString();
        var second=new TaskService.Event(1,lease,"RUNNING",60,"scan",null,null,attemptId,eventId,2L,null,"SCAN",80L,100L);
        tasks.event(id,second);long logs=db.count("SELECT COUNT(*) FROM task_logs WHERE task_id=?",id);Object heartbeat=db.one("SELECT lease_at FROM tasks WHERE id=?",id).get("lease_at");
        tasks.event(id,second);assertThat(db.count("SELECT COUNT(*) FROM task_logs WHERE task_id=?",id)).isEqualTo(logs);
        tasks.event(id,new TaskService.Event(1,lease,"RUNNING",1,"old",null,null,attemptId,UUID.randomUUID().toString(),1L,null,"READ",1L,null));
        assertThat(db.one("SELECT lease_at FROM tasks WHERE id=?",id).get("lease_at")).isEqualTo(heartbeat);
        assertThat(tasks.detail(user,id)).containsEntry("progress",60).containsEntry("stage","SCAN");
        assertThatThrownBy(()->tasks.event(id,new TaskService.Event(1,lease,"RUNNING",61,"changed",null,null,attemptId,eventId,2L,null,"SCAN",81L,100L))).hasMessageContaining("EVENT_PAYLOAD_CONFLICT");
        tasks.event(id,new TaskService.Event(1,lease,"RUNNING",65,"",null,null,attemptId,UUID.randomUUID().toString(),3L,null,"SCAN",70L,100L));
        assertThat(tasks.detail(user,id)).containsEntry("stageProcessedRows",80L);
        tasks.event(id,new TaskService.Event(1,lease,"FAILED",65,"deadline",null,null,attemptId,UUID.randomUUID().toString(),4L,"TASK_TIMEOUT","SCAN",80L,100L));
        tasks.event(id,new TaskService.Event(1,lease,"RUNNING",90,"late",null,null,attemptId,UUID.randomUUID().toString(),5L,null,"WRITE",1L,null));
        assertThat(tasks.detail(user,id)).containsEntry("status","FAILED").containsEntry("errorCode","TASK_TIMEOUT");
        assertThat(db.count("SELECT COUNT(*) FROM task_events WHERE task_id=?",id)).isEqualTo(5);
    }

    @Test void schema3PreflightFreezesBindingsAndRejectsParseVersionChanges() throws Exception {
        String file=file();var logical=json.read("[{\"id\":\"customer\",\"label\":\"客户\"}]");var config=json.read("[{\"id\":\"trim-name\",\"type\":\"trim\",\"enabled\":true,\"fieldIds\":[\"customer\"]}]");
        String rule=rules.create(user,new RuleService.Input("Bound",config,3,"v3-disk",logical)).get("id").toString();
        when(python.preflight(any())).thenReturn(mapper.valueToTree(map("valid",true,"normalizedRules",config,"outputFields",logical,"warnings",List.of())));
        var input=new TaskService.Create(file,rule,1,"csv",null,3,json.read("{\"customer\":\"f1\"}"),1);
        var created=tasks.create(user,input);assertThat(created).containsEntry("engineVersion","v3-disk").containsEntry("schemaVersion",3);
        var message=json.read(db.one("SELECT payload FROM outbox WHERE task_id=?",created.get("id")).get("payload"));
        assertThat(message.path("schemaVersion").asInt()).isEqualTo(3);assertThat(message.path("fieldBindings").path("customer").asText()).isEqualTo("f1");
        verify(python).preflight(any());
        db.jdbc.update("UPDATE files SET parse_version=2 WHERE id=?",file);
        assertThatThrownBy(()->tasks.create(user,input)).hasMessageContaining("PARSE_VERSION_CHANGED");
    }

    @Test @Transactional(propagation=org.springframework.transaction.annotation.Propagation.NOT_SUPPORTED)
    void simultaneousRetryWithOneKeyCreatesExactlyOneAttempt() throws Exception {
        String id=task();tasks.cancel(user,id);
        var start=new java.util.concurrent.CountDownLatch(1);var executor=java.util.concurrent.Executors.newFixedThreadPool(2);
        try {
            java.util.concurrent.Callable<TaskService.Created> retry=()->{start.await();return tasks.retryIdempotent(user,id,"simultaneous-retry");};
            var a=executor.submit(retry);var b=executor.submit(retry);start.countDown();
            var first=a.get(10,java.util.concurrent.TimeUnit.SECONDS);var second=b.get(10,java.util.concurrent.TimeUnit.SECONDS);
            assertThat(first.replayed()).isNotEqualTo(second.replayed());assertThat(json.write(first.body())).isEqualTo(json.write(second.body()));
            assertThat(db.count("SELECT COUNT(*) FROM task_attempts WHERE task_id=?",id)).isEqualTo(2);
            assertThat(db.count("SELECT COUNT(*) FROM outbox WHERE task_id=?",id)).isEqualTo(2);
        } finally {executor.shutdownNow();for(String table:List.of("task_events","task_logs","outbox","task_operation_keys","task_idempotency","task_attempts")) db.jdbc.update("DELETE FROM "+table+" WHERE task_id=?",id);db.jdbc.update("DELETE FROM tasks WHERE id=?",id);}
    }

    @Test void serverRejectsSuccessAfterFrozenDeadlineWithoutPublishingArtifacts() throws Exception {
        String id=task(),attemptId=tasks.detail(user,id).get("attemptId").toString();String lease=tasks.claim(id,new TaskService.Claim(1)).get("leaseToken").toString();
        var limits=(com.fasterxml.jackson.databind.node.ObjectNode)json.read(db.one("SELECT limits_snapshot FROM tasks WHERE id=?",id).get("limits_snapshot"));limits.put("executionTimeoutSeconds",1);
        db.jdbc.update("UPDATE tasks SET limits_snapshot=? WHERE id=?",json.write(limits),id);
        db.jdbc.update("UPDATE task_attempts SET started_at=? WHERE attempt_id=?",Timestamp.from(Instant.now().minusSeconds(2)),attemptId);
        tasks.event(id,new TaskService.Event(1,lease,"SUCCEEDED",100,"late result",null,null,attemptId,UUID.randomUUID().toString(),1L,null,"UPLOAD",2L,2L));
        assertThat(tasks.detail(user,id)).containsEntry("status","FAILED").containsEntry("errorCode","TASK_TIMEOUT");
        assertThat(tasks.attempt(user,id,attemptId).get("artifacts")).isNull();assertThat(tasks.attempt(user,id,attemptId).get("report")).isNull();
        assertThat(db.one("SELECT disposition FROM task_events WHERE task_id=?",id)).containsEntry("disposition","DEADLINE_EXCEEDED");
        verify(storage,never()).verify(anyString());tasks.recover();assertThat(tasks.attempts(user,id)).hasSize(1);
    }

    @Test void lostLeaseAfterFrozenDeadlineFailsWithoutAutomaticRetry() throws Exception {
        String id=tasks.create(user,new TaskService.Create(file(),rule(),1,"csv","v3-sequential")).get("id").toString();
        String attemptId=tasks.detail(user,id).get("attemptId").toString();tasks.claim(id,new TaskService.Claim(1,attemptId));
        var limits=(com.fasterxml.jackson.databind.node.ObjectNode)json.read(db.one("SELECT limits_snapshot FROM tasks WHERE id=?",id).get("limits_snapshot"));limits.put("executionTimeoutSeconds",1);db.jdbc.update("UPDATE tasks SET limits_snapshot=?,lease_at=? WHERE id=?",json.write(limits),Timestamp.from(Instant.now().minusSeconds(121)),id);
        db.jdbc.update("UPDATE task_attempts SET started_at=? WHERE attempt_id=?",Timestamp.from(Instant.now().minusSeconds(2)),attemptId);tasks.recover();
        assertThat(tasks.detail(user,id)).containsEntry("status","FAILED").containsEntry("errorCode","TASK_TIMEOUT");
        assertThat(tasks.attempts(user,id)).hasSize(1);assertThat(tasks.attempt(user,id,attemptId)).containsEntry("status","FAILED");
    }

    @Test void schema1RuleRejectsDiskEngineInsteadOfSilentlyChangingSemantics() {
        assertThatThrownBy(()->rules.create(user,new RuleService.Input("legacy-disk",json.read("[]"),1,"v3-disk",null)))
            .isInstanceOf(ApiException.class).hasMessageContaining("schema1规则");
    }
}
