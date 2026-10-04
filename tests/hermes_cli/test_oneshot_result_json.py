"""``hermes -z --output-format json`` writes one agentctl generic-process terminal record.

The default text path stays plain stdout. JSON mode is opt-in and does not call a provider:
the agent is stubbed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli.oneshot_result import build_oneshot_result_record, format_oneshot_result_line

_REPO = Path(__file__).resolve().parents[2]


def _loads(line: str) -> dict:
    assert line.endswith("\n")
    assert line.count("\n") == 1
    return json.loads(line)


def test_success_record_carries_the_answer_and_optional_usage():
    record = build_oneshot_result_record(
        response="OK",
        result={
            "final_response": "OK",
            "completed": True,
            "failed": False,
            "model": "stub-model",
            "provider": "stub",
            "session_id": "sess-1",
            "input_tokens": 3,
            "output_tokens": 1,
            "estimated_cost_usd": 0.0,
        },
        exit_code=0,
    )
    assert record["type"] == "result"
    assert record["status"] == "success"
    assert record["success"] is True
    assert record["is_error"] is False
    assert record["result"] == "OK"
    assert "error" not in record
    assert record["model"] == "stub-model"
    assert record["session_id"] == "sess-1"
    assert record["usage"]["input_tokens"] == 3
    assert record["usage"]["output_tokens"] == 1
    parsed = _loads(format_oneshot_result_line(record))
    assert parsed["result"] == record["result"]
    assert parsed["status"] == "success"


def test_failure_record_keeps_the_answer_and_the_error():
    record = build_oneshot_result_record(
        response="Got as far as step 2.",
        result={"failed": True, "completed": False, "error": "provider exploded", "final_response": "Got as far as step 2."},
        exit_code=2,
        failure="provider exploded",
    )
    assert record["status"] == "failed"
    assert record["success"] is False
    assert record["is_error"] is True
    assert record["result"] == "Got as far as step 2."
    assert record["error"] == "provider exploded"
    parsed = _loads(format_oneshot_result_line(record))
    assert parsed["error"] == "provider exploded"
    assert parsed["result"] == "Got as far as step 2."


def test_failure_without_answer_stores_the_error_as_the_result():
    record = build_oneshot_result_record(
        response="",
        result={"failed": True, "error": "tool crashed"},
        exit_code=1,
        failure="tool crashed",
    )
    assert record["status"] == "failed"
    assert record["result"] == "tool crashed"
    assert record["error"] == "tool crashed"


def test_large_answer_is_one_json_line_and_is_not_truncated():
    answer = ("line\n" + "x" * 250_000 + " \"quote\\")
    line = format_oneshot_result_line(build_oneshot_result_record(
        response=answer,
        result={"completed": True, "final_response": answer},
        exit_code=0,
    ))
    parsed = _loads(line)
    assert parsed["status"] == "success"
    assert parsed["result"] == answer
    assert len(parsed["result"]) == len(answer)


def test_unicode_answer_roundtrips_without_ascii_escapes():
    answer = "OK 界 — café"
    line = format_oneshot_result_line(build_oneshot_result_record(
        response=answer,
        result={"completed": True, "final_response": answer},
        exit_code=0,
    ))
    assert "界" in line
    assert "\\u754c" not in line
    assert _loads(line)["result"] == answer


def test_lone_surrogate_in_the_answer_becomes_replacement_char():
    dirty = "answer \ud800 here"
    record = build_oneshot_result_record(
        response=dirty,
        result={"completed": True, "final_response": dirty},
        exit_code=0,
    )
    assert "\ud800" not in record["result"]
    assert "\ufffd" in record["result"]
    # The line itself must be encodable as UTF-8.
    format_oneshot_result_line(record).encode("utf-8")


def _run_cli(home: Path, program: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [sys.executable, "-c", program],
        cwd=_REPO,
        capture_output=True,
        timeout=60,
        check=False,
        env=env,
    )


def test_cli_json_flag_with_stubbed_agent_prints_only_the_record(tmp_path):
    program = textwrap.dedent(
        """
        from hermes_cli._parser import build_top_level_parser
        import hermes_cli.oneshot as oneshot

        def fake_agent(*args, **kwargs):
            return ("OK", {
                "final_response": "OK",
                "completed": True,
                "failed": False,
                "model": "stub-model",
                "session_id": "sess-cli",
                "input_tokens": 2,
                "output_tokens": 1,
            })

        oneshot._run_agent = fake_agent
        parser = build_top_level_parser()[0]
        args = parser.parse_args(["--output-format", "json", "-z", "reply with exactly the word OK"])
        raise SystemExit(oneshot.run_oneshot(
            args.oneshot, output_format=args.oneshot_output_format,
        ))
        """
    )
    result = _run_cli(tmp_path, program)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    stdout = result.stdout.decode("utf-8")
    record = _loads(stdout)
    assert record["type"] == "result"
    assert record["status"] == "success"
    assert record["result"] == "OK"
    assert record["model"] == "stub-model"
    assert b"Traceback" not in result.stderr


def test_cli_json_flag_failure_is_a_record_not_a_traceback(tmp_path):
    program = textwrap.dedent(
        """
        from hermes_cli._parser import build_top_level_parser
        import hermes_cli.oneshot as oneshot

        def fake_agent(*args, **kwargs):
            raise RuntimeError("tool crashed")

        oneshot._run_agent = fake_agent
        parser = build_top_level_parser()[0]
        args = parser.parse_args(["-z", "do the thing", "--output-format", "json"])
        raise SystemExit(oneshot.run_oneshot(
            args.oneshot, output_format=args.oneshot_output_format,
        ))
        """
    )
    result = _run_cli(tmp_path, program)
    assert result.returncode == 1
    stdout = result.stdout.decode("utf-8")
    assert "Traceback" not in stdout
    record = _loads(stdout)
    assert record["status"] == "failed"
    assert record["is_error"] is True
    assert "tool crashed" in record["error"]
    assert record["result"]
    assert b"tool crashed" in result.stderr
    assert b"Traceback" not in result.stdout


def test_cli_default_oneshot_stdout_stays_plain_text(tmp_path):
    program = textwrap.dedent(
        """
        from hermes_cli._parser import build_top_level_parser
        import hermes_cli.oneshot as oneshot

        oneshot._run_agent = lambda *args, **kwargs: (
            "OK",
            {"final_response": "OK", "completed": True, "failed": False},
        )
        parser = build_top_level_parser()[0]
        args = parser.parse_args(["-z", "reply with exactly the word OK"])
        assert args.oneshot_output_format == "text"
        raise SystemExit(oneshot.run_oneshot(
            args.oneshot, output_format=args.oneshot_output_format,
        ))
        """
    )
    result = _run_cli(tmp_path, program)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    assert result.stdout == b"OK\n"


def test_interrupted_json_run_writes_a_failure_record_before_propagating(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import hermes_cli.oneshot as oneshot

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(oneshot, "_run_agent", boom)
    import io
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    with pytest.raises(KeyboardInterrupt) as caught:
        oneshot.run_oneshot("stop", output_format="json")
    assert getattr(caught.value, "oneshot_record_written", False) is True
    record = _loads(buf.getvalue())
    assert record["status"] == "failed"
    assert record["error"] == "Interrupted"
    assert record["result"] == "Interrupted"


def test_failure_record_redacts_secrets():
    from hermes_cli.oneshot_result import build_oneshot_result_record

    token = "sk-" + "a" * 48
    record = build_oneshot_result_record(
        response=None, result={}, exit_code=1, failure=f"provider rejected key {token}"
    )
    assert token not in record["error"]
    assert token not in record["result"]
    assert record["status"] == "failed"
