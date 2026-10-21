"""Make a high-resolution, looping README GIF from the investigation MP4.

Requires imageio-ffmpeg. The GIF shows six captioned moments from the video at
the video's native resolution by default; the MP4 remains the full-length version.
"""

import argparse
import subprocess
from pathlib import Path

from imageio_ffmpeg import get_ffmpeg_exe


def make_preview(video: Path, output: Path, width: int = 0, fps: int = 12,
                 colors: int = 256):
    segments = [(0, 2), (6, 9), (11, 14), (15, 19), (21, 25), (26, 29)]
    filters = [
        f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}]"
        for i, (start, end) in enumerate(segments)
    ]
    inputs = "".join(f"[v{i}]" for i in range(len(segments)))
    scale = f"scale={width}:-1:flags=lanczos," if width else ""
    filters.extend([
        f"{inputs}concat=n={len(segments)}:v=1:a=0,"
        f"fps={fps},{scale}split[x][y]",
        f"[y]palettegen=max_colors={colors}:stats_mode=full[p]",
        "[x][p]paletteuse=dither=bayer:bayer_scale=5[v]",
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
        "-i", str(video), "-filter_complex", ";".join(filters),
        "-map", "[v]", "-loop", "0", "-y", str(output),
    ], check=True)
    print(f"Wrote {output} ({output.stat().st_size / 1024 / 1024:.1f} MiB, "
          f"{width or 'native'}px wide, {fps} fps)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--width", type=int, default=0,
                        help="output width in pixels (default: native video width)")
    parser.add_argument("--fps", type=int, default=12, help="frames per second (default: 12)")
    parser.add_argument("--colors", type=int, default=256, help="palette size (default: 256)")
    args = parser.parse_args()
    make_preview(args.video, args.output, args.width, args.fps, args.colors)
