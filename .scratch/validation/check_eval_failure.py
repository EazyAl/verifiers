"""Check that startup failure reaches a real vf-eval trace and its log."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

TASKSET = """import verifiers.v1 as vf
from startup_audit_tools import BrokenTools

class FailureTask(vf.Task):
    @classmethod
    def toolsets(cls, config):
        return [BrokenTools(vf.ToolsetConfig())]

class FailureTaskset(vf.Taskset[FailureTask, vf.TasksetConfig]):
    def load(self):
        return [FailureTask(vf.TaskData(prompt="This task must fail before a model call."), self.config.task)]

__all__ = ["FailureTaskset"]
"""

TOOLS = '''import verifiers.v1 as vf

class BrokenTools(vf.Toolset[vf.ToolsetConfig]):
    TOOL_PREFIX = "startup_audit"

    @vf.tool
    def echo(self, text: str) -> str:
        """Echo text."""
        return text

    async def setup(self):
        raise RuntimeError("AUDIT_EVAL_STARTUP_FAILURE")

if __name__ == "__main__":
    BrokenTools.run()
'''


def main():
    output = Path("../verifiers-audit/eval-startup-output").resolve()
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vf-eval-startup-audit-") as folder:
        Path(folder, "startup_audit.py").write_text(TASKSET)
        Path(folder, "startup_audit_tools.py").write_text(TOOLS)
        env = {
            **os.environ,
            "PYTHONPATH": folder,
            "VF_AUDIT_UNUSED_KEY": "unused-local-fixture",
        }
        command = [
            shutil.which("vf-eval"),
            "startup-audit",
            "-n",
            "1",
            "-r",
            "1",
            "--model",
            "unused-model",
            "--no-push",
            "--no-rich",
            "--env.agent.harness.id",
            "null",
            "--env.agent.runtime.type",
            "subprocess",
            "--client.base-url",
            "http://127.0.0.1:9/v1",
            "--client.api-key-var",
            "VF_AUDIT_UNUSED_KEY",
            "--output-dir",
            str(output),
        ]
        result = subprocess.run(
            command, env=env, capture_output=True, text=True, timeout=40, check=False
        )
        Path("../verifiers-audit/eval-startup-console.txt").write_text(
            result.stdout + result.stderr
        )
        logs = "\n".join(path.read_text() for path in output.rglob("eval.log"))
        traces = "\n".join(path.read_text() for path in output.rglob("traces.jsonl"))
        assert "AUDIT_EVAL_STARTUP_FAILURE" in logs, result.stderr[-2000:]
        assert "ToolsetError" in traces
        assert "AUDIT_EVAL_STARTUP_FAILURE" in traces
        print(
            json.dumps(
                {
                    "passed": True,
                    "cli_exit_code": result.returncode,
                    "root_error_in_log": True,
                    "root_error_in_trace": True,
                    "output": str(output),
                }
            )
        )


if __name__ == "__main__":
    main()
