package dev.cleaning;

import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.util.*;
import static dev.cleaning.Database.*;

@Service
public class AdminService {
    private final Database db;private final AuthService auth;
    public AdminService(Database db,AuthService auth) {this.db=db;this.auth=auth;}
    public List<Map<String,Object>> users() {
        return db.jdbc.queryForList("SELECT * FROM users ORDER BY created_at DESC LIMIT 500").stream().map(u->map("id",str(u,"id"),"username",str(u,"username"),"role",str(u,"role"),"enabled",u.get("enabled"),"createdAt",time(u,"created_at"))).toList();
    }
    public record Update(String role,Boolean enabled) {}
    @Transactional public Map<String,Object> update(String id,Update input) {
        if(input==null||input.role()==null||!Set.of("ADMIN","USER").contains(input.role())||input.enabled()==null) throw ApiException.bad("role与enabled必填且必须有效");
        db.one("SELECT id FROM platform_lock WHERE id=1 FOR UPDATE");
        var current=db.one("SELECT * FROM users WHERE id=? FOR UPDATE",id);
        if("ADMIN".equals(str(current,"role"))&&Boolean.TRUE.equals(current.get("enabled"))&&(!"ADMIN".equals(input.role())||!input.enabled())&&db.count("SELECT COUNT(*) FROM users WHERE role='ADMIN' AND enabled=TRUE")<=1) throw ApiException.conflict("不能禁用或降级最后一位有效管理员");
        db.jdbc.update("UPDATE users SET role=?,enabled=? WHERE id=?",input.role(),input.enabled(),id);
        if(!input.enabled()) db.jdbc.update("DELETE FROM sessions WHERE user_id=?",id);
        return auth.me(id);
    }
}
