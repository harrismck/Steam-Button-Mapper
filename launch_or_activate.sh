#!/usr/bin/env bash
# launch_or_activate.sh
#
# Meant to be added as a Steam "Non-Steam Game" shortcut, so you can start
# the mapper from Gaming Mode's library without dropping to a terminal.
#
# Safe to run repeatedly, including if the service is already running!
# It checks first and does nothing in that case, rather than trying to
# start a second instance.

set -euo pipefail

SERVICE="steam-button-mapper.service"

if systemctl --user is-active --quiet "$SERVICE"; then
    echo "$SERVICE is already running; nothing to do."
    exit 0
fi

echo "Starting $SERVICE..."
systemctl --user start "$SERVICE"
