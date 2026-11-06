package dev.cleaning;

import org.springframework.boot.actuate.health.*;
import org.springframework.stereotype.Component;

@Component("objectStorageHealthIndicator")
public class StorageHealth implements HealthIndicator {
    private final ObjectStorage storage;
    public StorageHealth(ObjectStorage storage) { this.storage=storage; }
    @Override public Health health() {
        try { return storage.healthy()?Health.up().build():Health.down().build(); }
        catch(Exception e) { return Health.down().build(); }
    }
}
