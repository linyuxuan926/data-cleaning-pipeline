package dev.cleaning;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Component;
import java.time.Instant;
import java.sql.Timestamp;
import java.util.*;

@Component
public class Database {
    public final JdbcTemplate jdbc;
    public Database(JdbcTemplate jdbc) { this.jdbc = jdbc; }
    public Map<String,Object> one(String sql, Object... args) {
        var rows = jdbc.queryForList(sql, args);
        if (rows.isEmpty()) throw ApiException.missing();
        return rows.get(0);
    }
    public long count(String sql, Object... args) { return Objects.requireNonNull(jdbc.queryForObject(sql, Long.class, args)); }
    public static Timestamp now() { return Timestamp.from(Instant.now()); }
    public static String str(Map<String,Object> row,String key) { return Objects.toString(row.get(key),null); }
    public static int num(Map<String,Object> row,String key) { return ((Number)row.get(key)).intValue(); }
    public static Map<String,Object> map(Object... pairs) {
        Map<String,Object> result = new LinkedHashMap<>();
        for(int i=0;i<pairs.length;i+=2) result.put((String)pairs[i],pairs[i+1]);
        return result;
    }
    public static String time(Map<String,Object> row,String key) {
        Object v=row.get(key);
        return v instanceof Timestamp t ? t.toInstant().toString() : v.toString();
    }
}
