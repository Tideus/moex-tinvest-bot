#!/usr/bin/env bash
# Installs a separately verified checkout; never restarts or changes trading services.
set -Eeuo pipefail
umask 027
[[ $EUID -eq 0 ]] || { echo 'Run as root'; exit 2; }
source_dir="$(cd "$(dirname "$0")/../.." && pwd)"
prefix="${DESTDIR:-}"
[[ -z "$prefix" || ( "$prefix" == /* && "$prefix" != / ) ]] || { echo 'Invalid DESTDIR'; exit 2; }
target="$prefix/opt/moex-trader-journal"
state="$prefix/var/lib/moex-trader-journal"
settings="$prefix/etc/moex-trader-journal"
getent passwd moexbot >/dev/null
[[ -n "$prefix" ]] || command -v nginx >/dev/null
command -v openssl >/dev/null
# No overwrite of credentials or config. Re-running updates only isolated application/static files.
install -d -m 0755 "$target" "$target/src"
install -d -o root -g www-data -m 0750 "$settings"
install -d -o moexbot -g www-data -m 0750 "$state"
install -d -o moexbot -g www-data -m 2750 "$state/public"
install -d -o moexbot -g moexbot -m 0700 "$state/receipts"
cp -a "$source_dir/src/moex_bot" "$target/src/"
chown -R root:root "$target/src"
chmod -R a+rX,go-w "$target/src"
install -m 0644 "$source_dir/deploy/trader-journal/"{index.html,journal.js,journal-model.js,journal.css} "$state/public/"
if [[ ! -f "$settings/config.json" ]]; then
  install -m 0644 "$source_dir/deploy/trader-journal/config.example.json" "$settings/config.json"
fi
# Collector needs its own config, but cannot read the web password file.
chmod 0755 "$settings"
if [[ ! -f "$settings/htpasswd" ]]; then
  password="$(openssl rand -hex 20)"
  printf 'journal:%s\n' "$(printf '%s\n' "$password" | openssl passwd -6 -stdin)" > "$settings/htpasswd"
  printf 'URL: https://breakwood.ru/journal/\nUsername: journal\nPassword: %s\n' "$password" > "$settings/access.txt"
  chmod 0600 "$settings/access.txt"
  unset password
fi
chown root:www-data "$settings/htpasswd"
chmod 0640 "$settings/htpasswd"
install -d -m 0755 "$prefix/etc/nginx/snippets" "$prefix/etc/systemd/system"
install -m 0644 "$source_dir/deploy/trader-journal/nginx.conf" "$prefix/etc/nginx/snippets/moex-trader-journal.conf"
install -m 0644 "$source_dir/deploy/trader-journal/moex-trader-journal."{service,timer} "$prefix/etc/systemd/system/"
(cd "$target" && sha256sum src/moex_bot/trader_journal.py src/moex_bot/journal_import.py) > "$target/SHA256SUMS"
if [[ -n "$prefix" ]]; then
  echo 'Staged isolated journal; no systemd/nginx changes.'
  exit 0
fi
systemd-analyze verify "$prefix/etc/systemd/system/moex-trader-journal.service" "$prefix/etc/systemd/system/moex-trader-journal.timer"
systemctl daemon-reload
echo 'Installed isolated journal. Add nginx snippet inside breakwood.ru HTTPS server, test nginx, then enable timer.'
echo 'Private credentials: /etc/moex-trader-journal/access.txt (root only). No trading services changed.'
