ARG FEDORA_VERSION=44
FROM fedora:${FEDORA_VERSION}

# Build/runtime tools for vibebuild and its dependencies
RUN dnf install -y \
        python3 python3-pip python3-devel \
        python3-setuptools python3-wheel \
        rpm rpm-build rpmdevtools bubblewrap \
        koji \
        gcc gcc-c++ make \
        git curl jq which findutils less procps-ng \
        python3-scikit-learn python3-numpy python3-scipy \
    && dnf clean all

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspace

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
