"""Ensure the Debian-provided QEMU executables are available and usable."""

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess


QEMU_IMG = Path("/usr/bin/qemu-img")
QEMU_SYSTEM = Path("/usr/bin/qemu-system-x86_64")
QEMU_PACKAGES = ("qemu-system-x86", "qemu-utils")
COMMAND_TIMEOUT_SECONDS = 30
APT_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class QemuCompatibilityResult:
    available: bool
    detail: str = ""


_result = None


def _alert(message):
    print(f"[QEMU compatibility] {message}", flush=True)


def _output_text(output):
    text = (output or "").strip()
    return text[-2000:] if text else "no diagnostic output"


def _test_qemu_img():
    if not QEMU_IMG.is_file():
        return False, f"{QEMU_IMG} was not found"

    try:
        result = subprocess.run(
            (str(QEMU_IMG), "--version"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"could not start qemu-img: {error}"

    if result.returncode:
        return False, f"qemu-img exited with {result.returncode}: {_output_text(result.stdout)}"
    return True, ""


def _test_qemu_system():
    if not QEMU_SYSTEM.is_file():
        return False, f"{QEMU_SYSTEM} was not found"

    process = None
    try:
        process = subprocess.Popen(
            (
                str(QEMU_SYSTEM),
                "-machine", "none",
                "-display", "none",
                "-S",
            ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
        )
        try:
            output, _ = process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            # Reaching the timeout means QEMU started and is waiting as requested.
            process.terminate()
            try:
                process.communicate(timeout=COMMAND_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            return True, ""

        return False, f"qemu-system-x86_64 exited with {process.returncode}: {_output_text(output)}"
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"could not start qemu-system-x86_64: {error}"
    finally:
        if process and process.poll() is None:
            process.kill()
            process.communicate()


def _test_qemu():
    img_ok, img_detail = _test_qemu_img()
    if not img_ok:
        return False, img_detail
    return _test_qemu_system()


def _safe_test_qemu():
    """Keep a failed host probe from preventing the bot itself from starting."""
    try:
        return _test_qemu()
    except Exception as error:
        return False, f"unexpected QEMU verification error: {error}"


def _run_apt(arguments, description):
    apt_get = shutil.which("apt-get")
    if not apt_get:
        _alert("WARNING: apt-get was not found, so QEMU cannot be installed automatically.")
        return False

    _alert(description)
    environment = os.environ.copy()
    environment["DEBIAN_FRONTEND"] = "noninteractive"
    try:
        result = subprocess.run(
            (apt_get, *arguments),
            timeout=APT_TIMEOUT_SECONDS,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        _alert(f"WARNING: apt-get could not run: {error}")
        return False

    if result.returncode:
        _alert(f"WARNING: apt-get failed with exit code {result.returncode}. See the apt output above.")
        return False
    _alert("apt-get completed successfully.")
    return True


def _install_qemu():
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        _alert(
            "WARNING: automatic QEMU installation needs root privileges. "
            "Run 'sudo apt-get update && sudo apt-get install -y qemu-system-x86 qemu-utils'."
        )
        return False

    updated = _run_apt(("update",), "QEMU did not start; updating apt package lists...")
    if not updated:
        _alert("WARNING: apt update failed; trying the install with the existing package cache.")

    return _run_apt(
        ("install", "-y", *QEMU_PACKAGES),
        "Installing Debian QEMU packages (qemu-system-x86 and qemu-utils)...",
    )


def ensure_qemu_available():
    """Return the cached result after testing or installing Debian QEMU once."""
    global _result
    if _result is not None:
        return _result

    available, detail = _safe_test_qemu()
    if available:
        _alert("System QEMU passed its startup check.")
        _result = QemuCompatibilityResult(True)
        return _result

    _alert(f"System QEMU startup check failed: {detail}")
    try:
        _install_qemu()
    except Exception as error:
        _alert(f"WARNING: automatic QEMU installation failed unexpectedly: {error}")

    available, detail = _safe_test_qemu()
    if available:
        _alert("System QEMU was installed and passed its startup check.")
        _result = QemuCompatibilityResult(True)
        return _result

    _alert(
        "WARNING: system QEMU is unavailable. The bot will continue running, but VM executions will not work. "
        "It is installed automatically with apt when possible; otherwise run "
        "'apt-get update && apt-get install -y qemu-system-x86 qemu-utils'. "
        f"Last check: {detail}"
    )
    _result = QemuCompatibilityResult(False, detail)
    return _result
