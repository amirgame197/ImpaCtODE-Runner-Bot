"""Check that the bundled Linux QEMU binaries can start.

The executables stay bundled, but their libraries must come from the host
distribution. In particular, a bundled ``libc.so.6`` cannot safely be loaded
through ``LD_LIBRARY_PATH``.
"""

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import subprocess


BUNDLED_BIN = Path(__file__).resolve().parents[1] / "Binaries" / "Linux" / "bin"
QEMU_IMG = BUNDLED_BIN / "qemu-img"
QEMU_SYSTEM = BUNDLED_BIN / "qemu-system-x86_64"
HOST_DEPENDENCY_PACKAGES = ("qemu-system-x86", "qemu-utils")
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
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            timeout=APT_TIMEOUT_SECONDS,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        _alert(f"WARNING: apt-get could not run: {error}")
        return False

    if result.returncode:
        _alert(f"WARNING: apt-get failed with exit code {result.returncode}: {_output_text(result.stdout)}")
        return False
    return True


def _install_host_dependencies():
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        _alert(
            "WARNING: automatic host-dependency installation needs root privileges. "
            "Run 'sudo apt-get update && sudo apt-get install -y qemu-system-x86 qemu-utils'."
        )
        return False

    updated = _run_apt(("update",), "The bundled QEMU binary did not start; updating apt package lists...")
    if not updated:
        _alert("WARNING: apt update failed; trying the install with the existing package cache.")

    return _run_apt(
        ("install", "-y", *HOST_DEPENDENCY_PACKAGES),
        "Installing Debian packages that provide QEMU's host-compatible dependencies...",
    )


def ensure_qemu_available():
    """Return the cached result after testing or installing Debian QEMU once."""
    global _result
    if _result is not None:
        return _result

    available, detail = _safe_test_qemu()
    if available:
        _alert("Bundled QEMU passed its startup check.")
        _result = QemuCompatibilityResult(True)
        return _result

    _alert(f"Bundled QEMU startup check failed: {detail}")
    try:
        _install_host_dependencies()
    except Exception as error:
        _alert(f"WARNING: automatic QEMU installation failed unexpectedly: {error}")

    available, detail = _safe_test_qemu()
    if available:
        _alert("The required host packages were installed and bundled QEMU passed its startup check.")
        _result = QemuCompatibilityResult(True)
        return _result

    _alert(
        "WARNING: the bundled QEMU binary cannot start. The bot will continue running, but VM executions will not work. "
        "Install its host-compatible dependencies with 'apt-get update && apt-get install -y qemu-system-x86 qemu-utils'. "
        f"Last check: {detail}"
    )
    _result = QemuCompatibilityResult(False, detail)
    return _result
