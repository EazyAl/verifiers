"""A different listener must not satisfy readiness for a failed server launch."""

import asyncio
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path

from check_startup_design import FIXTURE, FixedPortBackend

import verifiers.v1 as vf
from verifiers.v1.errors import ToolsetError
from verifiers.v1.mcp import launch


async def respond(reader, writer):
    try:
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        )
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


async def main():
    with tempfile.TemporaryDirectory(prefix="vf-collision-audit-") as folder:
        Path(folder, "audit_design_tools.py").write_text(FIXTURE)
        sys.path.insert(0, folder)
        os.environ["PYTHONPATH"] = os.pathsep.join(
            [folder, str(Path(vf.__file__).resolve().parents[2])]
        )
        module = importlib.import_module("audit_design_tools")
        listener = await asyncio.start_server(respond, "127.0.0.1", 0)
        async with listener:
            port = listener.sockets[0].getsockname()[1]
            backend = FixedPortBackend(port)
            await backend.start()
            try:
                try:
                    async with asyncio.timeout(8):
                        result = await launch.serve_in_runtime(
                            module.FixtureTools(vf.ToolsetConfig()),
                            backend.bind(),
                            exposed=True,
                        )
                    print(
                        json.dumps(
                            {"outcome": "wrong_listener_accepted", "port": result}
                        )
                    )
                    raise AssertionError(
                        "readiness accepted an unrelated HTTP listener"
                    )
                except ToolsetError as exc:
                    assert "code 1" in str(exc)
                    assert "Address already in use" in str(exc)
                    print(
                        json.dumps(
                            {
                                "outcome": "ToolsetError",
                                "passed": True,
                                "error": str(exc),
                            }
                        )
                    )
                except TimeoutError:
                    # macOS can allow both a wildcard and a specific listener.
                    # The specific listener wins loopback traffic, so the right
                    # result is to keep waiting, never accept its foreign reply.
                    print(
                        json.dumps(
                            {"outcome": "foreign_listener_rejected", "passed": True}
                        )
                    )
            finally:
                await backend.stop()


if __name__ == "__main__":
    asyncio.run(main())
