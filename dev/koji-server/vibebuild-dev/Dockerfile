ARG FEDORA_VERSION=44
FROM fedora:${FEDORA_VERSION}

# Runtime and test tools; vibebuild itself runs from the read-only /workspace mount
RUN dnf install -y \
        python3 python3-requests python3-pytest python3-pytest-cov python3-pytest-mock \
        rpm rpm-build rpmdevtools bubblewrap \
        koji \
        git curl jq which findutils less procps-ng \
        python3-scikit-learn python3-numpy python3-scipy \
    && dnf clean all

# The client runs as an ordinary user, not root: whatever escapes rpm or a spec cannot
# write /proc/sys, which this container leaves unmasked for bubblewrap. VB_UID matches
# the host user, who owns the mounted certificate.
ARG VB_UID=1000
RUN useradd --uid "${VB_UID}" --create-home vibebuild \
    && mkdir -p /home/vibebuild/.cache/vibebuild /home/vibebuild/.koji \
    && chown -R vibebuild: /home/vibebuild \
    && printf '#!/bin/sh\nexec python3 -P -m vibebuild "$@"\n' > /usr/local/bin/vibebuild \
    && chmod 755 /usr/local/bin/vibebuild

# Nothing writes into the read-only checkout: no bytecode, no pytest cache, coverage in /tmp
ENV PYTHONPATH=/workspace \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTEST_ADDOPTS="-p no:cacheprovider" \
    COVERAGE_FILE=/tmp/.coverage

COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

USER vibebuild
WORKDIR /workspace

CMD ["/entrypoint.sh"]
