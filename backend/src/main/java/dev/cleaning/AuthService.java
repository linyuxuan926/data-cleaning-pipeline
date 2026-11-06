package dev.cleaning;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.HttpStatus;
import org.springframework.security.crypto.bcrypt.BCryptPasswordEncoder;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.*;
import static dev.cleaning.Database.*;

@Service
public class AuthService {
    private final Database db;
    private final BCryptPasswordEncoder encoder = new BCryptPasswordEncoder(12);
    private final SecureRandom random = new SecureRandom();
    private final boolean registration;
    public AuthService(Database db,@Value("${cleaning.registration-enabled:true}") boolean registration) { this.db=db; this.registration=registration; }
    public record Credentials(String username,String password) {}
    @Transactional
    public Map<String,Object> register(Credentials body) {
        if(!registration) throw new ApiException(HttpStatus.FORBIDDEN,"注册已关闭");
        validate(body);
        String id=UUID.randomUUID().toString();
        try { db.jdbc.update("INSERT INTO users(id,username,password_hash,created_at) VALUES(?,?,?,?)",id,body.username(),encoder.encode(body.password()),now()); }
        catch(org.springframework.dao.DuplicateKeyException e) { throw ApiException.conflict("用户名已存在"); }
        return session(id,body.username());
    }
    public Map<String,Object> login(Credentials body) {
        validate(body);
        var rows=db.jdbc.queryForList("SELECT * FROM users WHERE username=?",body.username());
        String hash=rows.isEmpty()?"$2a$12$8XDwYcYoiRCaeabYUB8XlehzNEnQZB3Wq1lnLvuCvskDy2WuT7vIi":str(rows.get(0),"password_hash");
        boolean match=encoder.matches(body.password(),hash);
        if(rows.isEmpty()||!match||!Boolean.TRUE.equals(rows.get(0).get("enabled"))) throw new ApiException(HttpStatus.UNAUTHORIZED,"用户名或密码错误，或用户已被禁用");
        return session(str(rows.get(0),"id"),str(rows.get(0),"username"));
    }
    void validate(Credentials c) {
        if(c==null) throw ApiException.bad("请求体不能为空");
        if(c.username()==null||!c.username().matches("[\\p{L}\\p{N}_-]{3,64}")) throw ApiException.bad("用户名需为3-64位字母、数字、下划线或连字符");
        if(c.password()==null||c.password().length()<10||c.password().getBytes(StandardCharsets.UTF_8).length>72) throw ApiException.bad("密码需至少10个字符且UTF-8编码不超过72字节");
    }
    private Map<String,Object> session(String id,String username) {
        byte[] bytes=new byte[32]; random.nextBytes(bytes);
        String token=Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
        db.jdbc.update("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",hash(token),id,Timestamp.from(Instant.now().plusSeconds(86400)));
        return map("token",token,"user",me(id));
    }
    public String authenticate(String token) {
        if(token==null||token.length()!=43) return null;
        var rows=db.jdbc.queryForList("SELECT s.user_id FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=TRUE",hash(token),now());
        return rows.isEmpty()?null:str(rows.get(0),"user_id");
    }
    public Map<String,Object> me(String id) {
        var u=db.one("SELECT id,username,role,enabled FROM users WHERE id=?",id);
        return map("id",str(u,"id"),"username",str(u,"username"),"role",str(u,"role"),"enabled",u.get("enabled"));
    }
    public boolean admin(String id) {return db.count("SELECT COUNT(*) FROM users WHERE id=? AND role='ADMIN' AND enabled=TRUE",id)==1;}
    @Transactional public void bootstrap(String username,String password) {
        if(username.isBlank()&&password.isBlank()) return;
        validate(new Credentials(username,password));
        db.one("SELECT id FROM platform_lock WHERE id=1 FOR UPDATE");
        var existing=db.jdbc.queryForList("SELECT role FROM users WHERE username=?",username);
        if(!existing.isEmpty()) {
            if(!"ADMIN".equals(str(existing.get(0),"role"))) throw new IllegalStateException("Bootstrap username belongs to a preexisting non-admin; refusing promotion");
            return;
        }
        db.jdbc.update("INSERT INTO users(id,username,password_hash,created_at,role,enabled) VALUES(?,?,?,?,'ADMIN',TRUE)",UUID.randomUUID().toString(),username,encoder.encode(password),now());
    }
    public void logout(String token) { db.jdbc.update("DELETE FROM sessions WHERE token_hash=?",hash(token)); }
    static String hash(String value) {
        try { return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8))); }
        catch(NoSuchAlgorithmException e) { throw new IllegalStateException(e); }
    }
}
