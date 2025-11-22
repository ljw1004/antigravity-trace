#!/usr/bin/env python3

# This tool creates a shadow copy of the antigravity extension.
# The shadow will be active until this tool terminates, whereupon it will be cleaned up.
#
# This shim has two means of entry:
# 1. Without arguments, it installs the shadow extension, but in the shadow it uses a shim in place
#    of the language_server_macos_arm binary
# 2. With arguments, it assumes it's being invoked as the shim, and so behaves as a proxy -- a proxy
#    for stdio, but also runs proxies for --extension_server_port, --cloud_code_endpoint,
#    and a few others.

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime
import os
import re
import shutil
import sys
import gzip
import hashlib
import traceback
from urllib.parse import urljoin
from pathlib import Path
from typing import Any, Awaitable, BinaryIO, Callable, List, Tuple, Literal, cast

# install_shim will symlink venv adjacent to the shim
VENV_SITE = Path(__file__).resolve().parent / f"venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
sys.path.insert(0, str(VENV_SITE))

import aiohttp
from aiohttp import web
import httpx


LOGDIR = Path.home() / "antigravity-trace"
SRC = Path("/Applications/Antigravity.app/Contents/Resources/app/extensions/antigravity")
DST = Path.home() / ".antigravity/extensions/antigravity"

TEMPLATE = """<!DOCTYPE html>
<html>
<head>
    <style>
        body {font-family: system-ui, -apple-system, sans-serif; margin: 0;}
        #controls {display: none;}
        body>details {margin-top: 1ex; padding-top: 1ex; border-top: 1px solid lightgray;}
        body:not(.show-LLM) details.label-LLM,
        body:not(.show-CLOUD) details.label-CLOUD,
        body:not(.show-STDIO) details.label-STDIO,
        body:not(.show-EXTENSION) details.label-EXTENSION,
        body:not(.show-INFERENCE) details.label-INFERENCE,
        body:not(.show-API) details.label-API {display: none}
        details {position: relative; padding-left: 1.25em;}
        summary {list-style: none; cursor: pointer;}
        summary::-webkit-details-marker {display: none;}
        summary::before {content: '▷';position: absolute;left: 0;color: #666;}
        details[open]>summary::before {content: '▽';}
        details>div {margin-left: 1.25em;}
        details[open]>summary output {display: none;}
        label {display: none;}
    </style>
    <script src="antigravity-trace.js"></script>
    <script>        
        if (window.buildNode === undefined) {
            // <!--antigravity-trace.js-->
        }
    </script>
</head>
<body>
    <div id="controls">
        <label id="cb-STDIO"><input type="checkbox" onchange="document.body.classList.toggle('show-STDIO', this.checked)">stdio</label>
        <label id="cb-EXTENSION"><input type="checkbox" onchange="document.body.classList.toggle('show-EXTENSION', this.checked)">ext</label>
        <label id="cb-INFERENCE"><input type="checkbox" onchange="document.body.classList.toggle('show-INFERENCE', this.checked)">inference</label>
        <label id="cb-API"><input type="checkbox" onchange="document.body.classList.toggle('show-API', this.checked)">api</label>
        <label id="cb-CLOUD"><input type="checkbox" onchange="document.body.classList.toggle('show-CLOUD', this.checked)">cloud</label>
        <label id="cb-LLM"><input type="checkbox" checked onchange="document.body.classList.toggle('show-LLM', this.checked)">llm</label>
    </div>
</body>
</html>
<!--
"""

def install_shim(argv: list[str]) -> None:
    if len(argv) > 2 or (len(argv) == 2 and argv[1] not in ["--verbose","--uninstall"]):
        sys.exit(f"Usage: {argv[0]} [--verbose|--uninstall]")
    verbose = len(argv) > 1 and argv[1] == "--verbose"
    uninstall = len(argv) > 1 and argv[1] == "--uninstall"

    if uninstall:
        if not DST.exists():
            sys.exit(f"Not found: {DST}")
        shutil.rmtree(DST, ignore_errors=True)
        print("Shadow antigravity extension uninstalled.")
        return

    if not SRC.exists():
        sys.exit(f"Not found: {SRC}")
    shutil.rmtree(DST, ignore_errors=True)
    DST.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SRC, DST, symlinks=True)
    pkg = json.loads((SRC / "package.json").read_text())
    version = pkg["version"]
    pkg["version"] = "99.99.99"
    (DST / "package.json").write_text(json.dumps(pkg, indent=2) + "\n")
    shutil.copyfile(Path(__file__), DST / "bin/language_server_macos_arm")
    os.chmod(DST / "bin/language_server_macos_arm", 0o755)
    shutil.copyfile(Path(__file__).parent / "antigravity-trace.js", DST / "bin/antigravity-trace.js")
    (DST / "bin/antigravity-trace.json").write_text(json.dumps({"verbose":verbose, "version":version}))
    (DST / "bin/venv").symlink_to(Path(__file__).resolve().parent / "venv")
    LOGDIR.mkdir(parents=True, exist_ok=True)
    print(f"Installed shadow {DST}; logs in {LOGDIR} ...")



class Log:
    def __init__(self, verbose: bool) -> None:
        self.ts = datetime.now().strftime("%Y.%m.%d_%H.%M.%S")
        self.path: Path = LOGDIR / f"{self.ts}.html"
        self.mode: Literal["absent", "preambled", "renamed"] = "absent"
        self.prev_log: dict[str, Tuple[Any,Any]] = {}
        self.verbose = verbose

    def _preamble(self, summary: str | None) -> None:
        if self.mode == "renamed" or (self.mode == "preambled" and summary is None):
            return
        if self.mode == "preambled" and summary is not None:
            sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", summary).strip(" .")
            newpath = LOGDIR / f"{self.ts} - {sanitized[:120]}.html"
            self.path.rename(newpath)
            self.path = newpath
        if self.mode == "absent":
            js = (Path(__file__).resolve().parent / "antigravity-trace.js").read_text()
            self.path.write_text(TEMPLATE.replace("// <!--antigravity-trace.js-->", js))
        self.mode = "renamed" if summary is not None else "preambled"

    def _redact_headers(self, h: dict[str, str]) -> dict[str,str]:
        blocklist = {
            "authorization",
            "cookie",
            "set-cookie",
            "proxy-authorization",
            "x-goog-auth-user",
            "x-goog-iap-jwt-assertion",
            "x-goog-api-key",
            "x-api-key",
        }
        return {k: ("[REDACTED]" if k.lower() in blocklist else v) for k, v in h.items()}

    def trace(self, label: Literal["STDIO", "EXTENSION", "INFERENCE", "API", "CLOUD"], endpoint: str, request: str | bytes | None, req_headers: dict[str,str] | None, response: str | bytes | None, resp_headers: dict[str,str] | None) -> None:
        # The job of this function should be to log absolutely everything in jsonl.
        # But, the traffic is so voluminous that we have to be selective! The bulk of this function
        # centers around computing deltas to keep the logs small.
        # Our goal is only ever that our result should be human-readable.
        # (However, it's up to the renderer antigravity-trace.js to actual format the data we put out).
        if not self.verbose and 'streamGenerateContent' not in endpoint:
            return
        request2, response2 = pretty(request), pretty(response)
        k = request2
        k = cast(None | dict[str,Any], k['request'] if isinstance(k, dict) and 'request' in k else None)

        # has the LLM given us a summary?
        summary: str | None = None
        try:
            if 'streamGenerateContent' in endpoint and k:
                requestParts = [part['text'] for content in k.get('contents', {}) for part in content.get('parts',[]) if 'text' in part and isinstance(part['text'], str)]
                if any(part.startswith("Generate a short conversation title") for part in requestParts):
                    kr = response2
                    responses = cast(list[Any], kr) if isinstance(kr, list) else [kr]
                    responseText = ''.join([str(part['text']) for response in responses for candidate in response.get('response',{}).get('candidates',[]) for part in candidate.get('content',{}).get('parts',[]) if 'text' in part and isinstance(part['text'],str)])
                    summary = responseText.strip().splitlines()[0]
        except Exception:
            pass

        self._preamble(summary)

        # Key for delta. Also key on systemInstruction: we'll only log deltas with respect to the same systemInstruction
        key = f"{label}:{endpoint}"
        if k and 'systemInstruction' in k:
            key += ':' + json.dumps(k['systemInstruction'], sort_keys=True)

        prev = self.prev_log.get(key)
        self.prev_log[key] = (request2, response2)
        _is_changed, request2 = delta(prev[0], request2) if prev else (True, request2)
        _is_changed, response2 = delta(prev[1], response2) if prev else (True, response2)
        # but keying on systemInstruction is confusing, so if the new one is shown as "..." then show truncated contents
        k1 = request2
        if k and isinstance(k1, dict):
            k1 = cast(dict[str, Any], k1)
            k2 = cast(dict[str, Any], k1.get('request', k1.get('*request', {})))
            k3 = k2.get('systemInstruction', k2.get('*systemInstruction', {}))
            if k3 and 'parts' in k3 and len(k3['parts']) >= 1 and k3['parts'][0] == "...":
                k3['parts'][0] = {'text': k['systemInstruction']['parts'][0]['text'][:256] + "..."}
        # Certain requests are just obnoxiously wordy
        if 'UpdateCascadeTrajectorySummaries' in endpoint:
            request2 = {k: v for k, v in request2.items() if k.startswith(("+", "-"))}

        d: dict[str,Any] = {"label": label, "endpoint": endpoint, "time": datetime.now().strftime("%H:%M:%S.%f")[:-3]}
        if request2 is not None:
            d["request"] = request2
        if req_headers is not None:
            d["req_headers"] = self._redact_headers(req_headers)
        if response2 is not None:
            d["response"] = response2
        if resp_headers is not None:
            d["resp_headers"] = self._redact_headers(resp_headers)
        with self.path.open("a") as f:
            f.write(json.dumps(d).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;") + "\n")


def delta(prev: Any, new: Any) -> Tuple[bool, Any]:
    """Given two json values, returns a bool for whether they're identical,
    plus a representation of the difference, intended for humans to read.
    The representation always has the same type as 'new'.

    The representation for a changed list is either just the new list,
    or if the change can best be expressed as some added and some removed items,
    then ["...", addItems, "---", removeItems].

    The representation for a changed dict has key `-k:None` if the key
    was removed, `+k:v` if the key was added, `*k:v` if the value of the key changed
    in some way. For list values we have an additional shortcut for readibility:
    if the changed list can best be expressed as some added and some removed items,
    then `k-:[_], k+:[_]`
    """
    if isinstance(prev, dict) and isinstance(new, dict):
        prevl, newl = cast(dict[str,Any], prev), cast(dict[str,Any], new)
        prev_keys = set(prevl.keys())
        new_keys = set(newl.keys())
        d: dict[str, Any] = {}

        for k in sorted(prev_keys - new_keys):
            d[f"-{k}"] = None
        for k in sorted(new_keys - prev_keys):
            d[f"+{k}"] = newl[k]
        for k in sorted(prev_keys & new_keys):
            identical, subdelta = delta(prevl[k], newl[k])
            if identical:
                if len(json.dumps(newl[k])) < 128:
                    d[k] = newl[k]
                elif isinstance(newl[k], dict):
                    d[k] = {"unchanged":"unchanged"}
                elif isinstance(newl[k], list):
                    d[k] = ["..."]
                elif isinstance(newl[k], str):
                    d[k] = "unchanged"
                continue
            if not isinstance(subdelta, list) or (subdelta[0] != "---" and subdelta[0] != "..."):
                d[f"*{k}"] = subdelta
                continue
            subdeltal = cast(list[Any], subdelta)
            iremove = next((i for i, x in enumerate(subdeltal) if x == "---"), None)
            removals = [] if iremove is None else subdeltal[iremove+1:]
            additions = [] if iremove == 0 else subdeltal[1:] if iremove is None else subdeltal[1:iremove]
            if len(removals) > 0:
                d[f"{k}-"] = removals
            if len(additions) > 0:
                d[f"{k}+"] = additions
        return (len(d) == 0, "{repeat}" if len(d) == 0 else d)
    elif isinstance(prev, list) and isinstance(new, list):
        prevl = [(v,blake2b(json.dumps(v, sort_keys=True))) for v in cast(list[Any], prev)]
        newl = [(v,blake2b(json.dumps(v, sort_keys=True))) for v in cast(list[Any], new)]
        removals: list[Any] = []
        additions: list[Any] = []
        for (pv,ph) in prevl:
            ni = next((ni for ni, (_nv,nh) in enumerate(newl) if ph == nh), None)
            if ni is None:
                removals.append(pv)
            else:
                additions.extend((nv for (nv, _nh) in newl[:ni]))
                newl = newl[ni+1:]
        additions.extend((nv for (nv, _nh) in newl))
        new2 = cast(list[Any], new)
        if len(removals) == 0 and len(additions) == 0:
            return (True, new2)
        elif len(removals) + len(additions) < len(new2):
            return (False, ["...", *additions] if len(removals) == 0 else ["---", *removals] if len(additions) == 0 else ["...", *additions, "---", *removals])
        else:
            return (False, new2)
    else:
        return (prev == new, new)

def pretty_proto(buf: bytes) -> Any:
    """Parse protobuf wire format supporting wire types 0,1,2,5. Throws if parse fails."""
    def read_varint(data: bytes, pos: int) -> Tuple[int, int]:
        shift = 0
        value = 0
        while pos < len(data):
            b = data[pos]
            pos += 1
            value |= (b & 0x7F) << shift
            if b < 0x80:
                return value, pos
            shift += 7
        assert False, "truncated varint"

    pos = 0
    out: List[Any] = []
    while pos < len(buf):
        key, pos = read_varint(buf, pos)
        wire = key & 0x7
        if wire == 0:  # varint
            val, pos = read_varint(buf, pos)
            out.append(val)
        elif wire == 1:  # 64-bit
            assert pos + 8 <= len(buf), "64bit bounds"
            out.append(int.from_bytes(buf[pos : pos + 8], "little"))
            pos += 8
        elif wire == 2:  # length-delimited
            length, pos = read_varint(buf, pos)
            assert pos + length <= len(buf), "blob bounds"
            out.append(pretty(buf[pos : pos + length]))
            pos += length
        elif wire == 5:
            assert pos + 4 <= len(buf), "32bit bounds"
            out.append(int.from_bytes(buf[pos : pos + 4], "little"))
            pos += 4
        else:
            assert False, "wire type"
    assert pos == len(buf), "incomplete"
    # list[list[key, list]] is a pattern that crops up enough that I'll turn it into dict[key, list]
    if len(out) == 1 and all(isinstance(x, list) and len(cast(list[Any],x)) == 2 and isinstance(x[0], str) for x in out[0]):
        return dict(out[0])
    return out


def pretty(buf: bytes | str | None) -> Any:
    if buf is None:
        return buf
    
    # Binary payloads are either protobuf, hex, or strings
    if isinstance(buf, bytes):
        try:
            return pretty_proto(buf)
        except Exception:
            pass
        hex = buf.hex()
        try:
            buf = buf.decode('utf-8').strip()
            if len(hex) < len(buf):
                return hex
        except UnicodeDecodeError:
            return hex
    
    # Strings may be SSE format, in which case reconstruct them
    if all((s.startswith("data:") or not s.strip() for s in buf.splitlines())):
        buf = '\n'.join([s[5:].lstrip() for s in buf.splitlines() if s.startswith("data:")])

    # Strings may be JSON or JSONL
    try:
        return json.loads(buf)
    except Exception:
        pass
    try:
        return [json.loads(line) for line in buf.splitlines() if line.strip()]
    except Exception:
        pass

    # Otherwise, just a string!
    return buf

def blake2b(s: str) -> bytes:
    h = hashlib.blake2b(digest_size=20)
    h.update(s.encode('utf-8'))
    return h.digest()


def parse_argv(argv: list[str]) -> dict[str, list[str]]:
    """Given CLI arguments ["--foo", "1", "--bar", "--baz", "2", "3"],
    returns them as a dictionary {"--foo": ["1"], "--bar": [], "--baz": ["2", "3"]}.
    Note that order is lost, and repeat keys are collapsed, and positional
    arguments before anything else go bad. So don't do those things!"""
    r: dict[str, list[str]] = {}
    current: str = ""
    for token in argv:
        if token.startswith("--"):
            current = token
            r[current] = []
        else:
            r[current].append(token)
    return r


async def start_extension_proxy(log: Log, target_port: int) -> tuple[int, Callable[[], Awaitable[None]]]:
    session = aiohttp.ClientSession()

    async def handle(request: web.Request) -> web.StreamResponse:
        req_body = await request.read()
        async with session.request(
            request.method,
            f"http://127.0.0.1:{target_port}{request.rel_url}",
            headers=request.headers,
            data=req_body,
        ) as resp:
            resp_body = await resp.read()
            log.trace("EXTENSION", str(request.rel_url), req_body, dict(request.headers), resp_body, dict(resp.headers))
            headers = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
            return web.Response(status=resp.status, headers=headers, body=resp_body)

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = int(site._server.sockets[0].getsockname()[1])  # type: ignore

    async def cleanup() -> None:
        await runner.cleanup()
        await session.close()

    return port, cleanup

async def start_web_proxy(log: Log, base_url_str: str, label: Literal["INFERENCE", "API", "CLOUD"]) -> tuple[str, Callable[[], Awaitable[None]]]:
    """Expose a local URL that forwards to base_url, keeping scheme/host intact."""
    transport = httpx.AsyncHTTPTransport(
        http2=True,
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
    )
    session: httpx.AsyncClient = httpx.AsyncClient(
        transport=transport,
        timeout=None,
        headers={"accept-encoding": "identity"},
    )

    async def handle(request: web.Request) -> web.StreamResponse:
        req_body = await request.read()
        async with session.stream(
            request.method,
            urljoin(base_url_str, request.raw_path),
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            content=req_body,
        ) as resp:
            out_headers: dict[str, str] = {
                k: v
                for k, v in resp.headers.items()
                if k.lower() not in {"content-length", "connection"}
            }
            stream = web.StreamResponse(status=resp.status_code, headers=out_headers)
            await stream.prepare(request)

            resp_body_chunks: list[bytes] = []
            async for chunk in resp.aiter_raw():
                resp_body_chunks.append(chunk)
                await stream.write(chunk)
            await stream.write_eof()
            resp_body = b''.join(resp_body_chunks)
            resp_body = gzip.decompress(resp_body) if resp.headers.get("content-encoding") == "gzip" else resp_body
        log.trace(label, str(request.rel_url), req_body, dict(request.headers), resp_body, dict(resp.headers))
        return stream

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = int(site._server.sockets[0].getsockname()[1])  # type: ignore

    async def cleanup() -> None:
        await runner.cleanup()
        await session.aclose()

    return f"http://127.0.0.1:{port}", cleanup



async def shim() -> None:
    config = json.loads((Path(__file__).resolve().parent / "antigravity-trace.json").read_text())
    pkg = json.loads((SRC / "package.json").read_text())
    if config["version"] != pkg["version"]:
        sys.exit(f"Shim is out of date. You must reinstall it.")
    log = Log(config["verbose"])
    log.trace("STDIO", "cmdline", ' '.join(sys.argv), None, None, None)

    args = parse_argv(sys.argv[1:])

    extension_server_port, extension_server_cleanup = await start_extension_proxy(log, int(args["--extension_server_port"][0]))
    args["--extension_server_port"] = [str(extension_server_port)]
    inference_url, inference_cleanup = await start_web_proxy(log, args["--inference_api_server_url"][0], 'INFERENCE')
    args["--inference_api_server_url"] = [inference_url]
    api_url, api_cleanup = await start_web_proxy(log, args["--api_server_url"][0], 'API')
    args["--api_server_url"] = [api_url]
    cloud_url, cloud_cleanup = await start_web_proxy(log, args["--cloud_code_endpoint"][0], 'CLOUD')
    args["--cloud_code_endpoint"] = [cloud_url]

    argv2 = [str(SRC / "bin/language_server_macos_arm"), *[arg for k, v in args.items() for arg in [k, *v]]]
    proc = await asyncio.create_subprocess_exec(
        *argv2,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    async def pump_output(reader: asyncio.StreamReader, writer: BinaryIO, label: str) -> None:
        while True:
            chunk = await reader.read(64 * 1024)
            if not chunk:
                break
            # There are some stderr messages that come out with crazy high frequency
            too_wordy = [b"could not convert a single message before hitting truncation",
                b"queryText was truncated",
                b") exceeds limit ("]
            if label == "stderr" and any((w in chunk for w in too_wordy)):
                pass
            else:
                log.trace("STDIO", label, None, None, chunk, None)
            try:
                writer.write(chunk)
                writer.flush()
            except BrokenPipeError:
                break

    async def pump_input() -> None:
        try:
            while True:
                chunk = await asyncio.to_thread(sys.stdin.buffer.read, 64 * 1024)
                if not chunk:
                    break
                log.trace("STDIO", "stdin", chunk, None, None, None)
                assert proc.stdin is not None
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        finally:
            assert proc.stdin
            proc.stdin.close()
            with contextlib.suppress(Exception):
                await proc.stdin.wait_closed()


    stdout = asyncio.create_task(pump_output(proc.stdout, sys.stdout.buffer, "stdout"))
    stderr = asyncio.create_task(pump_output(proc.stderr, sys.stderr.buffer, "stderr"))
    stdin = asyncio.create_task(pump_input())
    returncode = await proc.wait()

    stdout.cancel()
    stderr.cancel()
    stdin.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await stdout
        await stderr
        await stdin
    await extension_server_cleanup()
    await inference_cleanup()
    await api_cleanup()
    await cloud_cleanup()
    sys.exit(returncode)


if __name__ == "__main__":
    try:
        install_shim(sys.argv) if len(sys.argv) <= 2 else asyncio.run(shim())
    except Exception as e:
        (LOGDIR / "error.txt").write_text(traceback.format_exc())
        sys.exit(1)
