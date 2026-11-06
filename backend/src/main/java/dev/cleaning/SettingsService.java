package dev.cleaning;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.util.*;
import static dev.cleaning.Database.*;

@Service
public class SettingsService {
    static final Map<String,Long> CAPS=Map.ofEntries(Map.entry("maxFileBytes",104857600L),Map.entry("maxRows",500000L),Map.entry("maxColumns",200L),Map.entry("maxCells",5000000L),Map.entry("maxExcelExpandedBytes",536870912L),Map.entry("previewRows",1000L),Map.entry("maxActivePerUser",20L),Map.entry("maxActiveGlobal",100L),Map.entry("submissionsPerMinute",120L),Map.entry("maxRunningGlobal",8L),Map.entry("maxRunningPerUser",4L));
    private final Database db;private final Json json;
    public SettingsService(Database db,Json json) {this.db=db;this.json=json;}
    public JsonNode current() {var settings=(com.fasterxml.jackson.databind.node.ObjectNode)json.read(db.one("SELECT settings_json FROM system_settings WHERE id=1").get("settings_json"));if(!settings.has("maxRunningGlobal")) settings.put("maxRunningGlobal",2);if(!settings.has("maxRunningPerUser")) settings.put("maxRunningPerUser",1);return settings;}
    public JsonNode engineLimits() {
        var result=com.fasterxml.jackson.databind.node.JsonNodeFactory.instance.objectNode();var all=current();
        for(String key:List.of("maxFileBytes","maxRows","maxColumns","maxCells","maxExcelExpandedBytes","previewRows")) result.set(key,all.get(key));
        return result;
    }
    @Transactional public JsonNode update(JsonNode settings) {
        if(settings==null||!settings.isObject()) throw ApiException.bad("需要完整且无未知字段的系统配置");
        for(var fields=settings.fieldNames();fields.hasNext();) if(!CAPS.containsKey(fields.next())) throw ApiException.bad("未知系统配置");
        var normalized=(com.fasterxml.jackson.databind.node.ObjectNode)settings.deepCopy();
        for(String optional:List.of("maxRunningGlobal","maxRunningPerUser")) if(!normalized.has(optional)) normalized.set(optional,current().get(optional));
        settings=normalized;
        for(var entry:CAPS.entrySet()) {
            JsonNode value=settings.get(entry.getKey());
            if(value==null||!value.isIntegralNumber()||!value.canConvertToLong()||value.asLong()<1||value.asLong()>entry.getValue()) throw ApiException.bad(entry.getKey()+"范围为1-"+entry.getValue());
        }
        if(settings.get("maxActivePerUser").asLong()>settings.get("maxActiveGlobal").asLong()) throw ApiException.bad("用户并发不能超过全局并发");
        if(settings.get("maxRunningPerUser").asLong()>settings.get("maxRunningGlobal").asLong()) throw ApiException.bad("用户运行槽不能超过全局运行槽");
        db.one("SELECT id FROM platform_lock WHERE id=1 FOR UPDATE");
        db.jdbc.update("UPDATE system_settings SET settings_json=?,updated_at=? WHERE id=1",json.write(settings),now());return current();
    }
}
