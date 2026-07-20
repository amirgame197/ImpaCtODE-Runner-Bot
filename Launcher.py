from pathlib import Path
import LanguageSupport
import subprocess
import time
import sys

# ? Set up selected language images before either interface can start a run
LanguageSupport.ensure_language_support_images()
ROOT = Path(__file__).resolve().parent

def choose_interface():
    """Ask which interface(s) should be launched.
    """
    while True:
        print("\n1. Launch Telegram Bot")
        print("2. Launch Web Interface")
        print("3. Both")
        choice = input("Choose an option: ").strip()

        if choice == "1":
            return True, False
        elif choice == "2":
            return False, True
        elif choice == "3":
            return True, True

        print("Please enter 1, 2, or 3.")


def stop_interfaces(processes):
    """Stop any interface processes that are still running.
    """
    for _, process in processes:
        if process.poll() is None:
            process.terminate()

    for _, process in processes:
        if process.poll() is None:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def launch_interfaces(telegram, web):
    """Launch and keep track of each selected interface process.
    """
    targets = []
    if telegram:
        targets.append(("Telegram bot", ROOT / "ImpaCtODEBot.py"))
    if web:
        targets.append(("Web interface", ROOT / "ImpaCtODEWeb.py"))

    processes = []
    for name, target in targets:
        process = subprocess.Popen([sys.executable, str(target)], cwd=ROOT)
        processes.append((name, process))
        print(f"Started {name} (PID {process.pid}).", flush=True)

    active_processes = set(range(len(processes)))
    try:
        while active_processes:
            for index in tuple(active_processes):
                name, process = processes[index]
                if process.poll() is not None:
                    print(f"{name} stopped with exit code {process.returncode}.", flush=True)
                    active_processes.remove(index)
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nStopping launched interfaces...", flush=True)
    finally:
        stop_interfaces(processes)


if __name__ == "__main__":
    telegram, web = choose_interface()
    launch_interfaces(telegram, web)
