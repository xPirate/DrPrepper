#!/bin/sh
# Launch the touchscreen reference fullscreen and restart it if it exits or crashes.
# A lock keeps labwc and XDG autostart from starting two copies.
exec 9>/tmp/drprepper-kiosk.lock
flock -n 9 || exit 0
export DRP_FULLSCREEN=1
export DRP_DRUGS_DB=/var/lib/drprepper/drugs.db
export DRP_EHR_URL="https://10.42.0.1:8443"
export DRP_INTAKE_URL="https://127.0.0.1:8443"   # incoming-patient reports to the triage board
export DRP_KIOSK_DIR=/var/lib/drprepper/kiosk
export KIVY_GL_BACKEND=sdl2
# Window identity so labwc keeps the touchscreen app on the 7" panel (see ~/.config/labwc/rc.xml).
export SDL_VIDEO_WAYLAND_WMCLASS=drprepper-touch SDL_VIDEO_X11_WMCLASS=drprepper-touch
sleep 3
while true; do
  /opt/drprepper/venv/bin/drprepper-touch
  # Exit via About > Exit to desktop drops to the desktop for 60 s, then the kiosk returns.
  sleep 60
done
