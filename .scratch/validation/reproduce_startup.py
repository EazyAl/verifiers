"""Run issue #2937 against real local MCP subprocesses, without a model."""

import asyncio
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import verifiers.v1 as vf
from verifiers.v1.mcp.launch import serve_in_runtime
from verifiers.v1.runtimes import SubprocessConfig, provision_runtime

FIXTURE = '''import asyncio
import os
import verifiers.v1 as vf

class FixtureTools(vf.Toolset[vf.ToolsetConfig]):
    TOOL_PREFIX = "audit"

    @vf.tool
    def echo(self, text: str) -> str:
        """Echo the given text."""
        return text

    async def setup(self):
        mode = os.environ.get("VF_AUDIT_MODE", "healthy")
        if mode == "setup_crash":
            raise RuntimeError("AUDIT_SETUP_CRASH")
        if mode == "slow":
            await asyncio.sleep(2)

if __name__ == "__main__":
    mode = os.environ.get("VF_AUDIT_MODE", "healthy")
    if mode == "early_crash":
        raise RuntimeError("AUDIT_EARLY_CRASH")
    if mode == "shadow_run":
        class GameToolset(vf.Toolset[vf.ToolsetConfig]):
            TOOL_PREFIX = None
            @vf.tool
            def run(self, command: str) -> str:
                """Run a command."""
                return command
        GameToolset.run()
    else:
        FixtureTools.run()
'''


async def case(module, mode):
    started = time.monotonic()
    async with provision_runtime(SubprocessConfig()) as runtime:
        runtime = runtime.with_env({"VF_AUDIT_MODE": mode})
        try:
            async with asyncio.timeout(6):
                port = await serve_in_runtime(
                    module.FixtureTools(vf.ToolsetConfig()), runtime, exposed=False
                )
            result = {"outcome": "started", "port": port}
        except TimeoutError:
            result = {"outcome": "diagnostic_timeout"}
        except Exception as exc:  # noqa: BLE001 - record the original failure type
            result = {"outcome": type(exc).__name__, "error": str(exc)[-2000:]}
        result.update(
            mode=mode,
            seconds=round(time.monotonic() - started, 3),
            child_exit_codes=[p.returncode for p in runtime.backend._children],
        )
        log = Path(runtime.info.id) / "vf_tool_audit.log"
        if log.exists():
            result["log_tail"] = log.read_text(errors="replace")[-1600:]
        print(json.dumps(result), flush=True)


async def main():
    print(json.dumps({"source": vf.__file__, "python": sys.executable}), flush=True)
    with tempfile.TemporaryDirectory(prefix="vf-startup-audit-") as folder:
        Path(folder, "audit_fixture_tools.py").write_text(FIXTURE)
        sys.path.insert(0, folder)
        os.environ["PYTHONPATH"] = os.pathsep.join(
            [folder, str(Path(vf.__file__).resolve().parents[2])]
        )
        module = importlib.import_module("audit_fixture_tools")
        for mode in ("shadow_run", "early_crash", "setup_crash", "healthy", "slow"):
            await case(module, mode)


if __name__ == "__main__":
    asyncio.run(main())
