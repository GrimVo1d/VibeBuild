ARG FEDORA_VERSION=44
FROM fedora:${FEDORA_VERSION}

RUN dnf install -y \
    koji-builder \
    mock createrepo_c \
    rpm-build \
    && dnf clean all

RUN useradd -r kojibuilder 2>/dev/null || true && \
    usermod -aG mock kojibuilder

COPY kojid.conf /etc/kojid/kojid.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
