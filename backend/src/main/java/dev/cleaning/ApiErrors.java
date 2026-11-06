package dev.cleaning;

import org.slf4j.*;
import org.springframework.http.*;
import org.springframework.http.converter.HttpMessageNotReadableException;
import org.springframework.web.bind.annotation.*;
import org.springframework.web.method.annotation.MethodArgumentTypeMismatchException;
import java.util.Map;

@RestControllerAdvice
public class ApiErrors {
    private static final Logger log=LoggerFactory.getLogger(ApiErrors.class);
    @ExceptionHandler(ApiException.class) ResponseEntity<?> api(ApiException e) {return ResponseEntity.status(e.status).body(Map.of("message",e.getMessage(),"code",e.code,"requestId",RequestContext.id()));}
    @ExceptionHandler({HttpMessageNotReadableException.class,MethodArgumentTypeMismatchException.class,org.springframework.web.bind.MissingServletRequestParameterException.class})
    ResponseEntity<?> bad(Exception e) {return ResponseEntity.badRequest().body(Map.of("message","请求格式或参数无效","requestId",RequestContext.id()));}
    @ExceptionHandler(Exception.class) ResponseEntity<?> unexpected(Exception e) {
        log.error("Request failed",e);return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(Map.of("message","服务暂时无法完成请求，请稍后重试","requestId",RequestContext.id()));
    }
}
