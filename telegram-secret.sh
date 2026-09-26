#!/bin/bash
# Encrypt/decrypt telegram.env with a passphrase (AES-256, openssl + pbkdf2).
#
#   ./telegram-secret.sh encrypt   # telegram.env  -> telegram.env.enc  (commit .enc)
#   ./telegram-secret.sh decrypt   # telegram.env.enc -> telegram.env   (on the Pi)
#
# Only telegram.env.enc (ciphertext) is committed. The plaintext telegram.env is
# gitignored. The passphrase is never stored anywhere — you type it each time.
set -e
cd "$(dirname "$0")"

case "$1" in
  encrypt)
    [ -f telegram.env ] || { echo "telegram.env not found — create it first"; exit 1; }
    openssl enc -aes-256-cbc -pbkdf2 -salt -in telegram.env -out telegram.env.enc
    echo "wrote telegram.env.enc  ->  git add telegram.env.enc && commit && push"
    ;;
  decrypt)
    [ -f telegram.env.enc ] || { echo "telegram.env.enc not found — git pull first"; exit 1; }
    openssl enc -d -aes-256-cbc -pbkdf2 -in telegram.env.enc -out telegram.env
    chmod 600 telegram.env
    echo "wrote telegram.env  ->  sudo systemctl restart motor-server"
    ;;
  *)
    echo "usage: $0 encrypt|decrypt"; exit 1
    ;;
esac
