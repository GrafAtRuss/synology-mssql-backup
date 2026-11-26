#!/bin/sh
export HOME=/var/packages/MSSQLBackup/var
export USERPROFILE="$HOME"
mkdir -p "$HOME"
exec /usr/bin/python3 /var/packages/MSSQLBackup/target/bin/backup.py "$@"
