from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


REPO_DIR = Path(__file__).resolve().parents[1]
HELPER_PATH = REPO_DIR / "quick_ask_helper.py"
SPEC = importlib.util.spec_from_file_location("quick_ask_helper", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HELPER
SPEC.loader.exec_module(HELPER)


class HelperIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="quick-ask-test-")
        self.root = Path(self.temporary.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir(mode=0o700)
        self.environment = os.environ.copy()
        self.environment["PATH"] = f"{self.bin_dir}:/usr/bin"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_executable(self, name: str, source: str) -> Path:
        path = self.bin_dir / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o700)
        return path

    def select_agent(self, agent: str) -> None:
        self.write_executable(
            "omarchy-default-agent",
            f"#!/usr/bin/env python3\nprint({agent!r})\n",
        )

    def run_helper(
        self,
        action: str,
        request: bytes = b"",
        *extra_arguments: str,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(HELPER_PATH), action, *extra_arguments],
            input=request,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment,
            timeout=10,
            check=False,
        )

    @staticmethod
    def record(result: subprocess.CompletedProcess[bytes]) -> dict[str, object]:
        return json.loads(result.stdout.decode("utf-8"))

    def test_detect_returns_only_supported_canonical_agent(self) -> None:
        self.select_agent("codex")
        result = self.run_helper("detect")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.record(result), {"ok": True, "agent": "codex"})

    def test_codex_receives_prompt_on_stdin_and_not_in_proc_cmdline(self) -> None:
        self.select_agent("codex")
        pid_file = self.root / "agent.pid"
        argv_file = self.root / "agent.argv"
        stdin_file = self.root / "agent.stdin"
        environment_file = self.root / "agent.env"
        cwd_file = self.root / "agent.cwd"
        self.environment["UNRELATED_SECRET"] = "MUST_NOT_REACH_AGENT"
        self.environment["OPENAI_API_KEY"] = "REQUIRED_OPENAI_CREDENTIAL"
        self.write_executable(
            "codex",
            f"""#!/usr/bin/env python3
import os
from pathlib import Path
import sys
import time

Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding="utf-8")
Path({str(argv_file)!r}).write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
Path({str(environment_file)!r}).write_text("\\n".join(sorted(os.environ)), encoding="utf-8")
Path({str(cwd_file)!r}).write_text(os.getcwd(), encoding="utf-8")
prompt = sys.stdin.read()
Path({str(stdin_file)!r}).write_text(prompt, encoding="utf-8")
time.sleep(0.75)
output_path = sys.argv[sys.argv.index("--output-last-message") + 1]
with open(output_path, "w", encoding="utf-8") as output:
    output.write("SAFE_ANSWER")
""",
        )

        secret = "PROC_SENTINEL_7f408db78fd5"
        process = subprocess.Popen(
            [sys.executable, str(HELPER_PATH), "ask"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.environment,
        )
        assert process.stdin is not None
        process.stdin.write(json.dumps({"prompt": secret}).encode("utf-8") + b"\n")
        process.stdin.close()
        process.stdin = None

        deadline = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(pid_file.exists(), "mock Codex did not start")
        agent_pid = int(pid_file.read_text("utf-8"))
        for pid in (process.pid, agent_pid):
            command_line = Path(f"/proc/{pid}/cmdline").read_bytes()
            self.assertNotIn(secret.encode("utf-8"), command_line)

        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout), {"ok": True, "agent": "codex", "answer": "SAFE_ANSWER"})
        self.assertEqual(stdin_file.read_text("utf-8"), secret)
        codex_arguments = argv_file.read_text("utf-8").splitlines()
        self.assertNotIn(secret, codex_arguments)
        self.assertIn("--ephemeral", codex_arguments)
        self.assertIn("read-only", codex_arguments)
        self.assertIn("--ignore-user-config", codex_arguments)
        self.assertIn("--ignore-rules", codex_arguments)
        agent_environment = environment_file.read_text("utf-8").splitlines()
        self.assertIn("OPENAI_API_KEY", agent_environment)
        self.assertNotIn("UNRELATED_SECRET", agent_environment)
        self.assertRegex(cwd_file.read_text("utf-8"), r"/quick-ask-agent-[^/]+$")
        output_index = codex_arguments.index("--output-last-message") + 1
        self.assertRegex(codex_arguments[output_index], r"^/proc/self/fd/\d+$")

    def test_codex_full_configuration_requires_explicit_opt_in(self) -> None:
        self.select_agent("codex")
        argv_file = self.root / "agent.argv"
        self.write_executable(
            "codex",
            f"""#!/usr/bin/env python3
from pathlib import Path
import sys

Path({str(argv_file)!r}).write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
sys.stdin.read()
output_path = sys.argv[sys.argv.index("--output-last-message") + 1]
Path(output_path).write_text("ANSWER", encoding="utf-8")
""",
        )
        result = self.run_helper(
            "ask",
            b'{"prompt":"hello"}\n',
            "--inherit-agent-config",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = argv_file.read_text("utf-8").splitlines()
        self.assertNotIn("--ignore-user-config", arguments)
        self.assertNotIn("--ignore-rules", arguments)
        self.assertIn("read-only", arguments)

    def test_codex_multicall_shim_keeps_its_invocation_name(self) -> None:
        self.select_agent("codex")
        launcher = self.write_executable(
            "mise",
            """#!/usr/bin/env python3
import os
from pathlib import Path
import sys

if os.path.basename(sys.argv[0]) != "codex":
    print("error: unexpected argument '--ignore-user-config' found", file=sys.stderr)
    raise SystemExit(2)
sys.stdin.read()
output_path = sys.argv[sys.argv.index("--output-last-message") + 1]
Path(output_path).write_text("SHIM_ANSWER", encoding="utf-8")
""",
        )
        (self.bin_dir / "codex").symlink_to(launcher)

        result = self.run_helper("ask", b'{"prompt":"hello"}\n')

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.record(result)["answer"], "SHIM_ANSWER")

    def test_codex_internal_files_are_not_subject_to_the_answer_limit(self) -> None:
        self.select_agent("codex")
        internal_file = self.root / "codex-internal.db"
        self.write_executable(
            "codex",
            f"""#!/usr/bin/env python3
from pathlib import Path
import sys

Path({str(internal_file)!r}).write_bytes(b"x" * ({HELPER.MAX_ANSWER_BYTES} + 1))
sys.stdin.read()
output_path = sys.argv[sys.argv.index("--output-last-message") + 1]
Path(output_path).write_text("SMALL_ANSWER", encoding="utf-8")
""",
        )

        result = self.run_helper("ask", b'{"prompt":"hello"}\n')

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.record(result)["answer"], "SMALL_ANSWER")
        self.assertGreater(internal_file.stat().st_size, HELPER.MAX_ANSWER_BYTES)

    def test_codex_answer_pipe_enforces_its_own_byte_limit(self) -> None:
        self.select_agent("codex")
        self.write_executable(
            "codex",
            f"""#!/usr/bin/env python3
import os
import sys

sys.stdin.read()
output_path = sys.argv[sys.argv.index("--output-last-message") + 1]
output_fd = os.open(output_path, os.O_WRONLY)
os.write(output_fd, b"x" * ({HELPER.MAX_ANSWER_BYTES} + 1))
os.close(output_fd)
""",
        )

        result = self.run_helper("ask", b'{"prompt":"hello"}\n')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("answer exceeded", str(self.record(result)["error"]))

    def test_claude_receives_prompt_on_stdin_without_prompt_argument(self) -> None:
        self.select_agent("claude")
        argv_file = self.root / "claude.argv"
        stdin_file = self.root / "claude.stdin"
        environment_file = self.root / "claude.env"
        self.environment["UNRELATED_SECRET"] = "MUST_NOT_REACH_AGENT"
        self.environment["ANTHROPIC_API_KEY"] = "REQUIRED_ANTHROPIC_CREDENTIAL"
        self.write_executable(
            "claude",
            f"""#!/usr/bin/env python3
import os
from pathlib import Path
import sys

Path({str(argv_file)!r}).write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
Path({str(environment_file)!r}).write_text("\\n".join(sorted(os.environ)), encoding="utf-8")
prompt = sys.stdin.read()
Path({str(stdin_file)!r}).write_text(prompt, encoding="utf-8")
print("CLAUDE_ANSWER")
""",
        )
        secret = "CLAUDE_STDIN_SENTINEL"
        result = self.run_helper("ask", json.dumps({"prompt": secret}).encode("utf-8") + b"\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.record(result)["answer"], "CLAUDE_ANSWER")
        self.assertEqual(stdin_file.read_text("utf-8"), secret)
        claude_arguments = argv_file.read_text("utf-8").splitlines()
        self.assertNotIn(secret, claude_arguments)
        self.assertIn("plan", claude_arguments)
        self.assertIn("--restricted", claude_arguments)
        self.assertIn("--no-session-persistence", claude_arguments)
        self.assertIn("mcp__*", claude_arguments)
        agent_environment = environment_file.read_text("utf-8").splitlines()
        self.assertIn("ANTHROPIC_API_KEY", agent_environment)
        self.assertNotIn("UNRELATED_SECRET", agent_environment)

    def test_claude_full_configuration_requires_explicit_opt_in(self) -> None:
        self.select_agent("claude")
        argv_file = self.root / "claude.argv"
        self.write_executable(
            "claude",
            f"""#!/usr/bin/env python3
from pathlib import Path
import sys

Path({str(argv_file)!r}).write_text("\\n".join(sys.argv[1:]), encoding="utf-8")
sys.stdin.read()
print("ANSWER")
""",
        )
        result = self.run_helper(
            "ask",
            b'{"prompt":"hello"}\n',
            "--inherit-agent-config",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        arguments = argv_file.read_text("utf-8").splitlines()
        self.assertNotIn("--restricted", arguments)
        self.assertNotIn("mcp__*", arguments)
        self.assertIn("plan", arguments)
        self.assertIn("--no-session-persistence", arguments)

    def test_unsupported_agent_fails_closed(self) -> None:
        self.select_agent("gemini")
        result = self.run_helper("ask", b'{"prompt":"hello"}\n')
        self.assertNotEqual(result.returncode, 0)
        record = self.record(result)
        self.assertFalse(record["ok"])
        self.assertIn("supports claude, codex", str(record["error"]))

    def test_transport_and_prompt_limits_are_enforced_before_agent_start(self) -> None:
        self.select_agent("codex")
        result = self.run_helper("ask", b"x" * (HELPER.MAX_REQUEST_BYTES + 1))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("transport limit", str(self.record(result)["error"]))

        prompt = "x" * (HELPER.MAX_PROMPT_BYTES + 1)
        result = self.run_helper("ask", json.dumps({"prompt": prompt}).encode("utf-8") + b"\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Prompt must contain", str(self.record(result)["error"]))

    def test_malformed_or_extra_request_fields_are_rejected(self) -> None:
        for request in (b"not-json\n", b'{"prompt":"ok","model":"unsafe"}\n', b'{"prompt":7}\n'):
            with self.subTest(request=request):
                result = self.run_helper("ask", request)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.record(result)["ok"])


class ProcessSupervisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        HELPER._enable_subreaper()

    def test_deadline_applies_while_agent_refuses_to_read_stdin(self) -> None:
        with self.assertRaises(HELPER.ProcessDeadline):
            HELPER.run_bounded(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin_data=b"x" * HELPER.MAX_PROMPT_BYTES,
                timeout_seconds=0.3,
                stdout_limit=1024,
                stderr_limit=1024,
            )

    def test_stdout_overflow_terminates_producer(self) -> None:
        with self.assertRaises(HELPER.OutputOverflow):
            HELPER.run_bounded(
                [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 4096); sys.stdout.flush()"],
                stdin_data=b"",
                timeout_seconds=2,
                stdout_limit=1024,
                stderr_limit=1024,
            )

    def test_stderr_overflow_terminates_producer(self) -> None:
        with self.assertRaises(HELPER.OutputOverflow):
            HELPER.run_bounded(
                [sys.executable, "-c", "import sys; sys.stderr.buffer.write(b'x' * 4096); sys.stderr.flush()"],
                stdin_data=b"",
                timeout_seconds=2,
                stdout_limit=1024,
                stderr_limit=1024,
            )

    def test_deadline_kills_and_reaps_process_group(self) -> None:
        with tempfile.TemporaryDirectory(prefix="quick-ask-tree-") as directory:
            child_pid_path = Path(directory) / "child.pid"
            source = """
import pathlib
import subprocess
import sys
import time
p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
pathlib.Path(sys.argv[1]).write_text(str(p.pid), encoding='utf-8')
time.sleep(30)
"""
            with self.assertRaises(HELPER.ProcessDeadline):
                HELPER.run_bounded(
                    [sys.executable, "-c", source, str(child_pid_path)],
                    stdin_data=b"",
                    timeout_seconds=0.3,
                    stdout_limit=1024,
                    stderr_limit=1024,
                )
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text("utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)

    def test_deadline_kills_descendant_that_escaped_process_group(self) -> None:
        with tempfile.TemporaryDirectory(prefix="quick-ask-escaped-tree-") as directory:
            child_pid_path = Path(directory) / "child.pid"
            source = """
import pathlib
import subprocess
import sys
import time
p = subprocess.Popen(
    [sys.executable, '-c', 'import time; time.sleep(30)'],
    start_new_session=True,
)
pathlib.Path(sys.argv[1]).write_text(str(p.pid), encoding='utf-8')
time.sleep(30)
"""
            with self.assertRaises(HELPER.ProcessDeadline):
                HELPER.run_bounded(
                    [sys.executable, "-c", source, str(child_pid_path)],
                    stdin_data=b"",
                    timeout_seconds=0.3,
                    stdout_limit=1024,
                    stderr_limit=1024,
                )
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text("utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)


if __name__ == "__main__":
    unittest.main()
