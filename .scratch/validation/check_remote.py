"""Validate the actual package/install/launch path on disposable providers."""

import argparse
import asyncio
import contextlib
import getpass
import importlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

import verifiers.v1 as vf
from verifiers.v1.errors import ToolsetError
from verifiers.v1.mcp import launch
from verifiers.v1.runtimes import DockerConfig, PrimeConfig, provision_runtime

TOOL_CHECK = r"""
import asyncio, json, sys
from verifiers.v1.harnesses.utils.mcp import mcp_client
async def main():
    async with mcp_client({"url": f"http://127.0.0.1:{sys.argv[1]}/mcp"}) as client:
        tools = await client.list_tools()
        assert any(tool.name == "echo" for tool in tools.tools)
        result = await client.call_tool("echo", {"text": "remote-startup-ok"})
        assert any(getattr(part, "text", "") == "remote-startup-ok" for part in result.content)
        print(json.dumps({"tool_call": "passed"}))
asyncio.run(main())
"""


async def one_case(module, base, mode, *, user=False, exposed=False):
    identity_path = f"/tmp/vf-startup-audit-{uuid.uuid4().hex}.json"
    runtime = base.with_env(
        {
            "VF_STARTUP_AUDIT_MODE": mode,
            "VF_STARTUP_AUDIT_ID": identity_path,
        }
    )
    if user:
        runtime = runtime.with_user("nobody", "/tmp")
    start = time.monotonic()
    outcome = {"mode": mode, "user": user, "exposed": exposed}
    task = None
    try:
        if mode == "hang":
            task = asyncio.create_task(
                launch.serve_in_runtime(
                    module.AuditTools(vf.ToolsetConfig()),
                    runtime,
                    exposed=exposed,
                )
            )
            async with asyncio.timeout(120):
                while True:
                    try:
                        await runtime.read(identity_path)
                        break
                    except Exception:  # noqa: BLE001 - bounded wait for a remote file
                        await asyncio.sleep(0.5)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                outcome["outcome"] = "cancelled"
            assert outcome.get("outcome") == "cancelled"
        else:
            try:
                async with asyncio.timeout(300):
                    port = await launch.serve_in_runtime(
                        module.AuditTools(vf.ToolsetConfig()),
                        runtime,
                        exposed=exposed,
                    )
                outcome["outcome"] = "started"
                assert mode in {"healthy", "slow"}, outcome
                python = await launch._install_in_sandbox(
                    module.AuditTools(vf.ToolsetConfig()), runtime
                )
                result = await runtime.run([python, "-c", TOOL_CHECK, str(port)], {})
                assert result.exit_code == 0, result.stderr[-2000:]
                outcome["tool_call"] = "passed"
            except ToolsetError as error:
                message = str(error)
                outcome["outcome"] = "ToolsetError"
                outcome["error"] = message
                assert mode not in {"healthy", "slow"}, message
                if mode == "setup_crash":
                    assert "AUDIT_REMOTE_SETUP_FAILURE" in message
                    assert "code 1" in message
                elif mode == "exit_zero":
                    assert "code 0" in message
                elif mode == "exit_seven":
                    assert "code 7" in message
                else:
                    assert "no exit code was recorded" in message
        identity = json.loads(await runtime.read(identity_path))
        assert (identity["uid"] != 0) == user, identity
        outcome["passed"] = True
        outcome["seconds"] = round(time.monotonic() - start, 3)
        print(json.dumps(outcome), flush=True)
    finally:
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        try:
            identity = json.loads(await runtime.read(identity_path))
        except Exception:
            if outcome.get("passed"):
                raise
        else:
            stopped = await base.run(
                [
                    "sh",
                    "-c",
                    'kill -TERM "$1"',
                    "vf-startup-audit",
                    str(identity["pid"]),
                ],
                {},
            )
            if stopped.exit_code:
                # A crashed server may already be gone. Healthy servers must
                # actually be stopped before another fixed-port case starts.
                assert mode not in {"healthy", "slow", "hang"}, stopped.stderr
        await asyncio.sleep(0.5)


async def main(args):
    sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
    module = importlib.import_module("startup_fixture")
    if args.runtime == "prime":
        config = PrimeConfig(
            image="python:3.13-slim",
            cpu=1,
            memory=2,
            disk=5,
            idle_timeout=180,
            labels=["vf-startup-audit"],
        )
    else:
        config = DockerConfig(image="python:3.13-slim")
    resource = None
    try:
        async with (
            asyncio.timeout(1200),
            provision_runtime(
                config, name=f"vf-startup-audit-{uuid.uuid4().hex[:8]}"
            ) as runtime,
        ):
            resource = runtime.info.id
            print(
                json.dumps({"runtime": args.runtime, "resource": resource}), flush=True
            )
            cases = [
                ("healthy", False, True),
                ("setup_crash", False, True),
                ("exit_zero", False, False),
                ("exit_seven", False, False),
                ("hard_exit", False, False),
                ("kill_after_port", False, False),
                ("slow", False, False),
                ("healthy", True, False),
                ("setup_crash", True, False),
                ("hang", False, False),
            ]
            for mode, user, exposed in cases:
                await one_case(module, runtime, mode, user=user, exposed=exposed)
        print(
            json.dumps(
                {
                    "passed": True,
                    "cases": len(cases),
                    "resource_teardown": "completed",
                    "resource": resource,
                }
            ),
            flush=True,
        )
    except Exception as error:
        print(
            json.dumps({"passed": False, "error": str(error), "resource": resource}),
            flush=True,
        )
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", choices=["docker", "prime"], required=True)
    parser.add_argument("--ask-key", action="store_true")
    args = parser.parse_args()
    if args.ask_key:
        os.environ["PRIME_API_KEY"] = getpass.getpass("Prime API key: ")
    asyncio.run(main(args))
