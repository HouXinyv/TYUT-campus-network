#!/bin/sh
set -eu
umask 077
source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
target_dir="$HOME/.local/share/tyut-autologin"
command -v python3 >/dev/null
python3 -c 'import sys; assert sys.platform == "linux" and sys.version_info >= (3, 8), "Requires Linux and Python 3.8+"'
if [ -L "$target_dir" ]; then
    echo 'Refusing symbolic-link installation directory.' >&2
    exit 1
fi
mkdir -p "$target_dir"
chmod 700 "$target_dir"
install -m 700 "$source_dir/autologin.py" "$target_dir/autologin.py.new"
mv "$target_dir/autologin.py.new" "$target_dir/autologin.py"
echo 'Installed. Existing credentials and schedules are preserved.'
echo 'Next: python3 ~/.local/share/tyut-autologin/autologin.py setup'
echo 'Then: python3 ~/.local/share/tyut-autologin/autologin.py inspect'
echo 'Then: python3 ~/.local/share/tyut-autologin/autologin.py enable'
