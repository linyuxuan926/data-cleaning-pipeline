package dev.cleaning;

import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;

@RestController
public class UploadProgressController {
    private final FileService files;
    public UploadProgressController(FileService files) {this.files=files;}
    @GetMapping("/api/uploads/{uploadId}")
    Object progress(Authentication auth,@PathVariable String uploadId) throws Exception {
        return files.uploadProgress(auth.getName(),uploadId);
    }
    @GetMapping("/api/files/{id}")
    Object file(Authentication auth,@PathVariable String id) {
        return files.view(files.owned(auth.getName(),id,false));
    }
}
