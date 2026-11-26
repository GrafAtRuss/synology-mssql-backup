#!/bin/sh
DIR=/var/packages/MSSQLBackup/target/bin
LOG=/var/packages/MSSQLBackup/var/backup.log
ARCH=$(uname -m)
case "$ARCH" in
  x86_64) BIN=sqlcmd-linux-amd64 ;;
  aarch64|arm64) BIN=sqlcmd-linux-arm64 ;;
  *)
    echo "Архитектура $ARCH не поддерживается" | tee -a "$LOG"
    exit 1
    ;;
esac
if [ ! -x "$DIR/$BIN" ]; then
  chmod 755 "$DIR/$BIN" 2>/dev/null || true
fi
if [ ! -f "$DIR/$BIN" ]; then
  echo "sqlcmd не найден в пакете: $DIR/$BIN" | tee -a "$LOG"
  exit 1
fi
export HOME=/var/packages/MSSQLBackup/var
export USERPROFILE="$HOME"
mkdir -p "$HOME"
ln -sfn "$BIN" "$DIR/sqlcmd"
VER=$("$DIR/$BIN" --version 2>/dev/null | head -n 1)
echo "sqlcmd из пакета ($ARCH): ${VER:-готов}"
echo "$(date) sqlcmd готов" >> "$LOG"
exit 0
