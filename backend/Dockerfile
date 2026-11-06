FROM maven:3.9.9-eclipse-temurin-17 AS build
WORKDIR /app
COPY pom.xml .
RUN mvn -B dependency:go-offline
COPY src ./src
RUN mvn -B verify
FROM eclipse-temurin:17.0.13_11-jre-jammy
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/* && groupadd -r cleaning && useradd -r -g cleaning cleaning && mkdir /app /tmp/uploads && chown cleaning:cleaning /app /tmp/uploads
WORKDIR /app
COPY --from=build /app/target/data-cleaning-backend-1.0.0.jar app.jar
USER cleaning
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=5s --start-period=45s CMD curl -fsS http://localhost:8080/actuator/health || exit 1
ENTRYPOINT ["java","-XX:MaxRAMPercentage=70","-jar","app.jar"]
