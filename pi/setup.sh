#!/usr/bin/env bash
# Dr.Prepper provisioning for Raspberry Pi 5 (4 GB+) on Raspberry Pi OS Bookworm (64-bit, Desktop).
#
#   sudo ./pi/setup.sh [--drugs-db /path/to/drugs.db] [--ssid DrPrepper] [--wifi-pass 'secret12345'] [--no-hotspot]
#                      [--admin-last Rivera --admin-first Sam --admin-credential EMT-P]
#                      [--no-board] [--touch-output HDMI-A-1] [--board-output HDMI-A-2] [--ssh off|keys]
#
# What it does (idempotent; safe to re-run):
#   1. Installs system packages and a Python venv at /opt/drprepper with this repo installed.
#   2. Creates the 'drprepper' service user, /var/lib/drprepper (data) and /etc/drprepper (keys, TLS).
#   3. Installs drugs.db (copied from --drugs-db, else builds the offline SAMPLE; build the full one in Docker).
#   4. Creates the EHR encryption key, a self-signed TLS cert and the first admin (password printed once).
#   5. Turns the Pi's Wi-Fi into an access point (NetworkManager) so laptops/phones reach the web console.
#   6. Installs the systemd unit (web console + scheduled backups + syslog forwarding) and kiosk autostart.
#   7. Sets the Elecrow 7" HDMI panel to 1024x600.
#   8. Status board on the second HDMI port (Chromium kiosk), touch input locked to the 7" panel.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/drprepper
STATE=/var/lib/drprepper
SECRETS=/etc/drprepper
SSID="DrPrepper"
WIFI_PASS=""
DRUGS_DB=""
HOTSPOT=1
# The screen logs in as a dedicated account with no sudo, so leaving the kiosk never hands out admin rights.
KIOSK_USER="drprepper-kiosk"
SSH_MODE="off"            # off | keys   (--ssh keys keeps SSH with key-only login)
ADMIN_LAST="Admin"; ADMIN_FIRST="Site"; ADMIN_CRED=""
BOARD=1; TOUCH_OUT="HDMI-A-1"; BOARD_OUT="HDMI-A-2"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --drugs-db) DRUGS_DB="$2"; shift 2 ;;
    --ssid) SSID="$2"; shift 2 ;;
    --wifi-pass) WIFI_PASS="$2"; shift 2 ;;
    --no-hotspot) HOTSPOT=0; shift ;;
    --kiosk-user) KIOSK_USER="$2"; shift 2 ;;
    --ssh) SSH_MODE="$2"; shift 2 ;;
    --no-board) BOARD=0; shift ;;
    --touch-output) TOUCH_OUT="$2"; shift 2 ;;
    --board-output) BOARD_OUT="$2"; shift 2 ;;
    --admin-last) ADMIN_LAST="$2"; shift 2 ;;
    --admin-first) ADMIN_FIRST="$2"; shift 2 ;;
    --admin-credential) ADMIN_CRED="$2"; shift 2 ;;
    *) echo "unknown option $1"; exit 2 ;;
  esac
done
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
log() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

log "System packages"
apt-get update
apt-get install -y --no-install-recommends \
  python3-venv python3-dev build-essential pkg-config git openssl \
  libsdl2-dev libsdl2-image-dev libsdl2-ttf-dev libsdl2-mixer-dev libgl1-mesa-dev libgles2-mesa-dev libmtdev1 \
  network-manager
if [[ $BOARD -eq 1 ]]; then
  apt-get install -y --no-install-recommends libnss3-tools
  apt-get install -y --no-install-recommends chromium-browser || apt-get install -y --no-install-recommends chromium
fi

log "Python environment in $PREFIX"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade pip wheel
# Kivy ships aarch64 wheels; sqlcipher3-wheels bundles SQLCipher. No compilers needed in the normal case.
"$PREFIX/venv/bin/pip" install -c "$REPO/constraints.txt" "$REPO[server,touch,ingest]"   # exact tested versions
for b in drprepper-ingest drprepper-manage drprepper-touch; do ln -sf "$PREFIX/venv/bin/$b" /usr/local/bin/$b; done

log "Service user and directories"
id drprepper &>/dev/null || useradd --system --home "$STATE" --shell /usr/sbin/nologin drprepper
install -d -o drprepper -g drprepper -m 0750 "$STATE"
# Touchscreen intake: the kiosk user may read the device token and pinned certificate, nothing else.
getent group drprepper-kiosk >/dev/null || groupadd --system drprepper-kiosk
if ! id "$KIOSK_USER" &>/dev/null; then
  useradd --create-home --shell /bin/bash --gid drprepper-kiosk "$KIOSK_USER"
  passwd -l "$KIOSK_USER" >/dev/null           # no password: it can only be reached through autologin
fi
usermod -aG drprepper-kiosk,video,render,input,audio "$KIOSK_USER"
for g in sudo adm lpadmin netdev; do gpasswd -d "$KIOSK_USER" "$g" &>/dev/null || true; done
if sudo -l -U "$KIOSK_USER" 2>/dev/null | grep -q "may run"; then
  echo "WARNING: $KIOSK_USER can use sudo. Pick a kiosk account without admin rights (--kiosk-user)."; fi
install -d -o drprepper -g drprepper-kiosk -m 2750 "$STATE/kiosk"
install -d -o drprepper -g drprepper -m 0700 "$SECRETS"
cat >/etc/default/drprepper <<EOF
DRP_STATE_DIR=$STATE
DRP_SECRETS_DIR=$SECRETS
DRP_DRUGS_DB=$STATE/drugs.db
DRP_SITE_NAME="Dr.Prepper Field EHR"
DRP_SECURE_COOKIES=1
DRP_PUBLIC_REFERENCE=1
DRP_BACKUP_DEST=/mnt/drp-backup
EOF

log "Drug database"
if [[ -n "$DRUGS_DB" ]]; then
  install -o drprepper -g drprepper -m 0644 "$DRUGS_DB" "$STATE/drugs.db"
elif [[ ! -f "$STATE/drugs.db" ]]; then
  echo "No --drugs-db given: installing the SAMPLE now; the full FDA database downloads in the background after setup."
  sudo -u drprepper env DRP_STATE_DIR=$STATE "$PREFIX/venv/bin/drprepper-ingest" --out "$STATE/drugs.db" --sample
fi
chmod 0644 "$STATE/drugs.db"; chmod 0755 "$STATE"  # touchscreen user reads drugs.db (no patient data in it)

log "TLS certificate"
IP_AP=10.42.0.1
if [[ ! -f "$SECRETS/tls.crt" ]]; then
  # Self-signed for drprepper.local / 10.42.0.1; replace or renew it later in Admin > Trust center.
  sudo -u drprepper --preserve-env=DRP_STATE_DIR,DRP_SECRETS_DIR "$PREFIX/venv/bin/python" -c \
    "from pathlib import Path; from drprepper.server.trust import ensure_default as e; e(Path('$SECRETS/tls.crt'), Path('$SECRETS/tls.key'))"
fi

log "Off-device backup folder /mnt/drp-backup"
install -d -o drprepper -g drprepper -m 0700 /mnt/drp-backup
echo "Mount a USB drive or network share at /mnt/drp-backup (see README: Backups), then set the passphrase in Admin > Backup & restore."

log "EHR database + first admin"
set -a; . /etc/default/drprepper; set +a
sudo -u drprepper --preserve-env=DRP_STATE_DIR,DRP_SECRETS_DIR,DRP_DRUGS_DB drprepper-manage init \
  --last "$ADMIN_LAST" --first "$ADMIN_FIRST" --credential "$ADMIN_CRED" | tee /dev/tty || true
# ehr.db is only readable by the service user; the touchscreen never touches it.
chmod 0600 "$STATE"/ehr.db* 2>/dev/null || true

log "systemd: web console"
install -m 0644 "$REPO/pi/drprepper-web.service" /etc/systemd/system/drprepper-web.service
# Backups and syslog forwarding run inside the web service (Admin > Backup & restore, Admin > Syslog).
systemctl disable --now drprepper-backup.timer 2>/dev/null || true
rm -f /etc/systemd/system/drprepper-backup.service /etc/systemd/system/drprepper-backup.timer
systemctl daemon-reload
install -m 0644 "$REPO/pi/drprepper-ingest.service" /etc/systemd/system/drprepper-ingest.service
install -d -o drprepper -g drprepper -m 0755 "$STATE/cache"
systemctl daemon-reload
systemctl enable --now drprepper-web.service
if sudo -u drprepper "$PREFIX/venv/bin/python" -c "from pathlib import Path; from drprepper.ingest.__main__ import is_sample_db as s; raise SystemExit(0 if s(Path('$STATE/drugs.db')) else 1)"; then
  # Runs now, before the hotspot takes over Wi-Fi (the download needs the internet connection).
  echo "Downloading and building the full FDA drug database (about 1.5 GB; 15-45 minutes on a Pi 5)."
  echo "The console is already usable on the sample meanwhile. Progress: $STATE/ingest-status.json"
  if systemctl start drprepper-ingest.service; then
    echo "Full drug database installed."
  else
    echo "WARNING: the full download failed (see: journalctl -u drprepper-ingest). The unit keeps the SAMPLE."
    echo "         Connect Ethernet (or rerun with --no-hotspot) and run: sudo systemctl start drprepper-ingest"
  fi
fi

if [[ $HOTSPOT -eq 1 ]]; then
  log "Wi-Fi access point '$SSID'"
  if [[ -z "$WIFI_PASS" ]]; then WIFI_PASS="$(openssl rand -base64 12 | tr -dc 'A-Za-z0-9' | head -c 14)"; fi
  nmcli connection delete drprepper-ap &>/dev/null || true
  nmcli connection add type wifi ifname wlan0 con-name drprepper-ap autoconnect yes ssid "$SSID" \
    802-11-wireless.mode ap 802-11-wireless.band bg \
    ipv4.method shared ipv4.addresses "$IP_AP/24" ipv6.method disabled \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$WIFI_PASS" wifi-sec.proto rsn wifi-sec.pairwise ccmp
  nmcli connection up drprepper-ap || echo "AP will start on next boot"
  echo "Hotspot SSID: $SSID   password: $WIFI_PASS   console: https://$IP_AP:8443" | tee "$SECRETS/hotspot.txt"
  chmod 0600 "$SECRETS/hotspot.txt"
fi

log "Touchscreen kiosk autostart for user $KIOSK_USER"
HOME_DIR="$(getent passwd "$KIOSK_USER" | cut -d: -f6)"
install -m 0755 "$REPO/pi/kiosk.sh" /usr/local/bin/drprepper-kiosk
# labwc (Bookworm default since late 2024)
install -d -o "$KIOSK_USER" "$HOME_DIR/.config/labwc"
grep -q drprepper-kiosk "$HOME_DIR/.config/labwc/autostart" 2>/dev/null || \
  echo "/usr/local/bin/drprepper-kiosk &" >> "$HOME_DIR/.config/labwc/autostart"
# wayfire / X11 fallback via XDG autostart
install -d -o "$KIOSK_USER" "$HOME_DIR/.config/autostart"
install -m 0644 -o "$KIOSK_USER" "$REPO/pi/drprepper-touch.desktop" "$HOME_DIR/.config/autostart/drprepper-touch.desktop"
if [[ $BOARD -eq 1 ]]; then
  log "Status board on $BOARD_OUT; touch input locked to $TOUCH_OUT"
  install -m 0755 "$REPO/pi/board.sh" /usr/local/bin/drprepper-board
  grep -q drprepper-board "$HOME_DIR/.config/labwc/autostart" 2>/dev/null || \
    echo "/usr/local/bin/drprepper-board &" >> "$HOME_DIR/.config/labwc/autostart"
  # labwc: keep touches on the 7" panel and put the board window full screen on the second output.
  python3 - "$HOME_DIR/.config/labwc/rc.xml" "$TOUCH_OUT" "$BOARD_OUT" <<'PY'
import re, sys
path, touch, board = sys.argv[1:4]
block = f"""  <!-- drprepper:start -->
  <touch mapToOutput="{touch}" mouseEmulation="no" />
  <windowRules>
    <windowRule identifier="drprepper-touch*">
      <action name="MoveToOutput" output="{touch}" />
    </windowRule>
    <windowRule identifier="drprepper-board*">
      <action name="MoveToOutput" output="{board}" />
      <action name="ToggleFullscreen" />
    </windowRule>
    <windowRule identifier="*chromium*" title="Status*">
      <action name="MoveToOutput" output="{board}" />
    </windowRule>
  </windowRules>
  <!-- drprepper:end -->
"""
try:
    xml = open(path).read()
except FileNotFoundError:
    xml = '<?xml version="1.0"?>\n<openbox_config xmlns="http://openbox.org/3.4/rc">\n</openbox_config>\n'
xml = re.sub(r"  <!-- drprepper:start -->.*?<!-- drprepper:end -->\n", "", xml, flags=re.S)
xml = xml.replace("</openbox_config>", block + "</openbox_config>")
open(path, "w").write(xml)
PY
fi
chown -R "$KIOSK_USER" "$HOME_DIR/.config"
usermod -aG drprepper-kiosk "$KIOSK_USER"   # board.sh reads the unit certificate from the kiosk folder
raspi-config nonint do_boot_behaviour B4 || true   # desktop autologin...
LDM=/etc/lightdm/lightdm.conf                      # ...as the kiosk account, not the installer's admin account
if [[ -f $LDM ]]; then
  sed -i "s/^#\?autologin-user=.*/autologin-user=$KIOSK_USER/" $LDM
  grep -q "^autologin-user=" $LDM || sed -i "/^\[Seat:\*\]/a autologin-user=$KIOSK_USER" $LDM
fi

log "Network hardening"
# Firewall: only the console (8443) and DHCP/DNS for hotspot clients; SSH only if asked for.
apt-get install -y --no-install-recommends nftables
SSH_RULE=""; [[ $SSH_MODE == keys ]] && SSH_RULE="tcp dport 22 accept"
cat > /etc/nftables.conf <<NFT
#!/usr/sbin/nft -f
flush ruleset
table inet filter {
  chain input {
    type filter hook input priority 0; policy drop;
    ct state established,related accept
    iif lo accept
    ip protocol icmp accept
    udp dport { 53, 67 } accept
    tcp dport 53 accept
    tcp dport 8443 accept
    $SSH_RULE
  }
  chain forward { type filter hook forward priority 0; policy drop; }
  chain output { type filter hook output priority 0; policy accept; }
}
NFT
systemctl enable --now nftables
if [[ $SSH_MODE == keys ]]; then
  install -d /etc/ssh/sshd_config.d
  printf "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n" > /etc/ssh/sshd_config.d/drprepper.conf
  systemctl enable ssh && systemctl restart ssh || true
else
  systemctl disable --now ssh 2>/dev/null || true
fi
# Hotspot clients can't reach each other, only the unit.
nmcli connection modify drprepper-ap 802-11-wireless.ap-isolation 1 2>/dev/null || \
  echo "NOTE: this NetworkManager can't set AP client isolation; clients on the hotspot can see each other."

log "Display: Elecrow 7\" HDMI 1024x600"
CMDLINE=/boot/firmware/cmdline.txt
if [[ -f $CMDLINE ]] && ! grep -q "video=HDMI-A-1" $CMDLINE; then
  sed -i '1 s/$/ video=HDMI-A-1:1024x600@60/' $CMDLINE
fi
CFG=/boot/firmware/config.txt
grep -q "# drprepper" $CFG 2>/dev/null || cat >>$CFG <<'EOF'
# drprepper: Elecrow 7" HDMI panel (if your panel is the DSI version, follow Elecrow's overlay instructions instead)
hdmi_force_hotplug=1
EOF

log "Done"
echo "Reboot to start the kiosk:  sudo reboot"
echo "Next: set an exit PIN (Admin > Touchscreen) and seal the encryption key (Admin > Encryption) before real patient data."
echo "Web console:  https://$IP_AP:8443  (accept the self-signed certificate once per device)"
[[ $BOARD -eq 1 ]] && echo "Status board: plug a TV into the second HDMI port ($BOARD_OUT); other screens: Admin > Status board > display key"
echo "Add users:    sudo -u drprepper --preserve-env=DRP_STATE_DIR,DRP_SECRETS_DIR drprepper-manage create-user NAME 'Full Name'"
echo "Set a backup passphrase and destination in Admin > Backup & restore. Backups include the encryption key, locked with that passphrase."
