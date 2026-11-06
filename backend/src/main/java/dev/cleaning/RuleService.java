package dev.cleaning;

import com.fasterxml.jackson.databind.JsonNode;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import java.util.*;
import java.util.regex.Pattern;
import static dev.cleaning.Database.*;

@Service
public class RuleService {
    private final Database db;private final Json json;private final PythonGateway python;
    public RuleService(Database db,Json json,PythonGateway python) { this.db=db;this.json=json;this.python=python; }
    public record Input(String name,JsonNode rules,Integer schemaVersion,String engineVersion,JsonNode logicalFields) {
        public Input(String name,JsonNode rules) {this(name,rules,null,null,null);}
    }
    public void validate(Input input) {
        if(input==null) throw ApiException.bad("请求体不能为空");
        if(input.name()==null||input.name().isBlank()||input.name().length()>100) throw ApiException.bad("规则集名称需为1-100个字符");
        if(input.schemaVersion()!=null&&input.schemaVersion()!=1&&input.schemaVersion()!=3) throw ApiException.bad("schemaVersion仅支持1和3");
        if((input.schemaVersion()==null||input.schemaVersion()==1)&&input.engineVersion()!=null&&!Set.of("v2-phased","v3-sequential").contains(input.engineVersion())) throw ApiException.bad("schema1规则仅支持v2-phased或v3-sequential引擎");
        if(input.schemaVersion()!=null&&input.schemaVersion()==3) {
            if(!"v3-disk".equals(input.engineVersion())||input.logicalFields()==null||!input.logicalFields().isArray()) throw ApiException.bad("schema3需要v3-disk和logicalFields");
            validateStructure(input.rules());python.validateRules(map("rules",input.rules(),"schemaVersion",3,"engineVersion","v3-disk","logicalFields",input.logicalFields()));
        } else validateRules(input.rules());
    }
    public void validateRules(JsonNode rules) {
        validateStructure(rules);python.validateRules(rules);
    }
    private void validateStructure(JsonNode rules) {
        if(rules==null||!rules.isArray()||rules.size()>50) throw ApiException.bad("规则必须为数组且最多50条");
        if(json.write(rules).getBytes(java.nio.charset.StandardCharsets.UTF_8).length>100000) throw ApiException.bad("规则配置不能超过100000字节");
        for(JsonNode rule:rules) {
            if(!rule.isObject()||!rule.path("type").isTextual()||rule.path("type").asText().isBlank()) throw ApiException.bad("每条规则必须包含type");
        }
    }
    @Transactional
    public Map<String,Object> create(String user,Input input) {
        validate(input);String id=UUID.randomUUID().toString();var at=now();
        db.jdbc.update("INSERT INTO rule_sets(id,user_id,name,current_version,created_at) VALUES(?,?,?,1,?)",id,user,input.name(),at);
        db.jdbc.update("INSERT INTO rule_versions(rule_set_id,version,name,rules_json,schema_version,engine_version,logical_fields_json,created_at) VALUES(?,1,?,?,?,?,?,?)",id,input.name(),json.write(input.rules()),input.schemaVersion()==null?1:input.schemaVersion(),input.engineVersion()==null?"v3-sequential":input.engineVersion(),input.logicalFields()==null?null:json.write(input.logicalFields()),at);
        return version(user,id,1);
    }
    @Transactional
    public Map<String,Object> update(String user,String id,Input input) {
        validate(input);var current=db.one("SELECT * FROM rule_sets WHERE id=? AND user_id=? FOR UPDATE",id,user);int version=num(current,"current_version")+1;
        int schema=input.schemaVersion()==null?1:input.schemaVersion();
        var previous=db.one("SELECT schema_version,engine_version FROM rule_versions WHERE rule_set_id=? AND version=?",id,num(current,"current_version"));
        if(num(previous,"schema_version")!=schema) throw ApiException.conflict("不同schema的规则须另存为新规则集，历史版本不自动转换");
        db.jdbc.update("INSERT INTO rule_versions(rule_set_id,version,name,rules_json,schema_version,engine_version,logical_fields_json,created_at) VALUES(?,?,?,?,?,?,?,?)",id,version,input.name(),json.write(input.rules()),schema,input.engineVersion()==null?"v3-sequential":input.engineVersion(),input.logicalFields()==null?null:json.write(input.logicalFields()),now());
        db.jdbc.update("UPDATE rule_sets SET name=?,current_version=? WHERE id=?",input.name(),version,id);
        return version(user,id,version);
    }
    public Map<String,Object> version(String user,String id,int version) {
        return view(db.one("SELECT v.* FROM rule_versions v JOIN rule_sets r ON r.id=v.rule_set_id WHERE r.user_id=? AND r.id=? AND v.version=?",user,id,version));
    }
    public List<Map<String,Object>> list(String user) { return db.jdbc.queryForList("SELECT v.* FROM rule_versions v JOIN rule_sets r ON r.id=v.rule_set_id AND r.current_version=v.version WHERE r.user_id=? ORDER BY r.created_at DESC LIMIT 200",user).stream().map(this::view).toList(); }
    public List<Map<String,Object>> versions(String user,String id) {
        db.one("SELECT id FROM rule_sets WHERE id=? AND user_id=?",id,user);
        return db.jdbc.queryForList("SELECT * FROM rule_versions WHERE rule_set_id=? ORDER BY version DESC LIMIT 200",id).stream().map(this::view).toList();
    }
    void versionOwner(String user,String id) {db.one("SELECT id FROM rule_sets WHERE id=? AND user_id=?",id,user);}
    Map<String,Object> view(Map<String,Object> r) { return map("id",str(r,"rule_set_id"),"name",str(r,"name"),"version",num(r,"version"),"rules",json.read(r.get("rules_json")),"schemaVersion",num(r,"schema_version"),"engineVersion",str(r,"engine_version"),"logicalFields",r.get("logical_fields_json")==null?List.of():json.read(r.get("logical_fields_json")),"createdAt",time(r,"created_at")); }
}
