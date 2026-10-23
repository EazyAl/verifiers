import asyncio
import json
import os
import signal
import sys
from pathlib import Path

import verifiers.v1 as vf


class AuditTools(vf.Toolset[vf.ToolsetConfig]):
    TOOL_PREFIX = "startup_audit"

    @vf.tool
    def echo(self, text: str) -> str:
        """Echo text."""
        return text

    async def setup(self):
        mode = os.environ["VF_STARTUP_AUDIT_MODE"]
        if mode == "setup_crash":
            raise RuntimeError("AUDIT_REMOTE_SETUP_FAILURE")
        if mode == "slow":
            await asyncio.sleep(2)
        if mode == "hang":
            await asyncio.sleep(120)
        if mode == "kill_after_port":
            os.kill(os.getpid(), signal.SIGKILL)


if __name__ == "__main__":
    Path(os.environ["VF_STARTUP_AUDIT_ID"]).write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "uid": os.getuid(),
                "ppid": os.getppid(),
            }
        )
    )
    mode = os.environ["VF_STARTUP_AUDIT_MODE"]
    if mode == "exit_zero":
        sys.exit(0)
    if mode == "exit_seven":
        sys.exit(7)
    if mode == "hard_exit":
        os._exit(9)
    AuditTools.run()
