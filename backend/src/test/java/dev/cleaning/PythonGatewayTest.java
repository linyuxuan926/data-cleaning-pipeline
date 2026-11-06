package dev.cleaning;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicReference;
import static org.assertj.core.api.Assertions.*;

class PythonGatewayTest {
    @Test void forwardsInternalTokenAndUsesAuthoritativeValidationErrors() throws Exception {
        HttpServer server=HttpServer.create(new InetSocketAddress("127.0.0.1",0),0);
        AtomicReference<String> received=new AtomicReference<>();
        server.createContext("/internal/validate-rules",exchange->{
            received.set(exchange.getRequestHeaders().getFirst("X-Internal-Token")+":"+new String(exchange.getRequestBody().readAllBytes(),StandardCharsets.UTF_8));
            byte[] body="{\"detail\":\"unknown or unsafe rule option\"}".getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type","application/json");exchange.sendResponseHeaders(422,body.length);exchange.getResponseBody().write(body);exchange.close();
        });
        server.start();
        try {
            var json=new Json(new ObjectMapper());var gateway=new PythonGateway("http://127.0.0.1:"+server.getAddress().getPort(),"internal-test-token",json);
            assertThatThrownBy(()->gateway.validateRules(json.read("[{\"type\":\"new_rule\"}]"))).isInstanceOfSatisfying(ApiException.class,e->assertThat(e.status.value()).isEqualTo(400)).hasMessage("unknown or unsafe rule option");
            assertThat(received.get()).startsWith("internal-test-token:").contains("\"rules\":[{\"type\":\"new_rule\"}]");
        } finally {server.stop(0);}
    }
}
