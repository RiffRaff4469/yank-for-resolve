#!/usr/bin/env bash
# Yank for Resolve v2 — installer (macOS / Linux)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/Yank_for_Resolve.py"

if [[ ! -f "$SRC" ]]; then
  echo "Can't find Yank_for_Resolve.py next to this installer."
  exit 1
fi

case "$(uname -s)" in
  Darwin) DEST="$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility" ;;
  Linux)  DEST="$HOME/.local/share/DaVinciResolve/Fusion/Scripts/Utility" ;;
  *)
    echo "On Windows, copy Yank_for_Resolve.py to:"
    echo '  %APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility'
    exit 1 ;;
esac

mkdir -p "$DEST"
cp "$SRC" "$DEST/"
echo "Installed:"
echo "  $DEST/Yank_for_Resolve.py"
echo

missing=0
for tool in yt-dlp ffmpeg ffprobe; do
  if command -v "$tool" >/dev/null 2>&1; then
    printf '  %-8s %s\n' "$tool" "$(command -v "$tool")"
  else
    printf '  %-8s NOT FOUND\n' "$tool"
    missing=1
  fi
done

if [[ $missing -eq 1 ]]; then
  echo
  echo "Install what's missing:"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    echo "  brew install yt-dlp ffmpeg"
  else
    echo "  sudo apt install ffmpeg && pipx install yt-dlp"
  fi
fi

# Seed the watch queue so --watch works immediately.
mkdir -p "$HOME/.yank_for_resolve"
touch "$HOME/.yank_for_resolve/queue.txt"

echo
echo "Restart Resolve, then: Workspace > Scripts > Yank_for_Resolve"
echo
echo "Check your environment and detected tier any time with:"
echo "  python3 \"$DEST/Yank_for_Resolve.py\" --info"
echo
echo "Send links from anywhere (Studio):"
echo "  python3 \"$DEST/Yank_for_Resolve.py\" --watch"
echo "  echo '<url>' >> ~/.yank_for_resolve/queue.txt"
