package dev.cleaning;

import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;
import java.util.Map;

@RestController
public class PaginationController {
    private final PaginationService pages;
    public PaginationController(PaginationService pages) {this.pages=pages;}
    @GetMapping("/api/files/page") Object files(Authentication a,@RequestParam Map<String,String> q) {return pages.page(a.getName(),"files",null,null,q);}
    @GetMapping("/api/uploads/page") Object uploads(Authentication a,@RequestParam Map<String,String> q) {return pages.page(a.getName(),"uploads",null,null,q);}
    @GetMapping("/api/rule-sets/page") Object rules(Authentication a,@RequestParam Map<String,String> q) {return pages.page(a.getName(),"rules",null,null,q);}
    @GetMapping("/api/rule-sets/{id}/versions/page") Object versions(Authentication a,@PathVariable String id,@RequestParam Map<String,String> q) {return pages.page(a.getName(),"versions",id,null,q);}
    @GetMapping("/api/tasks/page") Object tasks(Authentication a,@RequestParam Map<String,String> q) {return pages.page(a.getName(),"tasks",null,null,q);}
    @GetMapping("/api/reports/page") Object reports(Authentication a,@RequestParam Map<String,String> q) {return pages.page(a.getName(),"reports",null,null,q);}
    @GetMapping("/api/tasks/{id}/attempts/page") Object attempts(Authentication a,@PathVariable String id,@RequestParam Map<String,String> q) {return pages.page(a.getName(),"attempts",id,null,q);}
    @GetMapping("/api/tasks/{id}/logs/page") Object logs(Authentication a,@PathVariable String id,@RequestParam Map<String,String> q) {return pages.page(a.getName(),"logs",id,null,q);}
    @GetMapping("/api/tasks/{id}/attempts/{attemptId}/logs/page") Object attemptLogs(Authentication a,@PathVariable String id,@PathVariable String attemptId,@RequestParam Map<String,String> q) {return pages.page(a.getName(),"logs",id,attemptId,q);}
    @GetMapping("/api/admin/users/page") Object users(Authentication a,@RequestParam Map<String,String> q) {return pages.page(a.getName(),"admin-users",null,null,q);}
    @GetMapping("/api/admin/tasks/page") Object allTasks(Authentication a,@RequestParam Map<String,String> q) {return pages.page(a.getName(),"admin-tasks",null,null,q);}
}
