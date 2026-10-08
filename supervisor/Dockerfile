FROM golang:1.27.0-bookworm AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY schemas ./schemas
COPY supervisor/cmd ./supervisor/cmd
COPY supervisor/internal ./supervisor/internal
COPY pyproject.toml ./
RUN version="$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml)" \
    && test -n "$version" \
    && CGO_ENABLED=0 go build -trimpath -ldflags "-s -w -X main.version=$version" -o /pig-supervisor ./supervisor/cmd/pig-supervisor

FROM scratch
COPY --from=build /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
COPY --from=build /pig-supervisor /pig-supervisor
USER 65532:65532
ENTRYPOINT ["/pig-supervisor"]
