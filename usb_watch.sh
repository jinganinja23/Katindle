#!/bin/bash
set -e

BOOKS_DIR="/home/j/Katindle/books"
FLAG_FILE="/home/j/Katindle/new-books.flag"
MOUNT_POINT="/mnt/usb"

mkdir -p "$BOOKS_DIR"
mkdir -p "$MOUNT_POINT"

echo "[usb_watch] starting, watching for USB..."

while true; do
    # detect first removable partition (ignore └─ etc.)
    DEV=$(lsblk -o NAME,RM,TYPE | awk '$2 == "1" && $3 == "part" {print $1; exit}' | tr -d '└─')
    if [ -n "$DEV" ]; then
        DEV="/dev/$DEV"
        echo "[usb_watch] found device $DEV"

        if mount | grep -q "$MOUNT_POINT"; then
            echo "[usb_watch] already mounted"
        else
            echo "[usb_watch] mounting $DEV -> $MOUNT_POINT"
            mount "$DEV" "$MOUNT_POINT" || {
                echo "[usb_watch] mount failed for $DEV"
                sleep 3
                continue
            }
        fi

        NEW=0
        while IFS= read -r file; do
            base=$(basename "$file")
            dest="$BOOKS_DIR/$base"
            if [ ! -f "$dest" ]; then
                echo "[usb_watch] copying $base"
                cp "$file" "$dest"
                NEW=1
            fi
        done < <(find "$MOUNT_POINT" -type f \( -iname "*.epub" -o -iname "*.txt" \))

        if [ "$NEW" -eq 1 ]; then
            echo "[usb_watch] new books copied, dropping flag"
            touch "$FLAG_FILE"
            chown j:j "$FLAG_FILE" 2>/dev/null || true
        else
            echo "[usb_watch] no new books"
        fi

        umount "$MOUNT_POINT" || echo "[usb_watch] couldn't unmount (busy?)"
        echo "[usb_watch] done, waiting for next USB..."
        sleep 3
    else
        sleep 3
    fi
done
