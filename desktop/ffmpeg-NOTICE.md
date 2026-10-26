# FFmpeg in the mosswork client

FFmpeg and ffprobe are separate executables, licensed under GPL v3 or later by
their respective authors. They are not covered by mosswork's MIT license.
The client invokes them as command-line tools. Binary download URLs and SHA-256
values are recorded in `downloads.json` beside this notice.

- License: <https://www.gnu.org/licenses/gpl-3.0.html>
- FFmpeg source: <https://git.ffmpeg.org/ffmpeg.git>
- Windows build scripts and dependency sources:
  <https://github.com/BtbN/FFmpeg-Builds>
- macOS build information and source downloads:
  <https://ffmpeg.martin-riedl.de/>

The macOS binaries are FFmpeg 9.0.1; Windows uses the pinned 9.0 release-branch
build `n9.0.1-26-g5c8e7e2433`. The binaries are mirrored by Sudowork at the
checksum-pinned addresses recorded in `downloads.json`. A copy of the GPL v3 is
included as `GPL-3.0.txt`. Keep the corresponding upstream source and build instructions available
when redistributing these binaries. Preserve upstream licenses included here.
