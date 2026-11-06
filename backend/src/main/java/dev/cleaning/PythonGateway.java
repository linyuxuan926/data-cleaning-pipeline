package dev.cleaning;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.*;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.*;
import static dev.cleaning.Database.*;

@Component
public class PythonGateway {
    private final RestClient client;private final Json json;
    public PythonGateway(@Value("${cleaning.python-api-url}") String url,@Value("${cleaning.internal-token}") String token,Json json) {
        this.json=json;var factory=new SimpleClientHttpRequestFactory();factory.setConnectTimeout(5000);factory.setReadTimeout(90000);
        client=RestClient.builder().baseUrl(url).defaultHeader("X-Internal-Token",token).requestFactory(factory).build();
    }
    public JsonNode preview(Object request) {return post("/internal/preview",request);}
    public JsonNode artifactPreview(Object request) {return post("/internal/artifact-preview",request);}
    public void validateRules(JsonNode rules) {post("/internal/validate-rules",map("rules",rules));}
    public void validateRules(Object request) {post("/internal/validate-rules",request);}
    public JsonNode preflight(Object request) {return post("/internal/preflight",request);}
    public JsonNode ruleSchema(int version) {
        try {return client.get().uri("/internal/rule-schemas/"+version).header("X-Request-ID",RequestContext.id()).retrieve().body(JsonNode.class);}
        catch(RestClientException e) {throw new ApiException(HttpStatus.BAD_GATEWAY,"规则Schema服务暂不可用");}
    }
    private JsonNode post(String path,Object request) {
        try {JsonNode response=client.post().uri(path).header("X-Request-ID",RequestContext.id()).body(request).retrieve().body(JsonNode.class);if(response==null) throw new RestClientException("Empty Python response");return response;}
        catch(HttpClientErrorException e) {
            if(e.getStatusCode().value()==400||e.getStatusCode().value()==422) {
                String detail="数据或规则校验失败";
                try {var error=json.read(e.getResponseBodyAsString());JsonNode item=error.has("detail")?error.get("detail"):error.get("message");if(item!=null) detail=item.isTextual()?item.asText():item.toString();} catch(Exception ignored) {}
                throw ApiException.bad(detail.length()>1800?detail.substring(0,1800):detail);
            }
            throw new ApiException(HttpStatus.BAD_GATEWAY,"数据处理服务暂不可用");
        } catch(RestClientException e) {throw new ApiException(HttpStatus.BAD_GATEWAY,"数据处理服务暂不可用");}
    }
}
