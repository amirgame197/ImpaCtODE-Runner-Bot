from dataclasses import dataclass
from pathlib import Path
import contextlib
import platform
import asyncio
import base64
import ctypes
import config
import shlex
import stat
import uuid
import os

_accelerator = None

class EnvironmentError(Exception):
    pass

class EnvironmentTimeoutError(EnvironmentError):
    pass


def fix_qemu_permissions(folder):
    if platform.system() == "Linux":
        for file in Path(folder).iterdir():
            if file.is_file():
                file.chmod(file.stat().st_mode | stat.S_IEXEC)


for qemu_executable in config.qemu_executable["Linux"].values():
    if Path(qemu_executable).is_file():
        fix_qemu_permissions(Path(qemu_executable).parent)


@dataclass
class CommandResult:
    command: str
    exit_code: int
    output: str


def platform_key():
    key = platform.system()
    if key not in config.qemu_executable:
        raise EnvironmentError(f"QEMU is not configured for {platform.system()}.")
    return key


def qemu_subprocess_env():
    """Return the environment required by the bundled QEMU binaries.
    """
    if platform.system() != "Linux":
        return None

    environment = os.environ.copy()
    library_path = str(config.qemu_executable["Linux"]["lib"])
    existing_path = environment.get("LD_LIBRARY_PATH")
    environment["LD_LIBRARY_PATH"] = (
        f"{library_path}{os.pathsep}{existing_path}" if existing_path else library_path
    )
    return environment


def qemu_firmware_args():
    if platform.system() != "Linux":
        return []
    linux_qemu = config.qemu_executable["Linux"]
    return ["-L", str(linux_qemu["data"])]


def host_memory_mb():
    if platform.system() == "Windows":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return status.ullTotalPhys // (1024 * 1024)

    try:
        return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) // (1024 * 1024)
    except (AttributeError, OSError, ValueError):
        return 1024


async def host_virtualization():
    """Return the best usable accelerator for the current QEMU host.

    QEMU can be built with an accelerator without the host being configured to
    use it (for example, when the Windows Hypervisor Platform feature is
    disabled).  Keep QEMU running briefly with the requested accelerator to
    verify that it is usable, rather than only checking whether it is compiled
    into the bundled binary.
    """
    global _accelerator
    if _accelerator:
        return _accelerator

    key = platform_key()
    qemu = Path(config.qemu_executable[key]["qemu-system-x86_64"])
    if not qemu.is_file():
        raise EnvironmentError(f"QEMU executable was not found: {qemu}")

    candidates = {"Windows": "whpx", "Linux": "kvm"}
    accelerator = candidates.get(key)
    if accelerator is None:
        _accelerator = "tcg"
        return _accelerator

    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            str(qemu), "-accel", accelerator, "-machine", "none", "-display", "none", "-S",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=qemu.parent,
            env=qemu_subprocess_env(),
        )
        try:
            await asyncio.wait_for(process.wait(), timeout=0.75)
        except asyncio.TimeoutError:
            # A running QEMU accepted and initialized the accelerator.
            _accelerator = accelerator
            return _accelerator
    except (OSError, asyncio.SubprocessError):
        # Starting the probe itself failed, so retain QEMU's portable fallback.
        pass
    finally:
        if process and process.returncode is None:
            process.terminate()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                await asyncio.wait_for(process.wait(), timeout=2)
        if process and process.returncode is None:
            process.kill()
            with contextlib.suppress(ProcessLookupError):
                await process.wait()

    _accelerator = "tcg"
    return _accelerator


class QemuEnvironment:
    """One disposable QEMU overlay controlled through the guest serial console.
    """

    def __init__(self, language, language_overlay):
        self.language = language
        self.language_overlay = Path(language_overlay)
        self.overlay_path = None
        self.process = None
        self.guest_workdir = config.qemu_guest_workdir
        self.output = ""
        self.timed_out = False
        self.deadline = None

        self.ready = asyncio.Event()
        self.command_done = None
        self.command_marker = None
        self.command_exit_code = None
        self.command_error = None
        self.reader_task = None
        self.qemu_output_task = None
        self.destroy_lock = asyncio.Lock()
        self.serial_buffer = ""
        self.qemu_output = ""
        self.serial_server = None
        self.serial_port = None
        self.serial_reader = None
        self.serial_writer = None

    @property
    def is_running(self):
        return self.process is not None and self.process.returncode is None

    @property
    def remaining_timeout(self):
        return max(0, self.deadline - asyncio.get_running_loop().time())

    def append_output(self, text):
        self.output = (self.output + text)[-config.captured_environment_output_limit:]

    def append_qemu_output(self, text):
        self.qemu_output = (self.qemu_output + text)[-config.captured_environment_output_limit:]

    def qemu_paths(self):
        executables = config.qemu_executable[platform_key()]
        qemu_img = Path(executables["qemu-img"])
        qemu = Path(executables["qemu-system-x86_64"])
        if not qemu_img.is_file() or not qemu.is_file():
            raise EnvironmentError("QEMU binaries were not found.")
        return qemu_img, qemu

    async def create_overlay(self):
        if not self.language_overlay.is_file():
            raise EnvironmentError(f"Language overlay was not found: {self.language_overlay}")

        qemu_img, _ = self.qemu_paths()
        Path(config.sequence_overlays_dir).mkdir(parents=True, exist_ok=True)
        self.overlay_path = Path(config.sequence_overlays_dir) / f"run-{uuid.uuid4().hex}.qcow2"
        process = await asyncio.create_subprocess_exec(
            str(qemu_img), "create", "-f", "qcow2", "-b", str(self.language_overlay.resolve()),
            "-F", "qcow2", str(self.overlay_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=qemu_img.parent,
            env=qemu_subprocess_env(),
        )
        output, _ = await process.communicate()
        if process.returncode:
            raise EnvironmentError(output.decode("utf-8", errors="replace"))

    async def start_serial_server(self):
        """Listen for QEMU's dedicated guest serial connection.

        Keeping the guest console off QEMU's stdio prevents the QEMU monitor
        and its escape handling from sharing a channel with command data.
        """
        self.serial_server = await asyncio.start_server(
            self.accept_serial_connection,
            host="127.0.0.1",
            port=0,
        )
        sockets = self.serial_server.sockets
        if not sockets:
            raise EnvironmentError("Could not create the QEMU serial listener.")
        self.serial_port = sockets[0].getsockname()[1]

    async def accept_serial_connection(self, reader, writer):
        """Accept exactly one connection from the QEMU serial backend."""
        if self.serial_writer is not None:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            return

        self.serial_reader = reader
        self.serial_writer = writer
        # No other local process can take over the serial channel after QEMU.
        self.serial_server.close()
        self.reader_task = asyncio.create_task(self.read_serial_output(reader))

    async def close_serial_transport(self):
        if self.serial_server:
            self.serial_server.close()
            with contextlib.suppress(Exception):
                await self.serial_server.wait_closed()
            self.serial_server = None

        if self.serial_writer:
            self.serial_writer.close()
            with contextlib.suppress(ConnectionError):
                await self.serial_writer.wait_closed()
            self.serial_writer = None
        self.serial_reader = None

    async def read_qemu_output(self):
        """Drain QEMU diagnostics without treating them as guest serial data."""
        while True:
            data = await self.process.stdout.read(4096)
            if not data:
                break
            self.append_qemu_output(data.decode("utf-8", errors="replace"))

    async def start(self):
        """Create the disposable overlay and wait for the guest ready marker.
        """
        await self.create_overlay()
        _, qemu = self.qemu_paths()
        memory = max(1, host_memory_mb() // (config.concurrent_runs + 1)) # ? Allocate less memory to keep the host responsive when running multiple environments
        cpus = max(1, (os.cpu_count() or 1) // config.concurrent_runs)
        drive = self.overlay_path.resolve().as_posix()

        await self.start_serial_server()

        try:
            self.process = await asyncio.create_subprocess_exec(
                str(qemu),
                *qemu_firmware_args(),
                "-no-reboot",
                "-m", str(memory),
                "-smp", str(cpus),
                "-drive", f"file={drive},if=virtio,format=qcow2",
                "-device", "virtio-net-pci,netdev=net0",
                "-netdev", "user,id=net0",
                "-chardev", f"socket,id=serial0,host=127.0.0.1,port={self.serial_port},server=off",
                "-serial", "chardev:serial0",
                "-monitor", "none",
                "-display", "none",
                "-accel", await host_virtualization(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=qemu.parent,
                env=qemu_subprocess_env(),
            )
        except Exception:
            await self.close_serial_transport()
            raise

        self.deadline = asyncio.get_running_loop().time() + config.env_timeout
        self.qemu_output_task = asyncio.create_task(self.read_qemu_output())
        ready_wait = asyncio.create_task(self.ready.wait())
        process_wait = asyncio.create_task(self.process.wait())

        try:
            done, _ = await asyncio.wait(
                (ready_wait, process_wait),
                timeout=self.remaining_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready_wait not in done:
                if process_wait in done:
                    if self.reader_task:
                        await self.reader_task
                    if self.qemu_output_task:
                        await self.qemu_output_task
                    output = "\n".join(part for part in (self.output.strip(), self.qemu_output.strip()) if part)
                    details = f"\n{output}" if output else ""
                    raise EnvironmentError(f"QEMU exited before the guest was ready.{details}")
                raise EnvironmentTimeoutError("The QEMU guest did not report that it was ready before the environment timeout.")
            await asyncio.sleep(config.qemu_ready_settle_seconds)
            await self.execute("stty -echo", record_command=False)
        except Exception:
            await self.destroy()
            raise
        finally:
            for task in (ready_wait, process_wait):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def read_serial_output(self, reader):
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                self.consume_serial_text(data.decode("utf-8", errors="replace"))
        finally:
            if self.serial_buffer:
                self.append_output(self.serial_buffer)
                self.serial_buffer = ""
            if self.command_done and not self.command_done.is_set():
                self.command_error = EnvironmentError("The QEMU serial console closed before the command completed.")
                self.command_done.set()

    def consume_serial_text(self, text):
        self.serial_buffer += text
        if len(self.serial_buffer) > config.captured_environment_output_limit:
            self.append_output(self.serial_buffer[:-4096])
            self.serial_buffer = self.serial_buffer[-4096:]
        while "\n" in self.serial_buffer:
            line, self.serial_buffer = self.serial_buffer.split("\n", 1)
            self.consume_serial_line(f"{line}\n")

    def consume_serial_line(self, line):
        if config.sandbox_ready_marker in line:
            self.ready.set()

        if not self.command_marker or self.command_marker not in line:
            self.append_output(line)
            return

        before, _, after = line.partition(self.command_marker)
        exit_code = after.partition(":")[2].strip()
        try:
            self.command_exit_code = int(exit_code)
        except ValueError:
            self.append_output(line)
            return

        self.append_output(before)
        self.command_done.set()

    async def write_serial_line(self, text):
        if not self.serial_writer or self.serial_writer.is_closing():
            raise EnvironmentError("The QEMU serial console is not connected.")

        self.serial_writer.write(text.encode("utf-8"))
        try:
            await self.serial_writer.drain()
        except (BrokenPipeError, ConnectionError) as error:
            raise EnvironmentError("The QEMU serial console closed while writing a command.") from error

    async def execute(self, command, record_command=True):
        """Run one command from the guest work directory.
        """
        if not self.is_running:
            raise EnvironmentError("The QEMU environment is not running.")

        start = len(self.output)
        if record_command:
            self.append_output(f"\n$ {command}\n")

        self.command_marker = f"__IMPACTODE_COMMAND_DONE_{uuid.uuid4().hex}__"
        self.command_done = asyncio.Event()
        self.command_exit_code = None
        self.command_error = None
        encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
        directory = shlex.quote(self.guest_workdir)
        wrapped_command = (
            f"if mkdir -p {directory} && cd {directory}; then "
            f"eval \"$(printf %s {encoded} | base64 -d)\"; status=$?; "
            f"else status=$?; fi; "
            f"printf '\\n{self.command_marker}:%s\\n' \"$status\""
        )

        try:
            await self.write_serial_line(f"{wrapped_command}\n")
            await asyncio.wait_for(self.command_done.wait(), timeout=self.remaining_timeout)
        except asyncio.TimeoutError as error:
            raise EnvironmentTimeoutError("A guest command exceeded the environment timeout.") from error
        finally:
            self.command_marker = None

        if self.command_error:
            raise self.command_error
        if self.command_exit_code is None:
            raise EnvironmentError("The guest command did not return an exit code.")

        return CommandResult(command, self.command_exit_code, self.output[start:])

    async def write_code_file(self, file_name, code):
        """Write source code without adding it to the environment transcript.
        """
        path = f"{self.guest_workdir.rstrip('/')}/{file_name}"
        await self.execute(f"mkdir -p {shlex.quote(self.guest_workdir)} && : > {shlex.quote(path)}", record_command=False)

        encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
        for start in range(0, len(encoded), 2048):
            chunk = encoded[start:start + 2048]
            await self.execute(f"printf %s {chunk} | base64 -d >> {shlex.quote(path)}", record_command=False)
        return path

    async def destroy(self, timed_out=False):
        """Stop the VM and delete its temporary overlay.
        """
        async with self.destroy_lock:
            self.timed_out = self.timed_out or timed_out
            if self.process and self.process.returncode is None:
                self.process.terminate()
                with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError):
                    await asyncio.wait_for(self.process.wait(), timeout=5)
                if self.process.returncode is None:
                    self.process.kill()
                    with contextlib.suppress(ProcessLookupError):
                        await self.process.wait()

            if self.reader_task and not self.reader_task.done():
                self.reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.reader_task

            if self.qemu_output_task and not self.qemu_output_task.done():
                self.qemu_output_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.qemu_output_task

            await self.close_serial_transport()

            if self.overlay_path:
                for _ in range(3):
                    try:
                        self.overlay_path.unlink(missing_ok=True)
                        break
                    except PermissionError:
                        await asyncio.sleep(0.25)
