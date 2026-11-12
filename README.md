# Yank for Resolve

**Paste a link. The clip lands in your Media Pool.**

A simple plugin for DaVinci Resolve that downloads YouTube links as video (MP4)
or audio (WAV / MP3) and imports them straight into your project. No browser
tabs, no sketchy converter sites, no dragging files around. You pick the
quality, the format and where it goes, and it handles the rest.

I built it because pulling reference footage, music and interview clips into an
edit was eating a lot of time on real projects.

---

## What it does

- **Video or audio.** Grab a link as MP4 (video + audio), video only, WAV or MP3.
- **Pick your quality.** Best available, 4K (2160p), 1440p, 1080p, 720p or 480p.
- **Imports for you.** Files go into a Media Pool bin of your choice. You can
  also append them to the current timeline or build a new timeline from the batch.
- **Grab just a section.** Add a time range after a link and you only get that part:
  ```
  https://www.youtube.com/watch?v=abc @ 1:30-2:45
  ```
  Timestamps already in the link (`&t=90s`) are picked up as the start point.
- **Edit-friendly files.** If a download comes in a codec Resolve struggles with
  (VP9, AV1, Opus), it can auto-transcode to DNxHR or ProRes. It can also build
  lightweight proxies so playback stays smooth on a laptop.
- **Batch downloads.** Paste a list of links and up to 6 download in parallel.
- **Stays organized.** Skips links already in the bin, color-codes clips by
  source and saves the original URL in each clip's Comments field.
- **Remembers your settings** between sessions.

### DaVinci Resolve Studio extras

The plugin checks which version of Resolve you're running and turns these on
automatically when it finds Studio:

- **Proxy linking:** proxies are attached to the clip, so you edit on the light
  file and render from the original.
- **Auto transcription:** turns the audio into a subtitle track on arrival.
- **Terminal and watch mode:** import into the open project from outside
  Resolve (see below).

On the free version those features stay off and everything else works the same.

---

## Install

**1. Install the two tools it uses** ([yt-dlp](https://github.com/yt-dlp/yt-dlp) and [ffmpeg](https://ffmpeg.org)):

macOS:
```bash
brew install yt-dlp ffmpeg
```

Windows (PowerShell):
```powershell
winget install yt-dlp.yt-dlp
winget install Gyan.FFmpeg
```

**2. Install the plugin**

macOS / Linux:
```bash
git clone https://github.com/RiffRaff4469/yank-for-resolve.git
cd yank-for-resolve
./install.sh
```

Windows: copy `Yank_for_Resolve.py` into
`%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility\`

**3. Restart Resolve** and open it from **Workspace → Scripts → Yank_for_Resolve**.

---

## Using it

1. Paste one or more links, one per line.
2. Pick **Grab** (Video + Audio, Video only, WAV or MP3) and **Quality**.
3. Choose the download folder and the bin name.
4. Hit **Download & Import**.

### From the terminal

```bash
# check your setup
python3 Yank_for_Resolve.py --info

# download a 1080p clip into a bin called "Refs"
python3 Yank_for_Resolve.py --cli "https://youtube.com/watch?v=..." --quality 1080p --bin "Refs"

# just the audio, as WAV
python3 Yank_for_Resolve.py --cli "https://youtube.com/watch?v=..." --audio

# watch a text file and pull in any link added to it (Studio)
python3 Yank_for_Resolve.py --watch
echo "https://youtube.com/watch?v=..." >> ~/.yank_for_resolve/queue.txt
```

Watch mode pairs well with an iOS Shortcut, Alfred or a Stream Deck button, so
you can send links to your edit from your phone.

Run `python3 Yank_for_Resolve.py --help` for every option.

---

## Troubleshooting

- **"yt-dlp not found" but it works in Terminal:** apps opened from the Dock
  don't see Homebrew's folder. The plugin checks the usual spots, but you can set
  `ytdlp_path` and `ffmpeg_path` in `~/.yank_for_resolve/settings.json`.
- **A link suddenly stops working:** run `brew upgrade yt-dlp`. Sites change
  often and yt-dlp updates fast.
- **Proxies build but don't link:** linking needs Resolve Studio. Variable frame
  rate sources (like screen recordings) can also cause it; set Transcode to
  "Always - DNxHR HQ" first.

---

## How it's built

Python, running inside Resolve's scripting environment, with a native panel
built in Fusion's UI Manager.

- Downloads run on a thread pool, but every Resolve API call stays on the main
  thread (calling it from a background thread crashes Resolve).
- `ffprobe` checks each file's codec so it only transcodes when it has to.
- Proxies keep the source's exact duration and frame rate, which Resolve
  requires before it will link them.
- The same core runs the panel, the terminal mode and the file watcher.

---

## Roadmap

- [x] YouTube links as MP4 or audio (WAV / MP3)
- [x] Quality picker, section grabs, batch downloads
- [x] Auto-transcode and proxy generation
- [ ] **More link types:** reliable, tested support for Instagram, X (Twitter),
      TikTok, Facebook and other social platforms, including posts that need a login
- [ ] **Spotify links:** paste a Spotify track and get the audio in your project
- [ ] Simpler one-click installer for Windows

---

Built on [yt-dlp](https://github.com/yt-dlp/yt-dlp) and [ffmpeg](https://ffmpeg.org).
Only download content you have the rights to use.
