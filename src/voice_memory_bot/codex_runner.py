"""One isolated Codex CLI process contract for text, folder names and images."""
import asyncio
import json
import os
import signal
from pathlib import Path

from .config import LLMConfig
from .errors import ProviderError


class CodexRunner:
    def __init__(self, config: LLMConfig):
        self.config = config

    @staticmethod
    def environment():
        # Explicit allowlist prevents inherited API keys, remote endpoints and parent agent settings.
        allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "CODEX_HOME", "XDG_CONFIG_HOME",
                   "XDG_DATA_HOME", "XDG_CACHE_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR"}
        return {k: v for k, v in os.environ.items() if k in allowed or k.startswith("LC_")}

    async def _run(self, args, *, data=None, timeout=10, capture=False):
        try:
            process = await asyncio.create_subprocess_exec(self.config.binary, *args,
                stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                env=self.environment(), start_new_session=True)
        except OSError:
            raise ProviderError("codex_missing") from None
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(data), timeout)
            return process.returncode, (stdout or b"") + (stderr or b"")
        except (TimeoutError, asyncio.CancelledError):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            if asyncio.current_task().cancelling():
                raise
            raise ProviderError("codex_timeout") from None

    async def check_auth(self):
        status, output = await self._run(["login", "status"], capture=True)
        if status or b"Logged in using ChatGPT" not in output:
            raise ProviderError("chatgpt_login_required")

    def command(self, work: Path):
        args = ["exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                "--sandbox", "read-only", "--model", self.config.model, "-C", str(work),
                "--output-schema", str(work / "schema.json"),
                "--output-last-message", str(work / "result.json")]
        overrides = {
            "model_instructions_file": str(work / "instructions.txt"),
            "model_reasoning_effort": self.config.effort,
            "forced_login_method": "chatgpt", "project_doc_max_bytes": 0,
            "web_search": "disabled", "approval_policy": "never",
        }
        for feature in ("shell_tool", "apply_patch_freeform", "multi_agent", "multi_agent_v2", "apps",
                        "plugins", "hooks", "memories", "browser_use", "computer_use", "image_generation",
                        "code_mode", "code_mode_host", "skill_search", "view_image", "sleep_tool"):
            overrides[f"features.{feature}"] = False
        for key, value in overrides.items():
            args.extend(["-c", key + "=" + json.dumps(value)])
        return [*args, "-"]

