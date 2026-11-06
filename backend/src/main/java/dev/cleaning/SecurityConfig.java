package dev.cleaning;

import com.fasterxml.jackson.databind.ObjectMapper;
import jakarta.servlet.*;
import jakarta.servlet.http.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.*;
import org.springframework.http.HttpMethod;
import org.springframework.security.authentication.UsernamePasswordAuthenticationToken;
import org.springframework.security.config.annotation.web.builders.HttpSecurity;
import org.springframework.security.config.http.SessionCreationPolicy;
import org.springframework.security.core.context.SecurityContextHolder;
import org.springframework.security.web.SecurityFilterChain;
import org.springframework.security.web.authentication.UsernamePasswordAuthenticationFilter;
import org.springframework.web.filter.OncePerRequestFilter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

@Configuration
public class SecurityConfig {
    @Bean SecurityFilterChain security(HttpSecurity http,AuthService auth,ObjectMapper mapper,@Value("${cleaning.internal-token}") String internalToken) throws Exception {
        if(internalToken.length()<32) throw new IllegalStateException("INTERNAL_API_TOKEN must have at least 32 characters");
        var filter=new OncePerRequestFilter() {
            final Map<String,long[]> authWindows=new ConcurrentHashMap<>();
            @Override protected void doFilterInternal(HttpServletRequest req,HttpServletResponse res,FilterChain chain) throws ServletException,IOException {
                String path=req.getRequestURI();
                long max=path.matches("/api/(files|uploads)/[^/]+/chunks/\\d+")?8L*1024*1024:3L*1024*1024;
                if(req.getContentLengthLong()>max) { error(res,413,"请求体过大"); return; }
                if(path.startsWith("/internal/")) {
                    String token=req.getHeader("X-Internal-Token");
                    if(token==null||!MessageDigest.isEqual(internalToken.getBytes(StandardCharsets.UTF_8),token.getBytes(StandardCharsets.UTF_8))) { error(res,401,"内部认证失败"); return; }
                } else if(path.equals("/api/auth/login")||path.equals("/api/auth/register")) {
                    String key=req.getRemoteAddr(); long minute=Instant.now().getEpochSecond()/60;
                    synchronized(authWindows) {
                        if(authWindows.size()>=10000) authWindows.entrySet().removeIf(e->e.getValue()[0]<minute);
                        if(authWindows.size()>=10000&&!authWindows.containsKey(key)) { error(res,429,"认证请求过于频繁，请稍后重试"); return; }
                        long[] counter=authWindows.computeIfAbsent(key,k->new long[]{minute,0});
                        if(counter[0]!=minute) { counter[0]=minute;counter[1]=0; }
                        if(++counter[1]>30) { error(res,429,"认证请求过于频繁，请稍后重试"); return; }
                    }
                } else {
                    String header=req.getHeader("Authorization");
                    if(header!=null&&header.startsWith("Bearer ")) {
                        String user=auth.authenticate(header.substring(7));
                        if(user!=null) SecurityContextHolder.getContext().setAuthentication(new UsernamePasswordAuthenticationToken(user,null,auth.admin(user)?List.of(new org.springframework.security.core.authority.SimpleGrantedAuthority("ROLE_ADMIN")):List.of(new org.springframework.security.core.authority.SimpleGrantedAuthority("ROLE_USER"))));
                    }
                }
                chain.doFilter(req,res);
            }
            void error(HttpServletResponse res,int status,String message) throws IOException { res.setStatus(status);res.setContentType("application/json;charset=UTF-8");mapper.writeValue(res.getOutputStream(),Map.of("message",message)); }
        };
        return http.csrf(c->c.disable()).cors(c->c.disable())
            .sessionManagement(c->c.sessionCreationPolicy(SessionCreationPolicy.STATELESS))
            .authorizeHttpRequests(c->c.requestMatchers(HttpMethod.POST,"/api/auth/register","/api/auth/login").permitAll()
                .requestMatchers("/actuator/health","/internal/**").permitAll().requestMatchers("/api/admin/**").hasRole("ADMIN").anyRequest().authenticated())
            .exceptionHandling(c->c.authenticationEntryPoint((req,res,e)-> {res.setStatus(401);res.setContentType("application/json;charset=UTF-8");mapper.writeValue(res.getOutputStream(),Map.of("message","请登录后继续"));}).accessDeniedHandler((req,res,e)->{res.setStatus(403);res.setContentType("application/json;charset=UTF-8");mapper.writeValue(res.getOutputStream(),Map.of("message","需要管理员权限"));}))
            .addFilterBefore(filter,UsernamePasswordAuthenticationFilter.class).build();
    }
}
