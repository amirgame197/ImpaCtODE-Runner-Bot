<img width="1280" height="640" alt="impactodebanner" src="https://github.com/user-attachments/assets/5f6e6da5-b68b-49eb-85f4-eb5d6a14b731" />

[Project Gallery](.gallery/README.md)

## Run code from Telegram or a local web interface inside a fresh, disposable Linux virtual machine.

**ImpaCtODE** uses AI to inspect the submitted code, plans the commands and dependency installation it needs, then runs everything in an isolated QEMU virtual machine.

Try the public demo: [**@ImpaCtODE_Bot**](https://t.me/ImpaCtODE_Bot).

> [!NOTE]
> The demo currently runs on my own system, which is not especially fast (mostly for compile-based languages). It may not be the smoothest experience, but it works and is a good way to see the project in action.

## What it does

ImpaCtODE provides a Telegram bot and a real-time web interface. The Telegram bot has two main commands:

- `/start` - shows a welcome message and quick reference.
- `/run` - runs code included in the message, or in a message you reply to.

When a run starts, the bot:

1. Detects and extracts the code and instructions around it.
2. Identifies the language and asks the AI to create the required guest commands.
3. Runs those commands in a new Linux VM with network and root access.
4. Streams the environment output back into the run-status message.

Each sandbox normally has **1 GB RAM**, **2 CPU cores**, and a maximum lifetime of **15 minutes**. Failed runs can receive an automatic environment-repair attempt before the bot shows possible code fixes. The VM is destroyed after the run, so it does not modify your workstation or the next run's environment.

ImpaCtODE is built with Python, an OpenAI-compatible SDK, [Telethon](https://codeberg.org/Lonami/Telethon), Flask-SocketIO with native threading, and bundled QEMU binaries for Windows and Linux.

## AI assistance

This project was built with GPT-5.6's help, especially around the web interface and environment-handling parts of the codebase. It helped design and implement the QEMU execution system, including launching and controlling the VM through Python's async subprocess handling with `asyncio.create_subprocess_exec`.

It also helped with the structured AI response handling used for code detection and execution planning, where responses follow a JSON schema so they are predictable and ready to parse. The project design, decisions, testing, and integration were done by me.

## Supported languages

The currently enabled, downloadable language environments are:

| Language | Runtime image |
| --- | --- |
| Python | Python overlay |
| JavaScript | Node.js overlay |
| TypeScript | Node.js / TypeScript overlay |
| C | C family overlay |
| C# | C family overlay |
| C++ | C family overlay |
| Go | Go overlay |
| Rust | Rust overlay |
| Java | Java overlay |

Additionally, See [Adding a language](#adding-a-language) below.

## Installation & setup

### Step 1: Download the project

Either clone the repository (recommended):

```bash
git clone https://github.com/amirgame197/ImpaCtODE-Runner-Bot.git
cd ImpaCtODE-Runner-Bot
```

or download and extract the repository ZIP from GitHub anywhere you like.

### Step 2: Create and activate a virtual environment

Using a virtual environment is strongly recommended but not required. Having a virtual environment keeps this bot's Python packages separate from your global Python installation and from your other projects, avoids version conflicts, and makes cleanup simple: delete the `venv` folder when you no longer need it.

**Windows (PowerShell):**

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

**Windows (Command Prompt):**

```bat
python -m venv venv
venv\Scripts\activate.bat
```

**Linux:**

```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 3: Install Python dependencies

With the environment activated (or not, if you choose to install the packages globally), install the required packages:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### Step 4: Configure your keys

The first start asks for missing values and saves them in a local `.env` file. You can also create that file yourself in the project root:

```env
IMPACTODE_TELEGRAM_BOT_TOKEN=your_bot_token
IMPACTODE_TELEGRAM_APP_ID=your_app_id
IMPACTODE_TELEGRAM_APP_HASH=your_app_hash
IMPACTODE_OPENAI_API_KEY=your_api_key
IMPACTODE_WEB_SECRET_KEY=a-long-random-secret-value
```

Get the Telegram bot token from [@BotFather](https://t.me/BotFather). Create a Telegram application at [my.telegram.org](https://my.telegram.org) (you can fill the application forms with any value) to get the app ID (a numerical value) and app hash (a long string). Keep all of these values private: they control access to your bot and Telegram application.

Use a unique random value for `IMPACTODE_WEB_SECRET_KEY`; Flask uses it to sign browser sessions.

By default, the AI requests use the [Mistral API](https://console.mistral.ai/) through its OpenAI-compatible endpoint. You can create a free Mistral account and get an API key there. If you use another OpenAI-compatible provider instead, put its key in `IMPACTODE_OPENAI_API_KEY` and change `openai_base_url` in [`config.py`](config.py) to that provider's base URL. You may also change the model names there if your provider uses different ones.

> [!IMPORTANT]
> As of right now, free mistral accounts support `codestral-latest` model so you can use their inference free at a monthly quota, but this can change any time.

### Step 5: Start Python

```bash
python Launcher.py
```

The launcher first checks for the base VM image and enabled language overlays. If an image is missing, it asks whether it should download and extract it automatically. Choose `a` to download all missing enabled language images. It then lets you start the Telegram bot, the web interface, or both. Open `http://localhost:1991` on the host machine; by default the web listener binds to all network interfaces.

That is all. Open the interface you selected: in Telegram, send `/start` and use `/run`. In the browser, create a run from the main page and follow its live output.

> [!Note]
> You can reverse proxy the interface using Nginx or Apache to enable HTTPS connections or accessing it through a domain

## QEMU and VM images

[QEMU](https://www.qemu.org/) is the virtual-machine emulator that keeps submitted code separate from the computer running the bot. ImpaCtODE uses QCOW2 overlays rather than copying a full disk for every run: a base Debian image sits at the bottom, a language image builds on it, and each execution receives one temporary overlay of its own.

The project includes bundled QEMU binaries for Windows. On Linux, it uses Debian's `qemu-system-x86` and `qemu-utils` packages. The bot checks for them at startup and automatically installs them with `apt` when it runs as root; otherwise, the manual command is in the [QEMU README](QEMU/README.md). Hardware virtualization should be available and enabled for the best performance: Windows uses WHPX and Linux uses KVM. Software emulation is possible, but noticeably slower.

The full image layout, manual QEMU commands, and overlay-creation instructions are in the [QEMU README](QEMU/README.md). It is worth reading before modifying VM images or adding runtimes.

### Downloading images manually

Automatic installation is the easy option, but manual installation works too:

1. Download the image archives from the repository's [Releases page](https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases).
2. Extract `base.qcow2` into `QEMU/`.
3. Extract each language `.qcow2` file into `QEMU/Overlays/`, preserving its expected filename - for example, `python-base.qcow2` or `javascript-base.qcow2`.
4. Start ImpaCtODE normally through `Launcher.py`.

Do not move `base.qcow2` after creating overlays. Their backing-file paths are relative (`../base.qcow2`), so an overlay will not boot if it can no longer find its base image in parent directory.

## Adding a language

Adding a language is basically easy:

1. Create a language overlay from `base.qcow2` using `qemu-img`. The exact Windows and Linux commands are in the [QEMU README](QEMU/README.md#creating-a-language-overlay).
2. Boot that overlay, install the language runtime and tools you want, then shut it down.
   
> [!NOTE]
> It is recommended to run the following commands to clean the final cache before shutting the overlay down:
> ```bash
> apt clean
> rm -rf /var/lib/apt/lists/*
> rm -rf /tmp/*
> rm -rf /var/tmp/*
> ```

3. Add or enable that language's entry in `languages_sequence` in [`config.py`](config.py). Set its `overlay_path`, source `file_name`, optional release `image_url`, and an execution-planning step.
4. Restart the bot and test it with a small program.

A language profile is mostly a config edit plus an overlay containing its runtime.

## Configuration notes

`config.py` also contains the run timeout, concurrency limit, memory/CPU behavior, QEMU paths, model settings, Telegram output limits, and web listener settings. The default concurrent-run setting affects how VM resources are allocated, so adjust it carefully on lower-powered systems.

## License

This project is released under the [MIT License](LICENSE).

QEMU is released under [GPL 2](QEMU/Binaries/Windows/COPYING).
