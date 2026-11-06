package dev.cleaning;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.client.RestClient;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import java.io.*;
import java.nio.file.*;
import java.util.*;
import static dev.cleaning.Database.*;

@Service
public class FileService {
    static final long MAX_SIZE=100L*1024*1024;
    private final Database db; private final ObjectStorage storage; private final Path uploadDir; private final PythonGateway python;private final SettingsService settings;private final Json json;private final RetentionService retention;
    public FileService(Database db,ObjectStorage storage,@Value("${cleaning.upload-dir}") String uploadDir,PythonGateway python,SettingsService settings,Json json,RetentionService retention) throws IOException {
        this.db=db;this.storage=storage;this.uploadDir=Path.of(uploadDir).toAbsolutePath().normalize();Files.createDirectories(this.uploadDir);
        this.python=python;this.settings=settings;this.json=json;this.retention=retention;
    }
    public record Init(String name,Long size,Integer chunkSize,String sha256,com.fasterxml.jackson.databind.JsonNode parseOptions) {
        public Init(String name,Long size,Integer chunkSize) {this(name,size,chunkSize,null,null);}
    }
    @Transactional
    public Map<String,Object> init(String user,Init input) {
        if(input==null) throw ApiException.bad("请求体不能为空");
        if(input.name()==null||input.name().length()>255||input.name().isBlank()||input.name().matches(".*[\\p{Cntrl}/\\\\].*")) throw ApiException.bad("文件名无效");
        if(input.size()==null||input.size()<=0||input.size()>MAX_SIZE) throw ApiException.bad("文件大小需在1字节到100MiB安全范围内");
        String format=input.name().substring(input.name().lastIndexOf('.')+1).toLowerCase(Locale.ROOT);format(format);
        int chunk=input.chunkSize()==null?4*1024*1024:input.chunkSize();
        if(chunk<256*1024||chunk>8*1024*1024) throw ApiException.bad("分片大小需为256 KiB至8 MiB");
        // Serialize reservations so simultaneous upload initialization cannot exceed disk quotas.
        db.one("SELECT id FROM platform_lock WHERE id=1 FOR UPDATE");
        if(input.size()>settings.current().path("maxFileBytes").asLong()) throw ApiException.bad("文件大小超过当前系统配置");
        if(db.count("SELECT COUNT(*) FROM files WHERE status='UPLOADING'")>=200||db.count("SELECT COALESCE(SUM(size),0) FROM files WHERE status='UPLOADING'")+input.size()>5L*1024*1024*1024) throw new ApiException(HttpStatus.TOO_MANY_REQUESTS,"平台临时上传空间已满，请稍后重试");
        if(db.count("SELECT COALESCE(SUM(size),0) FROM files WHERE user_id=? AND status='UPLOADING'",user)+input.size()>512L*1024*1024) throw new ApiException(HttpStatus.TOO_MANY_REQUESTS,"每位用户未完成上传总大小不能超过512 MiB");
        // Bound incomplete uploads per account as well as individual file sizes.
        if(db.count("SELECT COUNT(*) FROM files WHERE user_id=? AND status='UPLOADING'",user)>=20) throw new ApiException(HttpStatus.TOO_MANY_REQUESTS,"未完成上传过多，请完成已有上传或等待过期清理");
        if(input.sha256()!=null&&!input.sha256().matches("[a-fA-F0-9]{64}")) throw ApiException.bad("sha256需为64位十六进制摘要");
        var options=options(input.parseOptions());
        String id=UUID.randomUUID().toString(),uploadId=UUID.randomUUID().toString();int chunks=(int)((input.size()+chunk-1)/chunk);
        db.jdbc.update("INSERT INTO files(id,user_id,name,format,size,chunk_size,total_chunks,status,created_at,upload_id,expected_sha256,parse_options_json) VALUES(?,?,?,?,?,?,?,'UPLOADING',?,?,?,?)",id,user,input.name(),format,input.size(),chunk,chunks,now(),uploadId,input.sha256()==null?null:input.sha256().toLowerCase(Locale.ROOT),json.write(options));
        return map("id",id,"uploadId",uploadId,"chunkSize",chunk,"totalChunks",chunks);
    }
    public static void format(String format) { if(!Set.of("csv","xlsx","parquet").contains(format)) throw ApiException.bad("支持的格式为 CSV、XLSX 和 Parquet"); }
    public Map<String,Object> owned(String user,String id,boolean lock) { return db.one("SELECT f.*,u.username AS uploader_name FROM files f JOIN users u ON u.id=f.user_id WHERE f.id=? AND f.user_id=?"+(lock?" FOR UPDATE":""),id,user); }
    @Transactional
    public void chunk(String user,String id,int index,InputStream input) throws IOException {
        Map<String,Object> file=owned(user,id,true);
        if(!"UPLOADING".equals(str(file,"status"))) throw ApiException.conflict("文件已完成或已过期");
        int chunks=num(file,"total_chunks"),chunkSize=num(file,"chunk_size");
        if(index<0||index>=chunks) throw ApiException.bad("分片序号超出范围");
        long expected=index==chunks-1?((Number)file.get("size")).longValue()-(long)index*chunkSize:chunkSize;
        Path dir=directory(id);Files.createDirectories(dir);
        Path temp=Files.createTempFile(dir,"part-",".tmp");
        try(OutputStream out=Files.newOutputStream(temp)) {
            byte[] buffer=new byte[65536];long bytes=0;int n;
            while((n=input.read(buffer))!=-1) {
                bytes+=n;if(bytes>expected) throw ApiException.bad("分片长度与声明不一致");out.write(buffer,0,n);
            }
            if(bytes!=expected) throw ApiException.bad("分片长度与声明不一致");
        } catch(Exception e) { Files.deleteIfExists(temp);throw e; }
        Files.move(temp,dir.resolve(index+".part"),StandardCopyOption.REPLACE_EXISTING);
    }
    @Transactional(rollbackFor=Exception.class)
    public Map<String,Object> complete(String user,String id) throws Exception {
        Map<String,Object> file=owned(user,id,true);
        if("READY".equals(str(file,"status"))) return view(file);
        if(!"UPLOADING".equals(str(file,"status"))) throw ApiException.conflict("上传已过期，请重新上传");
        Path dir=directory(id);Path merged=dir.resolve("merged");
        Files.createDirectories(dir);
        try(OutputStream output=Files.newOutputStream(merged)) {
            for(int i=0;i<num(file,"total_chunks");i++) {
                Path part=dir.resolve(i+".part");if(!Files.isRegularFile(part)) throw ApiException.conflict("缺少分片 "+i);
                long expected=i==num(file,"total_chunks")-1?((Number)file.get("size")).longValue()-(long)i*num(file,"chunk_size"):num(file,"chunk_size");
                if(Files.size(part)!=expected) throw ApiException.conflict("分片损坏，请重新上传分片 "+i);
                Files.copy(part,output);
            }
        } catch(Exception e) { Files.deleteIfExists(merged);throw e; }
        String digest;
        try(InputStream stream=Files.newInputStream(merged)) {
            var md=java.security.MessageDigest.getInstance("SHA-256");byte[] bytes=new byte[65536];int n;
            while((n=stream.read(bytes))!=-1) md.update(bytes,0,n);
            digest=java.util.HexFormat.of().formatHex(md.digest());
        }
        if(file.get("expected_sha256")!=null&&!digest.equals(str(file,"expected_sha256"))) {Files.deleteIfExists(merged);throw ApiException.bad("SHA-256校验失败，请核对原文件后重新上传分片");}
        String key="raw/"+user+"/"+id+"/original."+str(file,"format");
        storage.put(key,merged,"application/octet-stream");
        db.jdbc.update("UPDATE files SET status='READY',object_key=?,sha256=? WHERE id=?",key,digest,id);
        // Keep all chunks until the READY metadata has committed. A database failure can then retry completion.
        org.springframework.transaction.support.TransactionSynchronizationManager.registerSynchronization(new org.springframework.transaction.support.TransactionSynchronization() {
            @Override public void afterCommit() {
                try {
                    for(int i=0;i<num(file,"total_chunks");i++) Files.deleteIfExists(dir.resolve(i+".part"));
                    Files.deleteIfExists(merged);Files.deleteIfExists(dir);
                } catch(IOException e) { org.slf4j.LoggerFactory.getLogger(FileService.class).warn("Committed upload temporary cleanup failed: {}",id,e); }
            }
        });
        file.put("status","READY");file.put("object_key",key);file.put("sha256",digest);
        parsePersisted(file,optionsFrom(file),settings.current().path("previewRows").asInt(),true);return view(file);
    }
    @Transactional
    public Object preview(String user,String id,int limit) {
        Map<String,Object> file=owned(user,id,true);
        if(!"READY".equals(str(file,"status"))) throw ApiException.conflict("请先完成文件上传");
        if(limit<1||limit>1000) throw ApiException.bad("预览行数范围为1-1000");
        // Preview reads the persisted parse snapshot; it must not rewrite a later task snapshot.
        try(var pin=retention.pinInput(str(file,"object_key"))) {return python.preview(map("objectKey",str(file,"object_key"),"format",str(file,"format"),"limit",limit,"parseOptions",optionsFrom(file),"limits",settings.engineLimits()));}
        catch(RuntimeException e) {throw e;} catch(Exception e) {throw new IllegalStateException("输入访问租约释放失败",e);}
    }
    @Transactional
    public Map<String,Object> parse(String user,String id,com.fasterxml.jackson.databind.JsonNode input) {
        var file=owned(user,id,true);
        if(!"READY".equals(str(file,"status"))) throw ApiException.conflict("请先完成文件上传");
        parsePersisted(file,options(input),settings.current().path("previewRows").asInt(),true);return view(file);
    }
    private void parsePersisted(Map<String,Object> file,com.fasterxml.jackson.databind.JsonNode options,int limit,boolean persistFailure) {
        try(var pin=retention.pinInput(str(file,"object_key"))) {
            var preview=python.preview(map("objectKey",str(file,"object_key"),"format",str(file,"format"),"limit",limit,"parseOptions",options,"limits",settings.engineLimits()));
            if(!preview.path("columns").isArray()||!preview.path("parseOptions").isObject()) throw new IllegalStateException("Preview response missing schema or options");
            file.put("parse_status","PARSED");file.put("parse_error",null);file.put("fields_json",json.write(preview.get("columns")));file.put("parse_options_json",json.write(preview.get("parseOptions")));
            file.put("parse_version",num(file,"parse_version")+1);
        } catch(ApiException e) {
            if(e.status==HttpStatus.GONE) throw e;
            if(!persistFailure) throw e;
            file.put("parse_status","FAILED");file.put("parse_error",e.getMessage());file.put("fields_json",null);file.put("parse_options_json",json.write(options));
        } catch(RuntimeException e) {throw e;} catch(Exception e) {throw new IllegalStateException("输入访问租约释放失败",e);}
        db.jdbc.update("UPDATE files SET parse_status=?,parse_error=?,fields_json=?,parse_options_json=?,parse_version=? WHERE id=?",file.get("parse_status"),file.get("parse_error"),file.get("fields_json"),file.get("parse_options_json"),file.get("parse_version"),file.get("id"));
    }
    private com.fasterxml.jackson.databind.JsonNode options(com.fasterxml.jackson.databind.JsonNode node) {
        if(node==null||node.isNull()) return json.read("{}");
        if(!node.isObject()||json.write(node).getBytes(java.nio.charset.StandardCharsets.UTF_8).length>20000) throw ApiException.bad("parseOptions必须是小于20000字节的对象");
        return node;
    }
    private com.fasterxml.jackson.databind.JsonNode optionsFrom(Map<String,Object> file) {return file.get("parse_options_json")==null?json.read("{}"):json.read(file.get("parse_options_json"));}
    public String fileIdByUpload(String user,String uploadId) {return str(db.one("SELECT id FROM files WHERE upload_id=? AND user_id=?",uploadId,user),"id");}
    public Map<String,Object> uploadProgress(String user,String uploadId) throws IOException {
        var file=owned(user,fileIdByUpload(user,uploadId),false);
        var received=new ArrayList<Integer>();
        int total=num(file,"total_chunks"),chunkSize=num(file,"chunk_size");
        for(int index=0;index<total;index++) {
            long expected=index==total-1?((Number)file.get("size")).longValue()-(long)index*chunkSize:chunkSize;
            Path part=directory(str(file,"id")).resolve(index+".part");
            if(Files.isRegularFile(part)&&Files.size(part)==expected) received.add(index);
        }
        return map("id",str(file,"id"),"uploadId",uploadId,"status",str(file,"status"),"name",str(file,"name"),"size",file.get("size"),"chunkSize",chunkSize,"totalChunks",total,"receivedChunks",received);
    }
    public List<Map<String,Object>> list(String user) { return db.jdbc.queryForList("SELECT f.*,u.username AS uploader_name FROM files f JOIN users u ON u.id=f.user_id WHERE f.user_id=? ORDER BY f.created_at DESC LIMIT 200",user).stream().map(this::view).toList(); }
    Map<String,Object> view(Map<String,Object> f) {
        return map("id",str(f,"id"),"uploadId",str(f,"upload_id"),"name",str(f,"name"),"format",str(f,"format"),"size",f.get("size"),"status",str(f,"status"),"objectStatus",f.get("object_key")==null?null:retention.status(str(f,"object_key")),"sha256",str(f,"sha256"),"parseStatus",str(f,"parse_status"),"parseVersion",num(f,"parse_version"),"parseError",str(f,"parse_error"),"parseOptions",optionsFrom(f),"fields",f.get("fields_json")==null?List.of():json.read(f.get("fields_json")),"uploader",map("id",str(f,"user_id"),"username",str(f,"uploader_name")),"objectKey",str(f,"object_key"),"createdAt",time(f,"created_at"));
    }
    private Path directory(String id) {
        UUID.fromString(id);Path path=uploadDir.resolve(id).normalize();
        if(!path.startsWith(uploadDir)) throw ApiException.bad("无效文件标识");return path;
    }
    @Transactional
    public void cleanupExpired() throws IOException {
        var expired=db.jdbc.queryForList("SELECT * FROM files WHERE status='UPLOADING' AND created_at<? FOR UPDATE",java.sql.Timestamp.from(java.time.Instant.now().minusSeconds(86400)));
        for(var file:expired) {
            Path dir=directory(str(file,"id"));
            if(Files.isDirectory(dir)) {
                try(var entries=Files.list(dir)) { for(Path path:entries.toList()) if(Files.isRegularFile(path)) Files.delete(path); }
                Files.deleteIfExists(dir);
            }
            db.jdbc.update("UPDATE files SET status='EXPIRED' WHERE id=?",str(file,"id"));
        }
        // A crash after committing READY can leave temporary chunks. Reconcile actual UUID directories.
        try(var dirs=Files.list(uploadDir)) {
            for(Path dir:dirs.filter(Files::isDirectory).toList()) {
                String id=dir.getFileName().toString();
                try { UUID.fromString(id); } catch(IllegalArgumentException e) { continue; }
                var rows=db.jdbc.queryForList("SELECT status FROM files WHERE id=?",id);
                if(!rows.isEmpty()&&Set.of("READY","EXPIRED").contains(str(rows.get(0),"status"))) {
                    try(var entries=Files.list(dir)) { for(Path path:entries.toList()) if(Files.isRegularFile(path)) Files.delete(path); }
                    Files.deleteIfExists(dir);
                }
            }
        }
    }
}
