package dev.cleaning;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.springframework.stereotype.Component;

@Component
public class Json {
    private final ObjectMapper mapper;
    public Json(ObjectMapper mapper) { this.mapper = mapper; }
    public String write(Object value) {
        try { return mapper.writeValueAsString(value); }
        catch (Exception e) { throw new IllegalStateException("Cannot encode JSON", e); }
    }
    public JsonNode read(Object value) {
        try { return mapper.readTree(value.toString()); }
        catch (Exception e) { throw new IllegalStateException("Invalid persisted JSON", e); }
    }
}
