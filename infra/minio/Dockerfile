# Official old Docker images and release binaries are no longer publicly
# available. Build the unmodified upstream AGPL source at an immutable commit.
FROM golang:1.23.2-bookworm AS build
WORKDIR /src
ADD --checksum=sha256:48868ef201be0b2f5a7378e66a6834d2a19dbef39ef0fecf649ea4b505834114 https://codeload.github.com/minio/minio/tar.gz/d10bb7e1b667c2df72c394ef1fa52ab4a6802d0f /tmp/minio.tar.gz
RUN tar -xzf /tmp/minio.tar.gz --strip-components=1 -C /src && rm /tmp/minio.tar.gz
RUN go mod download
RUN CGO_ENABLED=0 go build -trimpath -ldflags='-s -w -X github.com/minio/minio/cmd.Version=2024-10-13T13:34:11Z -X github.com/minio/minio/cmd.ReleaseTag=RELEASE.2024-10-13T13-34-11Z -X github.com/minio/minio/cmd.CommitID=d10bb7e1b667c2df72c394ef1fa52ab4a6802d0f' -o /out/minio .

FROM debian:bookworm-20241016-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl && rm -rf /var/lib/apt/lists/* && useradd --uid 10001 --create-home minio && mkdir /data && chown minio:minio /data
COPY --from=build /out/minio /usr/local/bin/minio
COPY --from=build /src/LICENSE /usr/share/doc/minio/LICENSE
USER minio
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/data", "--console-address", ":9001"]
