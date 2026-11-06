package dev.cleaning;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.*;
import org.springframework.stereotype.Component;

@Component
public class AdminBootstrap implements ApplicationRunner {
    private final AuthService auth;private final String username,password;
    public AdminBootstrap(AuthService auth,@Value("${BOOTSTRAP_ADMIN_USERNAME:}") String username,@Value("${BOOTSTRAP_ADMIN_PASSWORD:}") String password) {this.auth=auth;this.username=username;this.password=password;}
    @Override public void run(ApplicationArguments args) {auth.bootstrap(username,password);}
}
