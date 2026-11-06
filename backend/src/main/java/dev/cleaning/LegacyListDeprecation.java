package dev.cleaning;

import jakarta.servlet.http.*;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.*;
import org.springframework.web.servlet.config.annotation.*;

@Configuration
public class LegacyListDeprecation implements WebMvcConfigurer {
    @Override public void addInterceptors(InterceptorRegistry registry) {registry.addInterceptor(new HandlerInterceptor() {
        @Override public boolean preHandle(HttpServletRequest request,HttpServletResponse response,Object handler) {
            String path=request.getRequestURI();
            if("GET".equals(request.getMethod())&&(path.matches("/api/(files|rule-sets|tasks)")||path.matches("/api/admin/(users|tasks)")||path.matches("/api/rule-sets/[^/]+/versions")||path.matches("/api/tasks/[^/]+/(attempts|logs)")||path.matches("/api/tasks/[^/]+/attempts/[^/]+/logs"))) {
                response.setHeader("Deprecation","true");response.setHeader("Link","<"+path+"/page>; rel=\"successor-version\"");
            }
            return true;
        }
    });}
}
