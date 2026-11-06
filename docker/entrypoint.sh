#!/bin/sh
# drp-entrypoint <web|touch|ingest|ingest-sample|test|syslog-test|manage ...>
set -e
DB="${DRP_DRUGS_DB:-/data/drugs.db}"

# Run as the unprivileged 'drp' user. The first start fixes ownership of volumes created by older (root) builds.
# syslog-test stays root because it listens on port 514.
if [ "$(id -u)" = "0" ] && [ "$1" != "syslog-test" ] && command -v setpriv >/dev/null 2>&1 && id drp >/dev/null 2>&1; then
  for d in /data /secrets /backups; do
    [ -d "$d" ] && [ "$(stat -c %U "$d")" != "drp" ] && chown -R drp:drp "$d"
  done
  export HOME=/home/drp
  exec setpriv --reuid=drp --regid=drp --init-groups "$0" "$@"
fi

need_db() {
  if [ ! -f "$DB" ]; then
    echo "No drug database at $DB - building the SAMPLE so the console can start right away."
    drprepper-ingest --out "$DB" --sample
  fi
}

# Download and build the full FDA database in the background while only the sample is installed.
# The console keeps running on the sample and switches over when the new file is in place.
# Set DRP_AUTO_INGEST=0 to turn this off. Progress: /data/ingest-status.json, log: /data/ingest.log
auto_ingest() {
  [ "${DRP_AUTO_INGEST:-1}" = "1" ] || return 0
  (
    flock -n 9 || exit 0
    drprepper-ingest --out "$DB" --cache /data/cache --if-sample >> /data/ingest.log 2>&1 \
      && echo "Full FDA drug database installed." || echo "Full FDA download failed; see /data/ingest.log"
  ) 9>/data/.ingest.lock &
}

case "$1" in
  ingest)        shift
                 rc=0; flock -n -E 75 /data/.ingest.lock drprepper-ingest --out "$DB" --cache /data/cache "$@" || rc=$?
                 [ "$rc" = 75 ] && echo "A drug database build is already running (see /data/ingest-status.json)."
                 exit "$rc" ;;
  ingest-sample) exec drprepper-ingest --out "$DB" --sample ;;
  test)          exec python -m pytest -q /app/tests ;;
  manage)        shift; exec drprepper-manage "$@" ;;
  syslog-test)   exec drprepper-manage syslog-listen --port "${PORT:-6514}" --tls --udp-port 514 \
                   --cert /certs/syslog-test.crt --key /certs/syslog-test.key ;;
  web)
    need_db
    auto_ingest
    # First start only: create the first admin and print its one-time password to the log.
    drprepper-manage init --last "${DRP_ADMIN_LAST:-Admin}" --first "${DRP_ADMIN_FIRST:-Site}" \
      --credential "${DRP_ADMIN_CREDENTIAL:-}" || true
    if [ "${DRP_SECURE_COOKIES:-1}" = "1" ]; then
      exec drprepper-manage serve --host 0.0.0.0 --port 8443     # TLS; certificate managed in Admin > Trust center
    fi
    exec drprepper-manage serve --host 0.0.0.0 --port 8443 --no-tls ;;
  touch)
    # Kivy on a 1024x600 virtual screen, viewable at http://localhost:6080/vnc.html
    until [ -f "$DB" ]; do echo "waiting for the web container to create $DB"; sleep 2; done
    Xvfb :1 -screen 0 1024x600x24 -nolisten tcp &
    export DISPLAY=:1
    sleep 1
    x11vnc -display :1 -forever -shared -nopw -quiet -localhost &
    websockify --web /usr/share/novnc 6080 localhost:5900 &
    while true; do drprepper-touch --db "$DB" || true; sleep 1; done ;;
  *) exec "$@" ;;
esac
