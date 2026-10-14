"""Local SDK replay for the automatic launcher. No API keys or model charges.

Install the auto extra plus openai, anthropic, and langchain-core in one environment.
Run with: python -m observatory run --endpoint http://127.0.0.1:14318 \
    --service local-capture-demo examples/automatic_capture_demo.py
"""
import argparse
import asyncio
import json
import time

import anthropic
import httpx2
import openai
from langchain_core.runnables import RunnableLambda

REPLAY_DELAY = 0

def replay(request):
    if REPLAY_DELAY:
        time.sleep(REPLAY_DELAY)
    data = json.loads(request.content)
    model = data.get("model", "local-replay")
    if model == "demo-error":
        return httpx2.Response(400, json={"error": {"message": "Local replay: invalid model", "type": "invalid_request_error"}})
    if request.url.path.endswith('/messages'):
        return httpx2.Response(200, json={"id": "msg_replay", "type": "message", "role": "assistant",
            "model": model, "content": [{"type": "text", "text": "This is a local Anthropic replay."}],
            "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 9, "output_tokens": 5}})
    if data.get("stream"):
        chunks = [
            {"id":"chatcmpl_replay", "object":"chat.completion.chunk", "created":1, "model":model,
             "choices":[{"index":0,"delta":{"role":"assistant","content":"Local stream replay."},"finish_reason":None}]},
            {"id":"chatcmpl_replay", "object":"chat.completion.chunk", "created":1, "model":model,
             "choices":[{"index":0,"delta":{},"finish_reason":"stop"}]},
            {"id":"chatcmpl_replay", "object":"chat.completion.chunk", "created":1, "model":model,
             "choices":[], "usage":{"prompt_tokens":4,"completion_tokens":3,"total_tokens":7}},
        ]
        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx2.Response(200, headers={"content-type":"text/event-stream"}, content=body)
    return httpx2.Response(200, json={"id":"chatcmpl_replay", "object":"chat.completion", "created":1,
        "model": model, "choices":[{"index":0,"message":{"role":"assistant","content":"This is a local OpenAI replay."},"finish_reason":"stop"}],
        "usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}})


async def stream_replay():
    async with openai.AsyncOpenAI(api_key="local-replay", base_url="https://demo.invalid/v1",
        max_retries=0, http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(replay))) as client:
        response = await client.chat.completions.create(model="local-openai-stream", stream=True,
            stream_options={"include_usage": True}, messages=[{"role":"user","content":"Replay a streamed answer."}])
        async for _ in response:
            pass


def main():
    global REPLAY_DELAY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pause", type=float, default=0, help="pause before calls to inspect the connected state")
    parser.add_argument("--latency", type=float, default=0, help="simulate a slow model to inspect live progress")
    args = parser.parse_args()
    REPLAY_DELAY = max(0, args.latency)
    print("Running local replay: no external model requests or paid calls.")
    if args.pause > 0:
        time.sleep(args.pause)
    with openai.OpenAI(api_key="local-replay", base_url="https://demo.invalid/v1", max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(replay))) as client:
        chain = RunnableLambda(lambda prompt: client.chat.completions.create(model="local-openai-chat",
            messages=[{"role":"user","content":prompt}]).choices[0].message.content)
        print(chain.invoke("local-only-sensitive-demo-message", config={"run_name":"Local replay: research chain"}))
        try:
            client.chat.completions.create(model="demo-error", messages=[{"role":"user","content":"Replay an error."}])
        except openai.BadRequestError:
            print("Recorded an expected local error.")
    asyncio.run(stream_replay())
    with anthropic.Anthropic(api_key="local-replay", base_url="https://demo.invalid", max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(replay))) as client:
        response = client.messages.create(model="local-anthropic-chat", max_tokens=40,
            messages=[{"role":"user","content":"Replay an Anthropic answer."}])
        print(response.content[0].text)
    print("Finished. Open Observatory to inspect the automatic spans and token counts.")


if __name__ == "__main__":
    main()
