#!/usr/bin/env python3
"""Bring up an interactive Claude Code kernel session from a local terminal.

Does what the platform backend does when a user clicks "open terminal", minus
the portal: warm the runtime, wait for the kernel to report ready, sign a
SigV4 WSS URL, and attach this terminal to the container's ttyd session.

    python3 try_claude_kernel.py --region ap-northeast-1

Requires only AWS credentials with bedrock-agentcore:InvokeAgentRuntime on the
runtime. No Docker, no image build, no deploy.

The kernel's payload contract (runtimes/claude-code-kernel/contract-server):
    {"action": "warmup", "config": {...}}  -> start/reuse the container
    {"action": "status"}                  -> readiness probe
Anything else is a 400 — in particular {"prompt": "..."} belongs to the
headless SDK kernel, not this one.
"""

import argparse
import asyncio
import json
import os
import signal
import sys
import termios
import tty
import uuid
from urllib.parse import quote, urlencode

import boto3
from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
import websockets

KERNEL_NAME = "claude_code_kernel"
# ttyd's "tty" subprotocol: client sends an init JSON, then command-prefixed
# frames; the server prefixes every frame with a command byte too.
TTYD_INPUT = b"0"
TTYD_RESIZE = b"1"
TTYD_OUT = 0x30  # '0'
TTYD_TITLE = 0x31  # '1'
TTYD_PREFS = 0x32  # '2'


def discover_runtime_arn(region: str) -> str:
    """Find the deployed interactive kernel by name."""
    ctl = boto3.client("bedrock-agentcore-control", region_name=region)
    for rt in ctl.list_agent_runtimes().get("agentRuntimes", []):
        if rt["agentRuntimeName"] == KERNEL_NAME:
            return rt["agentRuntimeArn"]
    raise SystemExit(
        f"no runtime named {KERNEL_NAME} in {region} — pass --runtime-arn explicitly"
    )


def new_session_id() -> str:
    """AgentCore requires runtimeSessionId >= 33 characters."""
    return f"ses-{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}"


def invoke(client, arn: str, qualifier: str, session_id: str, payload: dict) -> dict:
    resp = client.invoke_agent_runtime(
        agentRuntimeArn=arn,
        qualifier=qualifier,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
    )
    body = resp["response"].read()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw_text": body.decode(errors="replace")}


def presign_wss(arn: str, region: str, qualifier: str, session_id: str, expires: int = 300) -> str:
    """Sign the /ws upgrade as a query-string request.

    WebSocket clients (browsers especially) cannot set an Authorization header,
    so both the signature and the session ID travel in the query string.
    """
    host = f"bedrock-agentcore.{region}.amazonaws.com"
    base = f"https://{host}/runtimes/{quote(arn, safe='')}/ws"
    params = {
        "qualifier": qualifier,
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
    }
    creds = boto3.Session().get_credentials().get_frozen_credentials()
    request = AWSRequest(method="GET", url=f"{base}?{urlencode(params)}", headers={"host": host})
    SigV4QueryAuth(creds, "bedrock-agentcore", region, expires=expires).add_auth(request)
    return request.url.replace("https://", "wss://")


def model_config(backend: str, model: str, small_fast_model: str) -> dict:
    """Optional per-session model routing, same spec shape the backend sends.

    Omit it and the session uses whatever the deployed container bakes in.
    """
    if not backend:
        return {}
    spec: dict = {"backend": backend}
    if model:
        spec["model"] = model
    if small_fast_model:
        spec["small_fast_model"] = small_fast_model
    return {"model": spec}


async def read_frames(ws, out, deadline: float | None = None) -> None:
    """Relay container output to `out` until the socket closes or time is up."""
    loop = asyncio.get_running_loop()
    while True:
        timeout = None if deadline is None else max(0.0, deadline - loop.time())
        if timeout is not None and timeout == 0.0:
            return
        try:
            frame = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
            return
        if isinstance(frame, str):
            frame = frame.encode()
        if not frame:
            continue
        if frame[0] == TTYD_OUT:
            out.write(frame[1:])
            out.flush()
        # title/preference frames are terminal chrome the portal renders; ignore


async def attach(url: str, smoke_seconds: float | None) -> None:
    cols, rows = os.get_terminal_size() if sys.stdout.isatty() else (120, 30)
    async with websockets.connect(
        url, subprotocols=["tty"], max_size=None, open_timeout=30
    ) as ws:
        # ttyd expects the init JSON before anything else; AuthToken is empty
        # because AgentCore already authorized the upgrade via SigV4.
        await ws.send(json.dumps({"AuthToken": "", "columns": cols, "rows": rows}))
        await ws.send(TTYD_RESIZE + json.dumps({"columns": cols, "rows": rows}).encode())

        if smoke_seconds is not None:
            loop = asyncio.get_running_loop()
            await read_frames(ws, sys.stdout.buffer, loop.time() + smoke_seconds)
            return

        # Interactive: put the local terminal in raw mode and pump both ways.
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        loop = asyncio.get_running_loop()
        pump = asyncio.create_task(read_frames(ws, sys.stdout.buffer))

        def on_stdin() -> None:
            data = os.read(fd, 4096)
            if data:
                asyncio.ensure_future(ws.send(TTYD_INPUT + data))

        def on_resize() -> None:
            c, r = os.get_terminal_size()
            asyncio.ensure_future(
                ws.send(TTYD_RESIZE + json.dumps({"columns": c, "rows": r}).encode())
            )

        try:
            tty.setraw(fd)
            loop.add_reader(fd, on_stdin)
            loop.add_signal_handler(signal.SIGWINCH, on_resize)
            await pump
        finally:
            loop.remove_reader(fd)
            try:
                loop.remove_signal_handler(signal.SIGWINCH)
            except (NotImplementedError, ValueError):
                pass
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            pump.cancel()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--runtime-arn", default="", help=f"defaults to the {KERNEL_NAME} runtime in --region")
    ap.add_argument("--qualifier", default="DEFAULT")
    ap.add_argument("--session-id", default="", help="reuse an existing session (>=33 chars) instead of a new one")
    ap.add_argument("--model-backend", default="", choices=["", "bedrock", "gateway"])
    ap.add_argument("--model", default="", help="e.g. global.anthropic.claude-opus-5")
    ap.add_argument("--small-fast-model", default="")
    ap.add_argument("--ready-timeout", type=int, default=180, help="seconds to wait for the kernel")
    ap.add_argument("--smoke", type=float, metavar="SECONDS", default=None,
                    help="non-interactive: dump the terminal for N seconds and exit")
    args = ap.parse_args()

    arn = args.runtime_arn or discover_runtime_arn(args.region)
    session_id = args.session_id or new_session_id()
    client = boto3.client("bedrock-agentcore", region_name=args.region)

    print(f"runtime : {arn}", file=sys.stderr)
    print(f"session : {session_id}", file=sys.stderr)

    payload: dict = {"action": "warmup"}
    cfg = model_config(args.model_backend, args.model, args.small_fast_model)
    if cfg:
        payload["config"] = cfg
    resp = invoke(client, arn, args.qualifier, session_id, payload)
    print(f"warmup  : {json.dumps(resp)}", file=sys.stderr)

    # A cold container reports "starting" until ttyd is up; poll /invocations
    # rather than guessing at a sleep.
    waited = 0
    while resp.get("status") != "ready" and waited < args.ready_timeout:
        import time

        time.sleep(3)
        waited += 3
        resp = invoke(client, arn, args.qualifier, session_id, {"action": "status"})
        print(f"status  : {json.dumps(resp)} ({waited}s)", file=sys.stderr)
    if resp.get("status") != "ready":
        raise SystemExit(f"kernel not ready after {args.ready_timeout}s — check CloudWatch logs")

    url = presign_wss(arn, args.region, args.qualifier, session_id)
    print("attaching — detach with Ctrl-B then D (tmux stays alive)\n", file=sys.stderr)
    asyncio.run(attach(url, args.smoke))


if __name__ == "__main__":
    main()
