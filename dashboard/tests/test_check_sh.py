"""ci/check.sh must report a failure, never become one.

The file's own contract: "A dead docker/vtysh/curl is a FAIL, never a PASS.
Exit = FAIL count." Under `set -euo pipefail` a command substitution that fails
takes the whole script with it — so the row is never printed, the `N FAIL`
summary is never printed, and the exit status reads as one failure rather than
the rows it owed. Measured against a 200 carrying an HTML error page.

The line is read out of the script rather than copied here, so this cannot pass
against a version of check.sh that no longer contains it.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CHECK = Path(__file__).resolve().parents[2] / "ci" / "check.sh"
BASH = shutil.which("bash")


def last_id_line() -> str:
    for line in CHECK.read_text().splitlines():
        if re.match(r"\s*last_id=\$\(", line):
            return line.strip()
    raise AssertionError("no `last_id=$(...)` line in ci/check.sh — did it change shape?")


@pytest.mark.skipif(not BASH, reason="no bash")
@pytest.mark.parametrize("body", [
    "<html>502 Bad Gateway</html>",     # a proxy answered instead of the app
    "",                                 # an empty 200
    '{"ready": true}',                  # valid JSON, no lastId
])
def test_a_bad_events_body_does_not_abort_the_check(body):
    script = f"""
set -euo pipefail
ev_raw={body!r}
{last_id_line()}
echo "reached the summary with last_id=${{last_id}}"
"""
    proc = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, f"check.sh aborts on this body: {proc.stderr.strip()[-200:]}"
    assert proc.stdout.strip() == "reached the summary with last_id=0"
