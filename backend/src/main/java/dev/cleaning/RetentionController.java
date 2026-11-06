package dev.cleaning;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.security.core.Authentication;
import org.springframework.web.bind.annotation.*;
import java.util.Map;

@RestController
@RequestMapping("/api/admin/retention")
public class RetentionController {
    private final RetentionService service;private final PaginationService pages;
    public RetentionController(RetentionService service,PaginationService pages) {this.service=service;this.pages=pages;}
    @GetMapping("/policy") Object policy(Authentication a) {return service.policy(a.getName());}
    @PutMapping("/policy") Object policy(Authentication a,@RequestBody JsonNode body) {return service.update(a.getName(),body);}
    @PostMapping("/plans") Object plan(Authentication a,@RequestBody(required=false) RetentionService.Scope scope) throws Exception {return service.plan(a.getName(),scope);}
    @GetMapping("/plans/{id}") Object detail(Authentication a,@PathVariable String id) {return service.detail(a.getName(),id);}
    @PostMapping("/plans/{id}/execute") Object execute(Authentication a,@PathVariable String id) {return service.execute(a.getName(),id,false);}
    @PostMapping("/plans/{id}/retry") Object retry(Authentication a,@PathVariable String id) {return service.execute(a.getName(),id,true);}
    @GetMapping("/plans/page") Object plans(Authentication a,@RequestParam Map<String,String> query) {return pages.page(a.getName(),"admin-retention-plans",null,null,query);}
    @GetMapping("/runs/page") Object runs(Authentication a,@RequestParam Map<String,String> query) {return pages.page(a.getName(),"admin-retention-runs",null,null,query);}
}
