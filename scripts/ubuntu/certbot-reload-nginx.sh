#!/usr/bin/env bash
set -Eeuo pipefail
# Install as /etc/letsencrypt/renewal-hooks/deploy/20-nginx-reload.
# Certbot runs this only after successful renewal; invalid nginx is never reloaded.
/usr/sbin/nginx -t
/usr/bin/systemctl reload nginx
