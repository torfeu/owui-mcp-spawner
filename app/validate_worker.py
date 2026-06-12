"""
Subprocess worker for tool-code validation.

Reads Python source from stdin, runs the validation (which execs the code)
isolated from the manager process, and prints the result as JSON to stdout.
Invoked by tool_editor.validate_tool_code() as: python -m app.validate_worker
"""
import json
import sys

from app.tool_editor import _validate_in_process


def main() -> None:
    code = sys.stdin.read()
    print(json.dumps(_validate_in_process(code)))


if __name__ == "__main__":
    main()
