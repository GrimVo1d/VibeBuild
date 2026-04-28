FROM fedora:42

RUN dnf install -y \
    koji-hub koji-web \
    httpd mod_ssl mod_wsgi \
    postgresql python3-psycopg2 \
    curl \
    && dnf clean all

RUN mkdir -p /mnt/koji/{packages,repos,work,scratch} && \
    chown -R apache:apache /mnt/koji

COPY hub.conf /etc/koji-hub/hub.conf
COPY kojiweb.conf /etc/kojiweb/web.conf
COPY httpd-koji.conf /etc/httpd/conf.d/kojihub.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p /root/.koji && printf '[koji]\nserver = https://localhost/kojihub\ncert = /etc/pki/koji/kojiadmin.pem\nserverca = /etc/pki/koji/koji_ca_cert.crt\nauthtype = ssl\n' > /root/.koji/config

RUN sed -i 's/^Listen 443/#Listen 443/' /etc/httpd/conf.d/ssl.conf && \
    sed -i '/<VirtualHost _default_:443>/,/<\/VirtualHost>/d' /etc/httpd/conf.d/ssl.conf

EXPOSE 443

CMD ["/entrypoint.sh"]
