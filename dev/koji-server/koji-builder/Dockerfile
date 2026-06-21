ARG FEDORA_VERSION=44
FROM fedora:${FEDORA_VERSION}

RUN dnf install -y \
    koji-builder \
    mock createrepo_c \
    rpm-build \
    && dnf clean all

RUN useradd -r kojibuilder 2>/dev/null || true && \
    usermod -aG mock kojibuilder

# Trust our self-signed CA *inside* mock chroots: put it into
# ssl_extra_certs via site-defaults so that dnf inside the chroot can download
# repomd.xml from koji-hub without SSL errors.
RUN printf "%s\n" \
    "import os" \
    "_ca = '/etc/pki/koji/koji_ca_cert.crt'" \
    "if os.path.exists(_ca):" \
    "    config_opts['ssl_extra_certs'] = _ca" \
    >> /etc/mock/site-defaults.cfg

# Compatibility of kojid (passes `-r koji/<name>`) with mock 6.7+, which no longer
# understands that form and fails with IsADirectoryError on '/'.
# /usr/bin/mock is a consolehelper symlink and must not be broken
# (kojibuilder from the mock group gets root privileges through PAM precisely
# via consolehelper). So we patch the mock Python script itself at the very
# beginning so that it rewrites `-r koji/<name>` into the modern
# `--configdir /etc/mock/koji -r <name>`.
RUN python3 -c "import sys, pathlib; p = pathlib.Path('/usr/libexec/mock/mock'); \
src = p.read_text(); \
shim = '''\n\
# --- KOJI COMPAT SHIM (added in Dockerfile) ---------------------------------\n\
# mock 6.7+ no longer understands '-r koji/<name>' (fails with IsADirectoryError on '/').\n\
# kojid still calls it that way. Translate to '--configdir /etc/mock/koji -r <name>'.\n\
import sys as _sys, os as _os\n\
_a = _sys.argv\n\
_i = 1\n\
while _i < len(_a) - 1:\n\
    if _a[_i] == '-r' and _a[_i+1].startswith('koji/'):\n\
        _short = _a[_i+1].split('/', 1)[1]\n\
        _a[_i:_i+2] = ['--configdir', '/etc/mock/koji', '-r', _short]\n\
        break\n\
    _i += 1\n\
del _a, _i\n\
# --- end KOJI COMPAT SHIM ---------------------------------------------------\n\
'''; \
marker = 'import sys'; \
idx = src.index(marker); \
p.write_text(src[:idx] + shim + src[idx:])"

COPY kojid.conf /etc/kojid/kojid.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
