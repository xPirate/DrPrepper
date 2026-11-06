#!/bin/sh
# Second HDMI output: the status board in a Chromium kiosk window. Restarts if Chromium exits.
# Opens https://127.0.0.1:8443/board; requests from the Pi itself need no login or key
# (Admin > Status board > "The unit's own screen ..."). Nothing here holds patient data.
exec 9>/tmp/drprepper-board.lock
flock -n 9 || exit 0
KIOSK_DIR=/var/lib/drprepper/kiosk
PROFILE="$HOME/.config/drprepper-board"
URL="https://127.0.0.1:8443/board"
BROWSER="$(command -v chromium-browser || command -v chromium)"
[ -n "$BROWSER" ] || { echo "chromium not installed"; exit 1; }
mkdir -p "$PROFILE"
sleep 8          # let the web console come up first
while true; do
  # Trust the unit's current certificate (it may have been replaced in the Trust center).
  if [ -r "$KIOSK_DIR/server.crt" ] && command -v certutil >/dev/null; then
    mkdir -p "$HOME/.pki/nssdb"
    [ -f "$HOME/.pki/nssdb/cert9.db" ] || certutil -d "sql:$HOME/.pki/nssdb" -N --empty-password
    certutil -d "sql:$HOME/.pki/nssdb" -D -n drprepper 2>/dev/null
    certutil -d "sql:$HOME/.pki/nssdb" -A -t "P,," -n drprepper -i "$KIOSK_DIR/server.crt"
  fi
  "$BROWSER" --kiosk --app="$URL" --class=drprepper-board --user-data-dir="$PROFILE" \
    --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble --disable-features=Translate \
    --password-store=basic --check-for-update-interval=31536000 --overscroll-history-navigation=0
  sleep 10
done
