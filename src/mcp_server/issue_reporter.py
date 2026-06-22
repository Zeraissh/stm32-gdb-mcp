"""File GitHub issues for MCP problems, straight from the failing session.

Lets an agent self-report a problem with one call: this bundles the session
journal, metrics, and MCP version into a structured issue and files it via the
``gh`` CLI — so the report is complete and goes to the right repo regardless of
which agent/IDE is in use (no dependency on a separate GitHub MCP). In-session
deduplication (see server.report_issue) keeps a retry loop from spamming issues.
"""

import hashlib
import os
import subprocess

DEFAULT_REPO = "Zeraissh/stm32-gdb-mcp"


def issue_fingerprint(title: str) -> str:
    return hashlib.sha1((title or "").strip().lower().encode("utf-8")).hexdigest()[:12]


def build_issue_body(description=None, env=None, version=None, journal=None, metrics=None) -> str:
    lines = [
        "## What the agent was doing",
        description or "(not provided)",
        "",
        "## Environment",
        env or "(not provided)",
        "",
        "## MCP version",
        version or "unknown",
    ]
    if metrics:
        totals = metrics.get("totals", {})
        lines += ["", "## Session metrics",
                  f"calls={totals.get('calls')} ok={totals.get('ok')} failed={totals.get('failed')}"]
    if journal:
        lines += ["", "## Recent journal"]
        for e in journal:
            status = "ok" if e.get("ok") else f"ERR:{e.get('error')}"
            lines.append(f"- #{e.get('seq')} {e.get('tool')} -> {status} {e.get('summary') or ''}".rstrip())
    lines += ["", "---", "_Filed automatically by the stm32-gdb-mcp report_issue tool._"]
    return "\n".join(lines)


def file_issue(repo: str, title: str, body: str, runner=None, timeout: int = 20) -> dict:
    """File a GitHub issue via the gh CLI. Returns {ok, url} or {ok:False, error, body}.

    Forces gh to run NON-INTERACTIVELY so it can never hang waiting for a prompt
    (auth, template choice, credential dialog) — it fails fast instead, and the
    prepared body is returned so the caller can file it another way.
    """
    runner = runner or subprocess.run
    cmd = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body-file", "-"]
    env = dict(os.environ)
    env["GH_PROMPT_DISABLED"] = "1"      # never open an interactive prompt
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    try:
        proc = runner(cmd, input=body, capture_output=True, text=True, timeout=timeout, env=env)
    except FileNotFoundError:
        return {"ok": False, "error": "gh CLI not found — install/auth GitHub CLI or file manually.", "body": body}
    except subprocess.TimeoutExpired:
        return {"ok": False,
                "error": f"gh timed out after {timeout}s (is it authenticated? run 'gh auth login').",
                "body": body}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "body": body}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or "gh issue create failed").strip(), "body": body}
    return {"ok": True, "url": (proc.stdout or "").strip()}
