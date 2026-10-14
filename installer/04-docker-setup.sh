#!/usr/bin/env bash
# Docker Engine + compose plugin + runtime dependency install/verify.

docker_daemon_ready() {
    docker info >/dev/null 2>&1
}

wait_for_docker_daemon() {
    local attempt
    for ((attempt = 1; attempt <= 10; attempt++)); do
        docker_daemon_ready && return 0
        sleep 2
    done
    return 1
}

start_docker_daemon() {
    log "Docker CLI is installed, but the daemon is not ready; recovering systemd socket/service startup..."
    systemctl daemon-reload || {
        echo "[ERROR] Could not reload systemd while recovering Docker." >&2
        return 1
    }
    systemctl unmask docker.service || {
        echo "[ERROR] Could not unmask docker.service." >&2
        return 1
    }

    # The official Docker service uses -H fd:// and requires docker.socket.
    # Starting/enabling the socket first repairs package transitions where
    # Ubuntu's docker.io installation left the socket unit disabled or masked.
    # Some existing installations use a non-socket-activated service, so only
    # manage the socket when systemd confirms that the unit is installed.
    if systemctl cat docker.socket >/dev/null 2>&1; then
        systemctl unmask docker.socket || {
            echo "[ERROR] Could not unmask docker.socket." >&2
            return 1
        }
        systemctl enable --now docker.socket || {
            echo "[ERROR] Could not enable/start docker.socket." >&2
            return 1
        }
    fi

    systemctl reset-failed docker.service >/dev/null 2>&1 || true
    systemctl enable --now docker.service || {
        echo "[ERROR] Could not enable/start docker.service." >&2
        return 1
    }
    if ! wait_for_docker_daemon; then
        echo "[ERROR] Docker service started, but 'docker info' did not become ready after 10 attempts; inspect systemctl status docker docker.socket and journalctl -u docker." >&2
        return 1
    fi
}

ensure_docker_daemon_ready() {
    if docker_daemon_ready; then
        # Keep an already-working daemon enabled across reboot without
        # interrupting its containers or restarting it.
        systemctl enable docker.service || {
            echo "[ERROR] Docker responds, but systemd could not enable docker.service." >&2
            return 1
        }
        return 0
    fi

    start_docker_daemon
}

docker_setup() {
    # rsync is required by exact payload synchronization and is not guaranteed
    # on a clean Ubuntu 24.04 image. Install base dependencies even when Docker
    # already exists so repair/reinstall never depends on an incidental package.
    log "Ensuring installer runtime dependencies are present..."
    apt-get update -y
    apt-get install -y ca-certificates curl gnupg rsync

    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
        log "Docker + compose plugin already present: $(docker --version)"
    else
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
    fi

    ensure_docker_daemon_ready
    log "Docker daemon is ready: $(docker --version)"
}
