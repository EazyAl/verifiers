"""Real-process acceptance checks for the proposed startup diagnostic design."""

import asyncio
import importlib
import json
import logging
import os
import socket
import sys
import tempfile
import time
from pathlib import Path

import verifiers.v1 as vf
from verifiers.v1.errors import ToolsetError
from verifiers.v1.mcp import launch
from verifiers.v1.runtimes import SubprocessBackend, SubprocessConfig

FIXTURE = '''import asyncio
import json
import os
from pathlib import Path
import signal
import sys
import time
import verifiers.v1 as vf

class FixtureTools(vf.Toolset[vf.ToolsetConfig]):
    TOOL_PREFIX = "matrix"

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
        if mode == "hang":
            await asyncio.sleep(60)
        if mode == "kill_after_port":
            os.kill(os.getpid(), signal.SIGKILL)

if __name__ == "__main__":
    Path("identity.json").write_text(json.dumps({
        "pid": os.getpid(), "ppid": os.getppid(), "argv": sys.argv,
        "file": __file__, "name": __name__, "package": __package__,
        "spec": __spec__.name,
    }))
    mode = os.environ.get("VF_AUDIT_MODE", "healthy")
    if mode == "exit_zero":
        sys.exit(0)
    if mode == "exit_seven":
        sys.exit(7)
    if mode == "exit_string":
        sys.exit("AUDIT_EXIT_STRING")
    if mode == "missing_dependency":
        import vf_deliberately_missing_dependency
    if mode == "hard_exit":
        os._exit(9)
    if mode == "kill_before_port":
        os.kill(os.getpid(), signal.SIGKILL)
    if mode == "slow_exception_hook":
        hook = sys.excepthook
        def slow_hook(*args):
            time.sleep(2)
            hook(*args)
        sys.excepthook = slow_hook
        raise RuntimeError("AUDIT_SLOW_HOOK")
    if mode == "large_log":
        print("x" * 100_000, file=sys.stderr, flush=True)
        raise RuntimeError("AUDIT_LARGE_LOG")
    FixtureTools.run()
'''


class FixedPortBackend(SubprocessBackend):
    """Exercise the fixed-port branch locally; this is not a cloud runtime test."""

    def __init__(self, port, **kwargs):
        super().__init__(SubprocessConfig(), **kwargs)
        self.port = port

    @property
    def published_port(self):
        return self.port


async def case(module, mode, *, fixed=False):
    if fixed:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        backend = FixedPortBackend(port)
    else:
        backend = SubprocessBackend(SubprocessConfig())
    await backend.start()
    started = time.monotonic()
    result = {"mode": mode, "fixed_port": fixed}
    error = ""
    try:
        runtime = backend.bind(env={"VF_AUDIT_MODE": mode})
        try:
            if mode == "hang":
                pending = asyncio.create_task(
                    launch.serve_in_runtime(
                        module.FixtureTools(vf.ToolsetConfig()), runtime, exposed=fixed
                    )
                )
                try:
                    async with asyncio.timeout(8):
                        while not (Path(backend.info.id) / "identity.json").exists():
                            await asyncio.sleep(0.05)
                    pending.cancel()
                    try:
                        await pending
                    except asyncio.CancelledError:
                        result["outcome"] = "cancelled"
                finally:
                    if not pending.done():
                        pending.cancel()
                        try:
                            await pending
                        except asyncio.CancelledError:
                            pass
            else:
                async with asyncio.timeout(8):
                    port = await launch.serve_in_runtime(
                        module.FixtureTools(vf.ToolsetConfig()), runtime, exposed=fixed
                    )
                result["outcome"] = "started"
        except ToolsetError as exc:
            result["outcome"] = "ToolsetError"
            error = str(exc)
            result["error"] = error
        except TimeoutError:
            result["outcome"] = "cancelled"
        identity = json.loads((Path(backend.info.id) / "identity.json").read_text())
        assert identity["pid"] == backend._children[0].pid
        assert identity["ppid"] == os.getpid(), "bootstrap added a supervisor parent"
        assert identity["argv"] == [identity["file"]]
        assert identity["name"] == "__main__"
        assert identity["spec"] == "audit_design_tools"
        result["module_semantics_and_parent_preserved"] = True
        if mode in {"healthy", "slow"}:
            assert result["outcome"] == "started", result
        elif mode == "hang":
            assert result["outcome"] == "cancelled", result
        else:
            assert result["outcome"] == "ToolsetError", result
            if mode == "exit_zero":
                assert "code 0" in error
            elif mode == "exit_seven":
                assert "code 7" in error
            elif mode in {"hard_exit", "kill_before_port", "kill_after_port"}:
                assert "no exit code was recorded" in error
            else:
                assert "code 1" in error
            expected = {
                "setup_crash": "AUDIT_SETUP_CRASH",
                "exit_string": "AUDIT_EXIT_STRING",
                "missing_dependency": "vf_deliberately_missing_dependency",
                "slow_exception_hook": "AUDIT_SLOW_HOOK",
                "large_log": "AUDIT_LARGE_LOG",
            }.get(mode)
            if expected:
                assert expected in error, f"missing root error: {result}"
            assert len(error) < 2500, len(error)
        result["seconds"] = round(time.monotonic() - started, 3)
        result["passed"] = True
    except Exception as exc:  # noqa: BLE001 - record acceptance-check failures
        result["passed"] = False
        result["check_error"] = repr(exc)
    finally:
        children = list(backend._children)
        await backend.stop()
        result["children_reaped"] = all(p.returncode is not None for p in children)
        result["workdir_removed"] = not Path(backend.info.id).exists()
    print(json.dumps(result), flush=True)
    return result


async def repeated(module):
    backend = SubprocessBackend(
        SubprocessConfig(), name=f"vf audit space {os.getpid()}"
    )
    await backend.start()
    try:
        for mode in ("exit_seven", "healthy"):
            runtime = backend.bind(env={"VF_AUDIT_MODE": mode})
            try:
                async with asyncio.timeout(8):
                    await launch.serve_in_runtime(
                        module.FixtureTools(vf.ToolsetConfig()), runtime, exposed=False
                    )
                assert mode == "healthy"
            except ToolsetError:
                assert mode == "exit_seven"
        assert len(list(Path(backend.info.id).glob("*.status"))) == 2
    finally:
        await backend.stop()
    result = {"mode": "relaunch_in_runtime_with_spaces", "passed": True}
    print(json.dumps(result), flush=True)
    return result


async def main():
    logging.getLogger(launch.__name__).addHandler(logging.NullHandler())
    with tempfile.TemporaryDirectory(prefix="vf-design-audit-") as folder:
        Path(folder, "audit_design_tools.py").write_text(FIXTURE)
        sys.path.insert(0, folder)
        os.environ["PYTHONPATH"] = os.pathsep.join(
            [folder, str(Path(vf.__file__).resolve().parents[2])]
        )
        module = importlib.import_module("audit_design_tools")
        cases = [
            ("healthy", False),
            ("slow", False),
            ("setup_crash", False),
            ("exit_zero", False),
            ("exit_seven", False),
            ("exit_string", False),
            ("missing_dependency", False),
            ("hard_exit", False),
            ("kill_before_port", False),
            ("kill_after_port", False),
            ("slow_exception_hook", False),
            ("large_log", False),
            ("hang", False),
            ("healthy", True),
            ("setup_crash", True),
            ("kill_before_port", True),
        ]
        results = []
        for offset in range(0, len(cases), 4):
            results.extend(
                await asyncio.gather(
                    *[
                        case(module, mode, fixed=fixed)
                        for mode, fixed in cases[offset : offset + 4]
                    ]
                )
            )
        results.append(await repeated(module))
        print(
            json.dumps(
                {
                    "passed": sum(result["passed"] for result in results),
                    "total": len(results),
                }
            ),
            flush=True,
        )
        assert all(result["passed"] for result in results)


if __name__ == "__main__":
    asyncio.run(main())
