package dev.cleaning;

import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;
import static dev.cleaning.Database.*;

/** Authorization is resolved by TaskService before any object reference reaches Python. */
@RestController
public class ResultPreviewController {
    private final TaskService tasks;
    private final PythonGateway python;
    private final RetentionService retention;
    public ResultPreviewController(TaskService tasks, PythonGateway python,RetentionService retention) { this.tasks=tasks; this.python=python;this.retention=retention; }
    @GetMapping("/api/tasks/{id}/preview")
    Object preview(Authentication auth, @PathVariable String id,
                   @RequestParam(defaultValue="result") String kind,
                   @RequestParam(defaultValue="100") int limit) {
        if (!java.util.Set.of("result","dirty").contains(kind)) throw ApiException.bad("仅支持结果和脏数据预览");
        if (limit<1 || limit>100) throw ApiException.bad("预览数量必须为1至100");
        var artifact=tasks.artifact(auth.getName(),id,kind);
        var task=tasks.detail(auth.getName(),id);
        return read(artifact.get("key"), kind, str(task,"outputFormat"), limit);
    }
    @GetMapping("/api/tasks/{id}/attempts/{attemptId}/preview")
    Object attemptPreview(Authentication auth, @PathVariable String id, @PathVariable String attemptId,
                          @RequestParam(defaultValue="result") String kind,
                          @RequestParam(defaultValue="100") int limit) {
        if (!java.util.Set.of("result","dirty").contains(kind)) throw ApiException.bad("仅支持结果和脏数据预览");
        if (limit<1 || limit>100) throw ApiException.bad("预览数量必须为1至100");
        var artifact=tasks.attemptArtifact(auth.getName(),id,attemptId,kind);
        var attempt=tasks.attempt(auth.getName(),id,attemptId);
        var task=tasks.detail(auth.getName(),id);
        if (!"SUCCEEDED".equals(str(attempt,"status"))) throw ApiException.conflict("该执行尝试尚未成功完成");
        return read(artifact.get("key"), kind, str(task,"outputFormat"), limit);
    }
    private Object read(String key, String kind, String outputFormat, int limit) {
        try(var pin=retention.pinArtifact(key)) {
            return python.artifactPreview(map("objectKey",key,"format","dirty".equals(kind)?"csv":outputFormat,"limit",limit));
        } catch(ApiException e) {throw e;} catch(Exception e) {throw new IllegalStateException("Cannot release artifact preview lease",e);}
    }
}
