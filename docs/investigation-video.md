# Investigation video

[Get the full 31-second MP4](https://raw.githubusercontent.com/abhishekash/agent-observatory/main/media/investigation-demo.mp4)
or view the [animated README preview](../media/investigation-demo-preview.gif).

The recording shows the actual Agent Observatory terminal UI populated with a
synthetic example. It contains no real prompts, repositories, or session logs.
There is no voiceover; all narration is visible on screen.

## Transcript

1. **Find the session.** A Codex session in `sample/payments-api` was asked to
   investigate a checkout failure in pull request #184. Its latest recorded
   action is `github / get_pull_request`.
2. **See the last action.** Activity shows the MCP request failed after 713 ms.
   A checkout test also failed, but its cause has not been established.
3. **Read the MCP response.** The observed GitHub tool call returned HTTP 403
   with `API rate limit exceeded`. The call detail includes its arguments,
   response, and trace link.
4. **Follow the trace.** The failed MCP span belongs to the same agent run.
   The pull request was not inspected; the GitHub access limit must be resolved
   before the checkout failure can be diagnosed further.

## Rebuild

From the repository root, with [uv](https://docs.astral.sh/uv/) installed:

```sh
uv run python scripts/capture_investigation_video.py --output /tmp/ao-capture
uv run --with cairosvg --with pillow --with imageio-ffmpeg \
  python scripts/compose_investigation_video.py \
  --captures /tmp/ao-capture --output media/investigation-demo.mp4
uv run --with imageio-ffmpeg python scripts/make_readme_preview.py \
  --video media/investigation-demo.mp4 \
  --output media/investigation-demo-preview.gif
```

The capture script renders a fixed synthetic case in Textual's test driver.
The composer converts the SVG screenshots into a 1920×1080 H.264 MP4 and
poster PNG. The local receiver and its database are not used.
