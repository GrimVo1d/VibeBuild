FROM fedora:42

# Build/runtime tools for vibebuild and its dependencies
RUN dnf install -y \
        python3 python3-pip python3-devel \
        python3-setuptools python3-wheel \
        rpm rpm-build rpmdevtools \
        koji \
        gcc gcc-c++ make \
        git curl jq which findutils less procps-ng \
        python3-scikit-learn python3-joblib python3-numpy python3-scipy \
    && dnf clean all

# Pick up the system-installed sklearn/joblib so as not to rebuild them via pip
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspace

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
