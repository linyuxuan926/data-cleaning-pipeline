package dev.cleaning;

import org.slf4j.*;
import org.springframework.amqp.core.*;
import org.springframework.amqp.rabbit.connection.CorrelationData;
import org.springframework.amqp.rabbit.core.RabbitTemplate;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.*;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.nio.charset.StandardCharsets;
import java.util.Objects;
import java.util.concurrent.TimeUnit;
import static dev.cleaning.Database.*;

@Configuration
public class Messaging {
    @Bean DirectExchange cleaningExchange() { return new DirectExchange("cleaning",true,false); }
    @Bean DirectExchange deadExchange() { return new DirectExchange("cleaning.dlx",true,false); }
    @Bean Queue cleaningQueue() { return QueueBuilder.durable("cleaning.tasks").deadLetterExchange("cleaning.dlx").deadLetterRoutingKey("dead").build(); }
    @Bean Queue deadQueue() { return QueueBuilder.durable("cleaning.dead").build(); }
    @Bean Binding cleaningBinding() { return BindingBuilder.bind(cleaningQueue()).to(cleaningExchange()).with("task"); }
    @Bean Binding deadBinding() { return BindingBuilder.bind(deadQueue()).to(deadExchange()).with("dead"); }
}

@Component
@ConditionalOnProperty(name="cleaning.scheduler-enabled",havingValue="true",matchIfMissing=true)
class Maintenance {
    private final Database db;private final RabbitTemplate rabbit;private final TaskService tasks;private final FileService files;private final ObjectStorage storage;private final Json json;
    private static final Logger log=LoggerFactory.getLogger(Maintenance.class);
    Maintenance(Database db,RabbitTemplate rabbit,TaskService tasks,FileService files,ObjectStorage storage,Json json) { this.db=db;this.rabbit=rabbit;this.tasks=tasks;this.files=files;this.storage=storage;this.json=json; }
    @Scheduled(fixedDelay=2000,initialDelay=3000)
    void publish() {
        var pending=db.jdbc.queryForList("SELECT o.*,t.status,t.attempt AS current_attempt,a.attempt_id AS current_attempt_id,a.engine_version AS attempt_engine_version FROM outbox o JOIN tasks t ON t.id=o.task_id JOIN task_attempts a ON a.task_id=o.task_id AND a.attempt_no=t.attempt WHERE o.published=FALSE ORDER BY o.id LIMIT 20");
        for(var entry:pending) {
            if(!"QUEUED".equals(str(entry,"status"))||num(entry,"attempt")!=num(entry,"current_attempt")||!Objects.equals(str(entry,"attempt_id"),str(entry,"current_attempt_id"))) {
                db.jdbc.update("UPDATE outbox SET published=TRUE WHERE id=?",entry.get("id"));continue;
            }
            try {
                MessageProperties props=new MessageProperties();props.setContentType("application/json");props.setContentEncoding("UTF-8");props.setDeliveryMode(MessageDeliveryMode.PERSISTENT);props.setMessageId(str(entry,"task_id")+":"+str(entry,"attempt_id"));
                CorrelationData correlation=new CorrelationData(str(entry,"id"));
                var body=json.read(entry.get("payload"));
                if(!(body instanceof ObjectNode object)) throw new IllegalStateException("Outbox payload is not an object");
                object.put("attemptId",str(entry,"attempt_id"));object.put("engineVersion",str(entry,"attempt_engine_version"));
                rabbit.send("cleaning","task",new Message(json.write(object).getBytes(StandardCharsets.UTF_8),props),correlation);
                var confirm=correlation.getFuture().get(5,TimeUnit.SECONDS);
                if(!confirm.isAck()||correlation.getReturned()!=null) throw new IllegalStateException("Message was not durably routed");
                db.jdbc.update("UPDATE outbox SET published=TRUE WHERE id=?",entry.get("id"));
            } catch(Exception e) { log.warn("Outbox publish failed for {}: {}",entry.get("id"),e.toString());break; }
        }
    }
    @Scheduled(fixedDelay=30000,initialDelay=15000) void recover() { tasks.recover(); }
    @Scheduled(fixedDelay=3600000,initialDelay=60000) void cleanup() { try { files.cleanupExpired(); } catch(Exception e) { log.warn("Upload cleanup failed",e); } }
    @Scheduled(fixedDelay=30000,initialDelay=1000) void bucket() { try { storage.ensureBucket(); } catch(Exception e) { log.warn("Object store unavailable: {}",e.toString()); } }
}
