package dev.cleaning;

import io.minio.*;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;
import java.io.*;
import java.nio.file.*;
import java.time.Instant;
import java.util.Iterator;

@Component
public class ObjectStorage {
    private final MinioClient client;
    private final String bucket;
    private volatile boolean initialized;
    public ObjectStorage(@Value("${cleaning.minio-endpoint}") String endpoint,@Value("${cleaning.minio-access-key}") String access,@Value("${cleaning.minio-secret-key}") String secret,@Value("${cleaning.minio-bucket}") String bucket) {
        client=MinioClient.builder().endpoint(endpoint).credentials(access,secret).build();this.bucket=bucket;
    }
    public synchronized void ensureBucket() throws Exception {
        if(initialized) return;
        if(!client.bucketExists(BucketExistsArgs.builder().bucket(bucket).build())) {
            try { client.makeBucket(MakeBucketArgs.builder().bucket(bucket).build()); }
            catch(Exception e) { if(!client.bucketExists(BucketExistsArgs.builder().bucket(bucket).build())) throw e; }
        }
        initialized=true;
    }
    public void put(String key,Path file,String type) throws Exception {
        ensureBucket();
        try(InputStream in=Files.newInputStream(file)) {
            client.putObject(PutObjectArgs.builder().bucket(bucket).object(key).stream(in,Files.size(file),-1).contentType(type).build());
        }
    }
    public InputStream get(String key) throws Exception {
        ensureBucket(); return client.getObject(GetObjectArgs.builder().bucket(bucket).object(key).build());
    }
    public void verify(String key) {
        try {
            ensureBucket();
            if(client.statObject(StatObjectArgs.builder().bucket(bucket).object(key).build()).size()<=0) throw new IllegalStateException("Empty artifact: "+key);
        } catch(Exception e) { throw new IllegalStateException("Cannot verify completed artifact",e); }
    }
    public boolean healthy() throws Exception { ensureBucket();return client.bucketExists(BucketExistsArgs.builder().bucket(bucket).build()); }
    public record StoredObject(String key,long size,Instant createdAt) {}
    public StoredObject metadata(String key) throws Exception {
        ensureBucket();var stat=client.statObject(StatObjectArgs.builder().bucket(bucket).object(key).build());
        return new StoredObject(key,stat.size(),stat.lastModified().toInstant());
    }
    public Iterable<StoredObject> objects() throws Exception {
        return objectsAfter(null);
    }
    public Iterable<StoredObject> objectsAfter(String afterObjectKey) throws Exception {
        ensureBucket();
        var builder=ListObjectsArgs.builder().bucket(bucket).recursive(true).maxKeys(1000);
        if(afterObjectKey!=null&&!afterObjectKey.isEmpty()) builder.startAfter(afterObjectKey);
        var source=client.listObjects(builder.build());
        return () -> new Iterator<>() {
            final Iterator<io.minio.Result<io.minio.messages.Item>> iterator=source.iterator();
            public boolean hasNext() {return iterator.hasNext();}
            public StoredObject next() {
                try {var item=iterator.next().get();return new StoredObject(item.objectName(),item.size(),item.lastModified().toInstant());}
                catch(Exception e) {throw new IllegalStateException("Cannot inspect object metadata",e);}
            }
        };
    }
    public void remove(String key) throws Exception {
        ensureBucket();client.removeObject(RemoveObjectArgs.builder().bucket(bucket).object(key).build());
    }
}
