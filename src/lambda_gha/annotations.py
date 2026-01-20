"""GitHub Actions annotation helpers."""

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lambda_gha.errors import LaunchAttempt


def emit_warning(title: str, message: str):
    """Emit a GitHub Actions warning annotation.

    Parameters
    ----------
    title : str
        Short title for the warning.
    message : str
        Detailed warning message.
    """
    # Escape special characters for workflow commands
    message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::warning title={title}::{message}")


def emit_error(title: str, message: str):
    """Emit a GitHub Actions error annotation.

    Parameters
    ----------
    title : str
        Short title for the error.
    message : str
        Detailed error message.
    """
    message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::error title={title}::{message}")


def emit_notice(title: str, message: str):
    """Emit a GitHub Actions notice annotation.

    Parameters
    ----------
    title : str
        Short title for the notice.
    message : str
        Detailed notice message.
    """
    message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::notice title={title}::{message}")


def write_summary(markdown: str):
    """Write markdown to the GitHub Actions job summary.

    Parameters
    ----------
    markdown : str
        Markdown content to append to the job summary.
    """
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(markdown + "\n")


def format_launch_summary(
    attempts: list["LaunchAttempt"],
    success: bool,
    instance_id: str = "",
    ip: str = "",
) -> str:
    """Format launch attempts as a markdown summary table.

    Parameters
    ----------
    attempts : list[LaunchAttempt]
        List of launch attempts made.
    success : bool
        Whether a launch eventually succeeded.
    instance_id : str
        The instance ID if successful.
    ip : str
        The instance IP if successful.

    Returns
    -------
    str
        Markdown-formatted summary.
    """
    lines = [
        "## Lambda Instance Launch",
        "",
        "| # | Instance Type | Region | Result |",
        "|---|---------------|--------|--------|",
    ]

    for i, attempt in enumerate(attempts, 1):
        if attempt.success:
            result = "✅ Launched"
        elif "capacity" in attempt.error.lower():
            result = "⚠️ No capacity"
        elif "rate" in attempt.error.lower():
            result = "⚠️ Rate limited"
        else:
            result = f"❌ {attempt.error[:30]}"

        lines.append(f"| {i} | `{attempt.instance_type}` | {attempt.region} | {result} |")

    lines.append("")

    if success:
        lines.append(f"**Instance ID:** `{instance_id}`")
        if ip:
            lines.append(f"**IP:** `{ip}`")
    else:
        lines.append("**Result:** ❌ All options exhausted")

    return "\n".join(lines)


def emit_capacity_warning(instance_type: str, region: str, next_option: str = ""):
    """Emit a warning annotation for a capacity failure.

    Parameters
    ----------
    instance_type : str
        The instance type that failed.
    region : str
        The region that failed.
    next_option : str
        Description of what will be tried next.
    """
    msg = f"{instance_type} in {region} unavailable"
    if next_option:
        msg += f", trying {next_option}"
    emit_warning("Capacity Retry", msg)


def emit_all_exhausted_error(attempts: list["LaunchAttempt"]):
    """Emit an error annotation when all capacity is exhausted.

    Parameters
    ----------
    attempts : list[LaunchAttempt]
        All launch attempts that were made.
    """
    types = sorted(set(a.instance_type for a in attempts))
    regions = sorted(set(a.region for a in attempts))
    msg = f"All options exhausted. Tried: {', '.join(types)} in {', '.join(regions)}"
    emit_error("No Capacity", msg)
