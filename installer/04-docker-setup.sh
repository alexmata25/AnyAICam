#!/usr/bin/env bash
# Docker Engine + compose plugin + runtime dependency install/verify.

docker_setup() {
    # rsync is required by exact payload synchronization and is not guaranteed
    # on a clean Ubuntu 24.04 image. Install base dependencies even when Docker
    # already exists so repair/reinstall never depends on an incidental package.
    log "Ensuring installer runtime dependencies are present..."
    apt-get update -y
    apt-get install -y ca-certificates curl gnupg rsync

    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        log "Docker + compose plugin already present: $(docker --version)"
        return 0
    fi

    log "Installing Docker Engine + compose plugin..."
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    local codename arch
    codename="$(parse_kv_line VERSION_CODENAME </etc/os-release | tr -d '"')"
    arch="$(dpkg --print-architecture)"
    echo "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $codename stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -y
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    systemctl enable --now docker
    log "Docker installed: $(docker --version)"
}
