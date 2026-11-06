package dev.cleaning;

import jakarta.servlet.*;
import jakarta.servlet.http.*;
import org.slf4j.MDC;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;
import org.springframework.web.filter.OncePerRequestFilter;
import java.io.IOException;
import java.util.UUID;

@Component
@Order(Ordered.HIGHEST_PRECEDENCE)
public class RequestContext extends OncePerRequestFilter {
    static String id() {String value=MDC.get("requestId");return value==null?UUID.randomUUID().toString():value;}
    @Override protected void doFilterInternal(HttpServletRequest request,HttpServletResponse response,FilterChain chain) throws ServletException,IOException {
        String incoming=request.getHeader("X-Request-ID");
        String id=incoming!=null&&incoming.matches("[A-Za-z0-9._:-]{1,128}")?incoming:UUID.randomUUID().toString();
        MDC.put("requestId",id);response.setHeader("X-Request-ID",id);
        try {chain.doFilter(request,response);} finally {MDC.remove("requestId");}
    }
}
