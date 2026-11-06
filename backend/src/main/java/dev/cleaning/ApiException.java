package dev.cleaning;

import org.springframework.http.HttpStatus;

public class ApiException extends RuntimeException {
    public final HttpStatus status;
    public final String code;
    public ApiException(HttpStatus status, String message) { this(status,message,status==HttpStatus.GONE?(message.startsWith("INPUT_EXPIRED")?"INPUT_EXPIRED":"ARTIFACT_EXPIRED"):status.name()); }
    public ApiException(HttpStatus status,String message,String code) {super(message);this.status=status;this.code=code;}
    static ApiException bad(String message) { return new ApiException(HttpStatus.BAD_REQUEST, message); }
    static ApiException conflict(String message) { return new ApiException(HttpStatus.CONFLICT, message); }
    static ApiException missing() { return new ApiException(HttpStatus.NOT_FOUND, "资源不存在"); }
}
