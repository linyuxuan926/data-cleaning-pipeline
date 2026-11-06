package dev.cleaning;

import org.junit.jupiter.api.*;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.mock.mockito.MockBean;
import org.springframework.transaction.annotation.Transactional;
import com.fasterxml.jackson.databind.JsonNode;
import java.io.*;
import java.time.*;
import java.util.*;
import static dev.cleaning.Database.*;
import static org.assertj.core.api.Assertions.*;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

@SpringBootTest(properties={
    "spring.datasource.url=jdbc:h2:mem:retention;MODE=MySQL;DB_CLOSE_DELAY=-1;DATABASE_TO_LOWER=TRUE",
    "spring.datasource.username=sa","spring.datasource.password=","spring.rabbitmq.dynamic=false","cleaning.scheduler-enabled=false",
    "cleaning.internal-token=test-internal-token-with-at-least-32-characters",
    "cleaning.minio-endpoint=http://localhost:9000","cleaning.minio-access-key=test-access","cleaning.minio-secret-key=test-secret","cleaning.minio-bucket=test-bucket",
    "cleaning.upload-dir=${java.io.tmpdir}/data-cleaning-retention-tests","cleaning.python-api-url=http://localhost:8000"
})
@Transactional
class RetentionTest {
    @Autowired RetentionService retention;@Autowired Database db;@Autowired Json json;@Autowired AuthService auth;
    @Autowired PaginationService pages;
    @MockBean ObjectStorage storage;@MockBean PythonGateway python;
    String admin,user,file,key;
    @BeforeEach void setup() throws Exception {
        admin=account("admin");user=account("user");db.jdbc.update("UPDATE users SET role='ADMIN' WHERE id=?",admin);
        file=UUID.randomUUID().toString();key="raw/"+file+"/input.csv";
        db.jdbc.update("INSERT INTO files(id,user_id,name,format,size,chunk_size,total_chunks,status,object_key,created_at,upload_id,parse_status) VALUES(?,?,'input.csv','csv',10,262144,1,'READY',?,?,?,'PARSED')",file,user,key,now(),UUID.randomUUID().toString());
        var object=new ObjectStorage.StoredObject(key,10,Instant.now().minusSeconds(20));
        when(storage.objects()).thenReturn(List.of(object));when(storage.metadata(key)).thenReturn(object);
        when(storage.objectsAfter(anyString())).thenAnswer(inv->{String after=inv.getArgument(0);var found=new ArrayList<ObjectStorage.StoredObject>();for(var value:storage.objects())if(value.key().compareTo(after)>0)found.add(value);found.sort(Comparator.comparing(ObjectStorage.StoredObject::key));return found;});
        when(storage.get(key)).thenReturn(new ByteArrayInputStream(new byte[]{1}));
    }
    String account(String name) {var result=auth.register(new AuthService.Credentials(name+"_"+UUID.randomUUID().toString().substring(0,8),"strong-password-123"));return ((Map<?,?>)result.get("user")).get("id").toString();}
    void eligible() {
        retention.update(admin,json.read("{\"includeExisting\":true,\"confirmation\":\"INCLUDE_EXISTING_DATA\"}"));
        retention.update(admin,json.read("{\"rawDays\":0,\"graceHours\":0,\"logDays\":36500}"));
    }
    void enable() {retention.update(admin,json.read("{\"enabled\":true,\"confirmation\":\"ENABLE_RETENTION\"}"));}
    String plan() throws Exception {return ((Map<?,?>)retention.plan(admin,new RetentionService.Scope(List.of(file),null,false))).get("id").toString();}
    @Test void defaultsAreDryRunAndFormalDeletionNeedsExplicitAdminAction() throws Exception {
        assertThat(retention.policy(admin).path("enabled").asBoolean()).isFalse();
        assertThatThrownBy(()->retention.policy(user)).isInstanceOf(ApiException.class);
        assertThatThrownBy(()->retention.update(admin,json.read("{\"enabled\":true}"))).isInstanceOf(ApiException.class);
        eligible();String plan=plan();
        assertThatThrownBy(()->retention.execute(admin,plan,false)).isInstanceOf(ApiException.class);
        verify(storage,never()).remove(anyString());
        enable();retention.execute(admin,plan,false);
        verify(storage).remove(key);assertThat(db.count("SELECT COUNT(*) FROM retention_objects WHERE object_key=? AND state='DELETED'",key)).isEqualTo(1);
        assertThatThrownBy(()->retention.assertAvailable(key,"INPUT_EXPIRED")).isInstanceOfSatisfying(ApiException.class,e->assertThat(e.status.value()).isEqualTo(410));
        retention.execute(admin,plan,false);verify(storage,times(1)).remove(key);
    }
    @Test void activeDownloadProtectsObjectThenClosedLeaseAllowsNewPlan() throws Exception {
        eligible();enable();
        try(var stream=retention.open(key)) {
            assertThat(stream.read()).isEqualTo(1);String p=plan();retention.execute(admin,p,false);
            verify(storage,never()).remove(anyString());
            assertThat(db.count("SELECT COUNT(*) FROM retention_items WHERE plan_id=? AND reason='ACTIVE_DOWNLOAD'",p)).isEqualTo(1);
        }
        assertThat(db.count("SELECT COUNT(*) FROM download_leases WHERE object_key=?",key)).isZero();
        retention.execute(admin,plan(),false);verify(storage).remove(key);
    }
    @Test void partialFailureIsAuditedAndSafeToRetry() throws Exception {
        eligible();enable();String p=plan();
        doThrow(new IOException("temporary unavailable")).doNothing().when(storage).remove(key);
        retention.execute(admin,p,false);
        assertThat(str(db.one("SELECT state FROM retention_plans WHERE id=?",p),"state")).isEqualTo("PARTIAL_FAILURE");
        assertThatThrownBy(()->retention.assertAvailable(key,"INPUT_EXPIRED")).isInstanceOf(ApiException.class);
        retention.execute(admin,p,true);
        assertThat(str(db.one("SELECT state FROM retention_plans WHERE id=?",p),"state")).isEqualTo("COMPLETED");
        verify(storage,times(2)).remove(key);
        assertThat(db.count("SELECT COUNT(*) FROM retention_runs WHERE plan_id=?",p)).isEqualTo(4);
    }
    @Test void activeTaskIsRecheckedAfterPlanningBeforeDelete() throws Exception {
        eligible();enable();String p=plan();
        String rule=UUID.randomUUID().toString(),task=UUID.randomUUID().toString();
        db.jdbc.update("INSERT INTO rule_sets(id,user_id,name,current_version,created_at) VALUES(?,?,'empty',1,?)",rule,user,now());
        db.jdbc.update("INSERT INTO rule_versions(rule_set_id,version,name,rules_json,created_at) VALUES(?,1,'empty','[]',?)",rule,now());
        db.jdbc.update("INSERT INTO tasks(id,user_id,file_id,rule_set_id,rule_version,output_format,status,progress,attempt,created_at,updated_at) VALUES(?,?,?,?,1,'csv','QUEUED',0,1,?,?)",task,user,file,rule,now(),now());
        retention.execute(admin,p,false);verify(storage,never()).remove(anyString());
        assertThat(db.count("SELECT COUNT(*) FROM retention_items WHERE plan_id=? AND reason='ACTIVE_INPUT_REFERENCE'",p)).isEqualTo(1);
    }
    @Test void existingBusinessObjectsStayProtectedUnlessExplicitlyIncluded() throws Exception {
        retention.update(admin,json.read("{\"rawDays\":0,\"graceHours\":0}"));enable();String p=plan();
        retention.execute(admin,p,false);verify(storage,never()).remove(anyString());
        assertThat(db.count("SELECT COUNT(*) FROM retention_items WHERE plan_id=? AND reason='PRE_UPGRADE_PROTECTED'",p)).isEqualTo(1);
    }
    @Test void planAndAuditCursorsAreSignedBoundToAdminAndFilteredByPlan() throws Exception {
        String p=plan();plan();
        var planPage=pages.page(admin,"admin-retention-plans",null,null,Map.of("pageSize","1"));
        assertThat((List<?>)planPage.get("items")).hasSize(1);assertThat(planPage.get("hasMore")).isEqualTo(true);
        for(int i=0;i<3;i++) db.jdbc.update("INSERT INTO retention_runs(plan_id,item_id,action,message,created_at) VALUES(?,?,'SKIP','test',?)",p,UUID.randomUUID().toString(),java.sql.Timestamp.from(Instant.now().minusSeconds(5)));
        var query=new HashMap<>(Map.of("pageSize","2","planId",p));var first=pages.page(admin,"admin-retention-runs",null,null,query);
        assertThat((List<?>)first.get("items")).hasSize(2);assertThat(first.get("hasMore")).isEqualTo(true);query.put("cursor",first.get("nextCursor").toString());
        assertThat((List<?>)pages.page(admin,"admin-retention-runs",null,null,query).get("items")).hasSize(1);
        assertThatThrownBy(()->pages.page(user,"admin-retention-runs",null,null,query)).isInstanceOfSatisfying(ApiException.class,e->assertThat(e.status.value()).isEqualTo(403));
        query.put("planId",UUID.randomUUID().toString());assertThatThrownBy(()->pages.page(admin,"admin-retention-runs",null,null,query)).hasMessageContaining("cursor");
    }

    @Test void activeLeaseRenewsPastItsOriginalExpiryAndCloseNeverResurrectsIt() throws Exception {
        eligible();enable();var pin=retention.pinInput(key);
        try {
            db.jdbc.update("UPDATE download_leases SET expires_at=? WHERE object_key=?",java.sql.Timestamp.from(Instant.now().minusSeconds(1)),key);
            pin.renew();assertThat(db.count("SELECT COUNT(*) FROM download_leases WHERE object_key=? AND expires_at>?",key,now())).isEqualTo(1);
            String p=plan();retention.execute(admin,p,false);verify(storage,never()).remove(anyString());
            assertThat(db.count("SELECT COUNT(*) FROM retention_items WHERE plan_id=? AND reason='ACTIVE_DOWNLOAD'",p)).isEqualTo(1);
        } finally {pin.close();}
        pin.renew();retention.renewActiveLeases();assertThat(db.count("SELECT COUNT(*) FROM download_leases WHERE object_key=?",key)).isZero();
        try(var stream=retention.open(key)) {
            db.jdbc.update("UPDATE download_leases SET expires_at=? WHERE object_key=?",java.sql.Timestamp.from(Instant.now().minusSeconds(1)),key);
            retention.renewActiveLeases();assertThat(stream.read()).isEqualTo(1);
            assertThat(db.count("SELECT COUNT(*) FROM download_leases WHERE object_key=? AND expires_at>?",key,now())).isEqualTo(1);
        }
        db.jdbc.update("INSERT INTO download_leases(id,object_key,expires_at) VALUES(?,?,?)",UUID.randomUUID().toString(),key,java.sql.Timestamp.from(Instant.now().minusSeconds(1)));
        retention.renewActiveLeases();assertThat(db.count("SELECT COUNT(*) FROM download_leases WHERE object_key=?",key)).isZero();
    }

    @Test void catalogContinuationReachesObjectBehindOneThousandProtectedRows() throws Exception {
        when(storage.objectsAfter(anyString())).thenReturn(List.of());
        retention.update(admin,json.read("{\"rawDays\":0,\"orphanDays\":0,\"graceHours\":0,\"logDays\":36500}"));
        String last=null;
        for(int i=0;i<1001;i++) {
            String object="raw/"+String.format("00000000-0000-0000-0000-%012d",i)+"/original.csv";last=object;
            db.jdbc.update("INSERT INTO retention_objects(object_key,kind,size_bytes,object_created_at,discovered_at,state) VALUES(?,'raw',1,?,?,'AVAILABLE')",object,java.sql.Timestamp.from(i<1000?Instant.now().minusSeconds(60):Instant.now()),now());
        }
        var first=(Map<?,?>)retention.plan(admin,new RetentionService.Scope(null,null,true));
        assertThat(first.get("truncated")).isEqualTo(true);assertThat((List<?>)first.get("items")).hasSize(1000);
        assertThat(((List<?>)first.get("items")).stream().allMatch(value->((Map<?,?>)value).get("state").equals("PROTECTED"))).isTrue();
        var scan=(JsonNode)first.get("scan");String cursor=scan.path("nextContinuation").asText();
        var second=(Map<?,?>)retention.plan(admin,new RetentionService.Scope(null,null,true,null,cursor));
        assertThat(second.get("truncated")).isEqualTo(false);assertThat((List<?>)second.get("items")).hasSize(1);
        var lastItem=(Map<?,?>)((List<?>)second.get("items")).get(0);assertThat(lastItem.get("objectKey")).isEqualTo(last);assertThat(lastItem.get("state")).isEqualTo("PLANNED");
        assertThatThrownBy(()->retention.plan(admin,new RetentionService.Scope(List.of(file),null,true,null,cursor))).hasMessageContaining("RETENTION_CURSOR_INVALID");
    }

    @Test void discoveryBudgetCountsUnmatchedObjectsAndContinuationAdvances() throws Exception {
        var objects=new ArrayList<ObjectStorage.StoredObject>();for(int i=0;i<1001;i++) objects.add(new ObjectStorage.StoredObject("raw/"+String.format("00000000-0000-0000-0000-%012d",i)+"/input.csv",1,Instant.now()));
        when(storage.objects()).thenReturn(objects);
        var first=(Map<?,?>)retention.plan(admin,new RetentionService.Scope(List.of(file),null,false));
        var scan=(JsonNode)first.get("scan");assertThat(scan.path("scannedObjects").asInt()).isEqualTo(1000);assertThat(scan.path("matchedObjects").asInt()).isZero();assertThat(first.get("truncated")).isEqualTo(true);
        var second=(Map<?,?>)retention.plan(admin,new RetentionService.Scope(List.of(file),null,false,null,scan.path("nextContinuation").asText()));
        assertThat(((JsonNode)second.get("scan")).path("scannedObjects").asInt()).isEqualTo(1);assertThat(second.get("truncated")).isEqualTo(false);
        verify(storage,never()).remove(anyString());
    }

    @Test void scopedLogPlanFiltersBeforeGlobalLimit() throws Exception {
        eligible();when(storage.objectsAfter(anyString())).thenReturn(List.of());
        retention.update(admin,json.read("{\"logDays\":0}"));String rule=UUID.randomUUID().toString();
        db.jdbc.update("INSERT INTO rule_sets(id,user_id,name,current_version,created_at) VALUES(?,?,'log fixture',1,?)",rule,user,now());
        db.jdbc.update("INSERT INTO rule_versions(rule_set_id,version,name,rules_json,created_at) VALUES(?,1,'log fixture','[]',?)",rule,now());
        String target="ffffffff-ffff-ffff-ffff-ffffffffffff";
        for(int i=0;i<1002;i++) {String id=i==1001?target:String.format("00000000-0000-0000-0000-%012d",i);db.jdbc.update("INSERT INTO tasks(id,user_id,file_id,rule_set_id,rule_version,output_format,status,progress,attempt,error,created_at,updated_at) VALUES(?,?,?,?,1,'csv','FAILED',0,1,'RETENTION_LOG_FIXTURE',?,?)",id,user,file,rule,java.sql.Timestamp.from(Instant.now().minusSeconds(60)),java.sql.Timestamp.from(Instant.now().minusSeconds(60)));}
        var p=(Map<?,?>)retention.plan(admin,new RetentionService.Scope(null,List.of(target),false));
        assertThat(((JsonNode)p.get("scan")).path("logTasksInspected").asInt()).isEqualTo(1);
        assertThat(((List<?>)p.get("items")).stream().filter(value->((Map<?,?>)value).get("kind").equals("logs")).toList()).hasSize(1);
        assertThat(((List<?>)p.get("items")).stream().anyMatch(value->target.equals(((Map<?,?>)value).get("taskId")))).isTrue();
    }

    @Test void mixedFileAndTaskScopeIncludesOnlyExplicitTaskLogs() throws Exception {
        eligible();when(storage.objectsAfter(anyString())).thenReturn(List.of());
        retention.update(admin,json.read("{\"logDays\":0}"));String rule=UUID.randomUUID().toString(),target=UUID.randomUUID().toString(),other=UUID.randomUUID().toString();
        db.jdbc.update("INSERT INTO rule_sets(id,user_id,name,current_version,created_at) VALUES(?,?,'mixed scope fixture',1,?)",rule,user,now());
        db.jdbc.update("INSERT INTO rule_versions(rule_set_id,version,name,rules_json,created_at) VALUES(?,1,'mixed scope fixture','[]',?)",rule,now());
        for(String task:List.of(target,other)) db.jdbc.update("INSERT INTO tasks(id,user_id,file_id,rule_set_id,rule_version,output_format,status,progress,attempt,error,created_at,updated_at) VALUES(?,?,?,?,1,'csv','FAILED',0,1,'RETENTION_LOG_FIXTURE',?,?)",task,user,file,rule,java.sql.Timestamp.from(Instant.now().minusSeconds(60)),java.sql.Timestamp.from(Instant.now().minusSeconds(60)));
        var mixed=(Map<?,?>)retention.plan(admin,new RetentionService.Scope(List.of(file),List.of(target),false));
        String plan=mixed.get("id").toString();
        assertThat(((JsonNode)mixed.get("scan")).path("logTasksInspected").asInt()).isEqualTo(1);
        assertThat(db.count("SELECT COUNT(*) FROM retention_items WHERE plan_id=? AND kind='logs' AND log_task_id=?",plan,target)).isEqualTo(1);
        assertThat(db.count("SELECT COUNT(*) FROM retention_items WHERE plan_id=? AND kind='logs' AND log_task_id=?",plan,other)).isZero();
        var filesOnly=(Map<?,?>)retention.plan(admin,new RetentionService.Scope(List.of(file),null,false));
        assertThat(((JsonNode)filesOnly.get("scan")).path("logTasksInspected").asInt()).isZero();
        verify(storage,never()).remove(anyString());
    }
}
