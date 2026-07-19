import os
from pathlib import Path
from dotenv import load_dotenv, set_key

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"

# Load .env file if it exists
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)

# # #################### Basic Bot Configuration #######################################################

bot_username = "Automatic" # ? These will change on startup after a successful auth
bot_name = "Automatic"     # ? ^^^^^^^^^^^^^^^^^^^^
bot_id = "Automatic"       # ? ^^^^^^^^^^^^^^^^^^^^

def get_environment_variable(name):
    """Get an environment variable value, prompting user if not set and saving to .env.
    """
    variable = os.environ.get(name)
    if variable is not None:
        return variable
    
    # Prompt user for the value
    prompt = f"Enter value for {name}: "
    variable = input(prompt).strip()
    
    if not variable:
        raise ValueError(f"No value provided for '{name}'.")
    
    # Set in current process environment
    os.environ[name] = variable
    
    # Save to .env file for persistence across sessions
    set_key(str(ENV_FILE), name, variable)
    print(f"Saved {name} to {ENV_FILE}")
    
    return variable

token = get_environment_variable("IMPACTODE_TELEGRAM_BOT_TOKEN")
app_id = get_environment_variable("IMPACTODE_TELEGRAM_APP_ID")
app_hash = get_environment_variable("IMPACTODE_TELEGRAM_APP_HASH")

# # #################### Bot Responses Configuration ###################################################

usage_instructions = """
<aside><b>ImpaCtODE</b><cite>ᯤ</cite></aside>

##### Run your codes in a disposable Linux environment and follow its output live.
---

### Basic functionality
- ==/start==  Shows this reference.
- ==/run==  Runs the code included in the message, or code in the message you reply to.

- Use the ==Abort== button on an active run to stop and destroy its environment.
- In future, artifact generation, multi-code support and more speed optimizations / compatibility features will be added.

---
<details open><summary>How it works</summary>

1. The bot detects and extracts the submitted code and any instructions around it.
2. It identifies the language, prepares the code, and plans the required guest commands.
3. The code runs in a fresh Linux virtual machine. The VM has network and root access.<br>
It comes with 1 GB RAM, 2 CPU cores, and a maximum lifetime of 15 minutes.
4. Environment output is shown in the run status message.<br>
Failed runs may trigger an automatic environment repair attempt before possible code fixes are displayed.

</details>

---

| **Supported** | **Languages** |
|-|-|
| **C/C++** | **C#** |
| **Rust** | **Go** |
| **Python** | **Java** |
| **JavaScript** | **TypeScript** |


---
<details><summary>Best practices</summary>

- Put any setup requirements directly beside the code: required special packages, command-line tools, environment variables, expected files, or run instructions.
- The planner can infer many dependencies from the code, but explicit instructions reduce ambiguity on complex setups.
- Keep credentials and production-only secrets out of submitted code.<br>
==The guest VM is temporary and is deleted after the run.==
- Use self-contained examples when possible so the result is easy to reproduce.

</details>

"""

# # #################### OpenAI SDK Configuration ######################################################

openai_base_url = "https://api.mistral.ai/v1"
openai_api_key = get_environment_variable("IMPACTODE_OPENAI_API_KEY")

output_refresh_interval = 3 
# ? Seconds between Telegram message edits when showing the environment transcript

# # #################### QEMU Configuration ######################################################

qemu_executable = {
    "Windows": {
        "qemu-img": ROOT / "QEMU" / "Binaries" / "Windows" / "qemu-img.exe",
        "qemu-system-x86_64": ROOT / "QEMU" / "Binaries" / "Windows" / "qemu-system-x86_64.exe"
    },

    "Linux": {
        "qemu-img": ROOT / "QEMU" / "Binaries" / "Linux" / "bin" / "qemu-img",
        "qemu-system-x86_64": ROOT / "QEMU" / "Binaries" / "Linux" / "bin" / "qemu-system-x86_64",

        "lib": ROOT / "QEMU" / "Binaries" / "Linux" / "lib",
        # ? Path to the QEMU shared libraries, which must be added to LD_LIBRARY_PATH on Linux
        
        "data": ROOT / "QEMU" / "Binaries" / "Linux" / "share",
        # ? QEMU data and SeaBIOS firmware paths required by the bundled Linux binary
    },
}

sandbox_ready_marker = "SANDBOX_READY"
# ? The commands start running once this string gets in environment's stdout

qemu_guest_workdir = "/root" # "/tmp/impactode"
# ? Every command and extracted code file is run from this directory inside the disposable VM

qemu_ready_settle_seconds = 3
# ? Wait for automatic serial login to finish after sandbox_ready_marker before sending commands

telegram_output_limit = 3584
# ? The latest characters of the environment transcript shown in the continuously edited Telegram message

captured_environment_output_limit = 4096
# ? Maximum latest environment transcript retained on the bot host to protect host memory

sequence_max_tokens = 2048
# ? Default maximum response size for structured AI sequence steps

# # #################### Sequence Structure Variables ##################################################

max_attempts = 3
# ? How many times the AI should try running the code after failure until it just aborts the process

concurrent_runs = 3
# ? How many runs can happen at the same time until the rest gets inside a running queue

env_timeout = 15 * 60 
# ? Seconds until the environment gets destroyed

overlays_dir = ROOT / "QEMU" / "Overlays"
# ? Path that language-specific overlays are stored

sequence_overlays_dir = ROOT / "QEMU" / "Overlays" / "SequenceDisposal"
# ? Path that disposal overlays (created from language specific overlays) are temporarily stored

environment_details_command = (
    "printf '%s\\n' '--- uname -a ---' && uname -a && "
    "printf '%s\\n' '--- cwd ---' && pwd && "
    "printf '%s\\n' '--- tree ---' && "
    "(tree -a 2>/dev/null || find . -maxdepth 3 -print)"
)
# ? Command to collect the guest environment details

base_image_url = "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/latest/download/base.qcow2.tar.xz"
# ? URL to the base Debian image used for all language overlays (LanguageSupport.py)

languages_sequence = {
    "Python": [
        { 
            "overlay_path": overlays_dir / "python-base.qcow2", "file_name": "code.py",
            "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/latest/download/python-base.qcow2.tar.xz"
        },
        {
            "name": "Python execution plan",
            "description": "Creating ordered Linux shell commands to run the Python code.",
            "model_name": "codestral-latest",

            "system_prompt": (
                "You are a Debian code execution planner. The supplied source code has already been written to "
                "the supplied file_name in the supplied current working directory inside a disposable VM. "

                "Return an ordered array of non-interactive /bin/bash commands that should run one after another. "
                "Inspect imports before planning dependencies. Python standard-library modules are already installed and must never be installed with pip; "
                "this includes base64, os, json, platform, pathlib, sys, re, asyncio, and all other standard-library modules. "

                "Code that imports only standard-library modules needs no dependency-install command. Do not add speculative pip installs. "
                "Install only a confirmed third-party dependency when the source actually imports it, using python3 -m pip install --break-system-packages. "
                "Current environment already has a full Python standard-library installation, with additional tools such as git and curl. "

                "If retry_feedback is non-empty, it identifies a command from an earlier failed plan. Treat it as mandatory "
                "correction context: do not emit that command or an equivalent invalid command. "

                "Each command starts in the stated working directory, so use explicit paths or commands such as python3 -m pip instead of relying on shell state. "
                "The final command must execute the supplied code in the foreground and wait for it to finish."
            ),

            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "PythonExecutionPlanResponse",
                    "schema": {
                        "properties": {
                            "commands": {
                                "description": "Ordered shell commands. The final command runs the submitted code in the foreground.",
                                "items": { "type": "string" },
                                "minItems": 1,
                                "type": "array"
                            }
                        },
                        "required": ["commands"],
                        "title": "PythonExecutionPlanResponse",
                        "type": "object"
                    }
                }
            }
        },

    ],
    "JavaScript": [
        { 
            "overlay_path": overlays_dir / "javascript-base.qcow2", "file_name": "code.js",
            "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/latest/download/javascript-base.qcow2.tar.xz"
        },
        {
            "name": "JavaScript execution plan",
            "description": "Creating ordered Linux shell commands to run the JS code.",
            "model_name": "codestral-latest",

            "system_prompt": (
                "You are a Debian Node.js code execution planner. The supplied source code has already been written to "
                "the supplied file_name in the supplied current working directory inside a disposable VM. "
                "Return an ordered array of non-interactive /bin/bash commands that should run one after another. "

                "Inspect imports before planning dependencies. Node.js built-in modules are already installed and must never "
                "be installed with npm; this includes fs, path, os, util, events, stream, http, https, url, crypto, child_process, "
                "buffer, assert, axios, lodash, dotenv, uuid and all other Node.js built-in modules. "

                "Code that imports only built-in Node.js modules needs no dependency-install command. "
                "Do not add speculative npm installs. Install only confirmed third-party dependencies when the source actually "
                "imports them, using npm install, if they're not in built-in modules. "

                "Current environment already has Node.js, npm, and common execution tools installed. "
                "Global packages may already be available through the configured Node.js environment. "

                "If retry_feedback is non-empty, it identifies a command from an earlier failed plan. Treat it as mandatory "
                "correction context: do not emit that command or an equivalent invalid command. "

                "Each command starts in the stated working directory, so use explicit paths or commands and do not rely on "
                "previous shell state. Use node to execute JavaScript files and tsx to execute TypeScript files when needed. "
                "The final command must execute the supplied code in the foreground and wait for it to finish."
            ),

            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "JavaScriptExecutionPlanResponse",
                    "schema": {
                        "properties": {
                            "commands": {
                                "description": "Ordered shell commands. The final command runs the submitted code in the foreground.",
                                "items": { "type": "string" },
                                "minItems": 1,
                                "type": "array"
                            }
                        },
                        "required": ["commands"],
                        "title": "JavaScriptExecutionPlanResponse",
                        "type": "object"
                    }
                }
            }
        },

    ],
    "TypeScript": [
        { 
            "overlay_path": overlays_dir / "javascript-base.qcow2", "file_name": "code.ts",
            "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/latest/download/javascript-base.qcow2.tar.xz"
        },
        {
            "name": "TypeScript execution plan",
            "description": "Creating ordered Linux shell commands to run the TS code.",
            "model_name": "codestral-latest",

            "system_prompt": (
                "You are a Debian TypeScript code execution planner. The supplied source code has already been written to "
                "the supplied file_name in the supplied current working directory inside a disposable VM. "
                "Return an ordered array of non-interactive /bin/bash commands that should run one after another. "

                "Inspect imports before planning dependencies. TypeScript and Node.js built-in modules are already installed and must never "
                "be installed with npm; this includes fs, path, os, util, events, stream, http, https, url, crypto, child_process, "
                "buffer, assert, axios, lodash, dotenv, uuid and all other Node.js built-in modules. "

                "Code that imports only built-in Node.js modules needs no dependency-install command. "
                "Do not add speculative npm installs. Install only confirmed third-party dependencies when the source actually "
                "imports them, using npm install, if they're not in built-in modules. "

                "Current environment already has Node.js, npm, TypeScript tooling, and tsx installed. "
                "Use tsx to execute TypeScript files directly; do not compile with tsc unless the source specifically requires compilation. "
                "Global packages are already available through the configured Node.js environment. "

                "If retry_feedback is non-empty, it identifies a command from an earlier failed plan. Treat it as mandatory "
                "correction context: do not emit that command or an equivalent invalid command. "

                "Each command starts in the stated working directory, so use explicit paths or commands and do not rely on "
                "previous shell state. Use tsx to execute supplied TypeScript files. "
                "The final command must execute the supplied code in the foreground and wait for it to finish."
            ),

            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "TypeScriptExecutionPlanResponse",
                    "schema": {
                        "properties": {
                            "commands": {
                                "description": "Ordered shell commands. The final command runs the submitted code in the foreground.",
                                "items": { "type": "string" },
                                "minItems": 1,
                                "type": "array"
                            }
                        },
                        "required": ["commands"],
                        "title": "TypeScriptExecutionPlanResponse",
                        "type": "object"
                    }
                }
            }
        },

    ],
    "C#": [
        { 
            "overlay_path": overlays_dir / "cfam-base.qcow2", "file_name": "code.cs",
            "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/latest/download/cfam-base.qcow2.tar.xz"
        },
        {
            "name": "C# execution plan",
            "description": "Creating ordered Linux shell commands to run the C# code.",
            "model_name": "codestral-latest",

            "system_prompt": (
                "You are a Debian C# code execution planner. The supplied source code has already been written to "
                "the supplied file_name in the supplied current working directory inside a disposable VM. "
                "Return an ordered array of non-interactive /bin/bash commands that should run one after another. "

                "Inspect using directives and #r references before planning dependencies. Standard .NET libraries are already installed "
                "and must never be installed separately; this includes System, System.IO, System.Collections, System.Collections.Generic, "
                "System.Linq, System.Net, System.Net.Http, System.Threading, System.Threading.Tasks, and all other standard .NET namespaces. "

                "Code that imports only standard .NET namespaces needs no dependency-install command. "
                "Do not add speculative NuGet package installs. Install only confirmed external NuGet dependencies when the source "
                "actually requires them. "

                "Current environment already has the .NET SDK and dotnet-script installed. "
                "Use 'dotnet-script code.cs' to execute supplied C# files directly. Do not create projects, csproj files, or use dotnet new "
                "unless the source specifically requires a project structure. "

                "Global .NET tools are already available through the configured environment. "
                "Do not reinstall dotnet, the SDK, or dotnet-script. "

                "If retry_feedback is non-empty, it identifies a command from an earlier failed plan. Treat it as mandatory "
                "correction context: do not emit that command or an equivalent invalid command. "

                "Each command starts in the stated working directory, so use explicit paths or commands and do not rely on "
                "previous shell state. Use 'dotnet-script --version' syntax when checking the installed script runner. "
                "The dotnet-script is already installed in the environment. "
                "The final command must execute the supplied C# code in the foreground and wait for it to finish."
            ),

            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "CSharpExecutionPlanResponse",
                    "schema": {
                        "properties": {
                            "commands": {
                                "description": "Ordered shell commands. The final command runs the submitted code in the foreground.",
                                "items": { "type": "string" },
                                "minItems": 1,
                                "type": "array"
                            }
                        },
                        "required": ["commands"],
                        "title": "CSharpExecutionPlanResponse",
                        "type": "object"
                    }
                }
            }
        },

    ],
    "C++": [
        { 
            "overlay_path": overlays_dir / "cfam-base.qcow2", "file_name": "code.cpp",
            "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/latest/download/cfam-base.qcow2.tar.xz"
        },
        {
            "name": "C++ execution plan",
            "description": "Creating ordered Linux shell commands to run the C++ code.",
            "model_name": "codestral-latest",

            "system_prompt": (
                "You are a Debian C++ code execution planner. The supplied source code has already been written to "
                "the supplied file_name in the supplied current working directory inside a disposable VM. "
                "Return an ordered array of non-interactive /bin/bash commands that should run one after another. "

                "Inspect #include directives before planning dependencies. Standard C++ headers are already installed and must never "
                "be installed separately; this includes iostream, vector, string, map, unordered_map, algorithm, filesystem, "
                "memory, thread, mutex, chrono, random, regex, and all other standard C++ library headers. "

                "Code that imports only standard C++ headers needs no dependency-install command. "
                "Do not add speculative apt installs. Install only confirmed third-party development dependencies when the source "
                "actually includes them and the required Debian package is clear, using apt install -y. "

                "Current environment already has GCC, G++, standard build tools, pkg-config, cmake, gdb, libcurl development headers, "
                "OpenSSL development headers, zlib development headers, and SQLite development headers installed. "
                "Do not reinstall existing tools or libraries. "

                "Compile C++ source files using g++. Prefer modern standards such as -std=c++17 or -std=c++20 when appropriate. "
                "Use required linker flags when the source imports external libraries. "

                "The final command must compile and execute the supplied C++ code in the foreground and wait for it to finish. "

                "If retry_feedback is non-empty, it identifies a command from an earlier failed plan. Treat it as mandatory "
                "correction context: do not emit that command or an equivalent invalid command. "

                "Each command starts in the stated working directory, so use explicit paths or commands and do not rely on "
                "previous shell state."
            ),

            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "CppExecutionPlanResponse",
                    "schema": {
                        "properties": {
                            "commands": {
                                "description": "Ordered shell commands. The final command runs the submitted code in the foreground.",
                                "items": { "type": "string" },
                                "minItems": 1,
                                "type": "array"
                            }
                        },
                        "required": ["commands"],
                        "title": "CppExecutionPlanResponse",
                        "type": "object"
                    }
                }
            }
        },

    ],
    "C": [
        { 
            "overlay_path": overlays_dir / "cfam-base.qcow2", "file_name": "code.c",
            "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/latest/download/cfam-base.qcow2.tar.xz"
        },
        {
            "name": "C execution plan",
            "description": "Creating ordered Linux shell commands to run the C code.",
            "model_name": "codestral-latest",

            "system_prompt": (
                "You are a Debian C code execution planner. The supplied source code has already been written to "
                "the supplied file_name in the supplied current working directory inside a disposable VM. "
                "Return an ordered array of non-interactive /bin/bash commands that should run one after another. "

                "Inspect #include directives before planning dependencies. Standard C headers are already installed and must never "
                "be installed separately; this includes stdio.h, stdlib.h, string.h, stdint.h, stdbool.h, math.h, time.h, "
                "unistd.h, errno.h, signal.h, pthread.h, and all other standard C library headers. "

                "Code that imports only standard C headers needs no dependency-install command. "
                "Do not add speculative apt installs. Install only confirmed third-party development dependencies when the source "
                "actually includes them and the required Debian package is clear, using apt install -y. "

                "Current environment already has GCC, standard build tools, pkg-config, cmake, gdb, libcurl development headers, "
                "OpenSSL development headers, zlib development headers, and SQLite development headers installed. "
                "Do not reinstall existing tools or libraries. "

                "Compile C source files using gcc. Use appropriate compiler flags when required by the source. "
                "The final command must compile and execute the supplied C code in the foreground and wait for it to finish. "

                "If retry_feedback is non-empty, it identifies a command from an earlier failed plan. Treat it as mandatory "
                "correction context: do not emit that command or an equivalent invalid command. "

                "Each command starts in the stated working directory, so use explicit paths or commands and do not rely on "
                "previous shell state."
            ),

            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "CExecutionPlanResponse",
                    "schema": {
                        "properties": {
                            "commands": {
                                "description": "Ordered shell commands. The final command runs the submitted code in the foreground.",
                                "items": { "type": "string" },
                                "minItems": 1,
                                "type": "array"
                            }
                        },
                        "required": ["commands"],
                        "title": "CExecutionPlanResponse",
                        "type": "object"
                    }
                }
            }
        },

    ],
    "Go": [
        { 
            "overlay_path": overlays_dir / "go-base.qcow2", "file_name": "code.go",
            "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/latest/download/go-base.qcow2.tar.xz"
        },
        {
            "name": "Go execution plan",
            "description": "Creating ordered Linux shell commands to run the Go code.",
            "model_name": "codestral-latest",

            "system_prompt": (
                "You are a Debian Go code execution planner. The supplied source code has already been written to "
                "the supplied file_name in the supplied current working directory inside a disposable VM. "
                "Return an ordered array of non-interactive /bin/bash commands that should run one after another. "

                "Inspect import statements before planning dependencies. Go's standard library is already installed and must never "
                "be installed separately; this includes fmt, os, io, net/http, encoding/json, strings, bytes, path/filepath, "
                "crypto/*, database/sql, sync, time, context, math, strconv, regexp, log, testing, and all other Go standard-library packages. "

                "Do not attempt to manually install Go packages using 'go get'. "
                "The current working directory is already initialized as a Go module with a valid go.mod file. "
                "When third-party packages are imported, use 'go mod tidy' to resolve and download only the required dependencies. "
                "If the source imports only standard-library packages, do not emit a 'go mod tidy' command. "

                "Current environment already has the Go toolchain installed and ready to use. "
                "Do not recreate or modify the module using 'go mod init'. "

                "If retry_feedback is non-empty, it identifies a command from an earlier failed plan. Treat it as mandatory "
                "correction context: do not emit that command or an equivalent invalid command. "

                "Each command starts in the stated working directory, so use explicit commands and do not rely on previous shell state. "
                "Execute Go programs using 'go run'. "

                "The final command must execute the supplied Go code in the foreground and wait for it to finish."
            ),

            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "GoExecutionPlanResponse",
                    "schema": {
                        "properties": {
                            "commands": {
                                "description": "Ordered shell commands. The final command runs the submitted code in the foreground.",
                                "items": { "type": "string" },
                                "minItems": 1,
                                "type": "array"
                            }
                        },
                        "required": ["commands"],
                        "title": "GoExecutionPlanResponse",
                        "type": "object"
                    }
                }
            }
        },

    ],
    "Rust": [
        { 
            "overlay_path": overlays_dir / "rust-base.qcow2", "file_name": "src/main.rs",
            "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/latest/download/rust-base.qcow2.tar.xz"
        },
        {
            "name": "Rust execution plan",
            "description": "Creating ordered Linux shell commands to run the Rust code.",
            "model_name": "codestral-latest",

            "system_prompt": (
                "You are a Debian Rust code execution planner. The supplied source code has already been written to "
                "the supplied file_name in the supplied current working directory inside a disposable VM. "
                "Return an ordered array of non-interactive /bin/bash commands that should run one after another. "

                "Inspect use statements before planning dependencies. Rust standard library modules are already installed and must never "
                "be installed separately; this includes std, core, alloc, collections, fs, io, net, thread, sync, time, process, "
                "env, path, fmt, and all other Rust standard-library modules. "

                "Code that imports only Rust standard-library modules needs no dependency-install command. "
                "Do not add speculative crate installations. Add only confirmed external Rust crates when the source code actually "
                "uses them. Use 'cargo add <crate-name>' to install external dependencies, and include required features when the "
                "source code clearly requires them. "

                "Current environment already has rustc, cargo, and cargo-edit installed. "
                "The current working directory is already configured as a Cargo project with Cargo.toml and src/main.rs. "
                "Do not run cargo init, create new projects, or manually edit Cargo.toml. "

                "Use 'cargo add' for dependencies and 'cargo run' to compile and execute the supplied Rust code. "
                "Cargo will automatically resolve, download, and compile dependencies after they are added. "

                "If retry_feedback is non-empty, it identifies a command from an earlier failed plan. Treat it as mandatory "
                "correction context: do not emit that command or an equivalent invalid command. "

                "Each command starts in the stated working directory, so use explicit commands and do not rely on previous shell state. "
                "Use cargo run as the final execution command. "

                "The final command must execute the supplied Rust code in the foreground and wait for it to finish."
            ),

            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "RustExecutionPlanResponse",
                    "schema": {
                        "properties": {
                            "commands": {
                                "description": "Ordered shell commands. The final command runs the submitted code in the foreground.",
                                "items": { "type": "string" },
                                "minItems": 1,
                                "type": "array"
                            }
                        },
                        "required": ["commands"],
                        "title": "RustExecutionPlanResponse",
                        "type": "object"
                    }
                }
            }
        },

    ],
    "Java": [
        { 
            "overlay_path": overlays_dir / "java-base.qcow2", "file_name": "src/main/java/Main.java",
            "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/latest/download/java-base.qcow2.tar.xz"
        },
        {
            "name": "Java execution plan",
            "description": "Creating ordered Linux shell commands to run the Java code.",
            "model_name": "codestral-latest",

            "system_prompt": (
                "You are a Debian Java code execution planner. The supplied source code has already been written to "
                "the supplied file_name in the supplied current working directory inside a disposable VM. "
                "Return an ordered array of non-interactive /bin/bash commands that should run one after another. "

                "Inspect import statements before planning dependencies. Java standard library packages are already installed and must never "
                "be installed separately; this includes java.lang, java.util, java.io, java.nio, java.net, java.time, java.math, "
                "java.sql, java.security, java.crypto, javax.* standard modules, and all other classes included with the Java Development Kit. "

                "Code that imports only Java standard library classes needs no external dependency entries. "
                "Do not add speculative Maven dependencies. Add only confirmed third-party dependencies required by imports in the source code. "

                "The current environment already has OpenJDK 21 and Maven installed. "
                "The current working directory is already a Maven project with the standard layout: "
                "./src/main/java/Main.java and ./pom.xml. "
                "Do not run mvn archetype commands, do not create a new project, and do not move or modify the supplied Java source file. "

                "When external libraries are required, create or replace pom.xml with a valid Maven project configuration. "
                "The pom.xml must target Java 21 using maven.compiler.source and maven.compiler.target set to 21. "
                "Use Maven Central dependency coordinates with explicit versions. "
                "Include required build configuration for executing the Main class using exec-maven-plugin. "
                "Do not add unnecessary plugins or dependencies. "
                "Write pom.xml using cat with a heredoc, e.g.: cat > pom.xml << 'EOF' ... EOF"

                "Use Maven to resolve dependencies and compile the program. "
                "Use 'mvn compile exec:java' as the execution command. "
                "Maven will automatically download required dependencies from Maven Central. "

                "If retry_feedback is non-empty, it identifies a command from an earlier failed plan. Treat it as mandatory "
                "correction context: do not emit that command or an equivalent invalid command. "

                "Each command starts in the stated working directory, so use explicit commands and do not rely on previous shell state. "
                "Keep generated files inside the current project directory. "

                "The final command must execute the supplied Java code in the foreground and wait for it to finish."
            ),

            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "JavaExecutionPlanResponse",
                    "schema": {
                        "properties": {
                            "commands": {
                                "description": "Ordered shell commands. The final command runs the submitted code in the foreground.",
                                "items": { "type": "string" },
                                "minItems": 1,
                                "type": "array"
                            }
                        },
                        "required": ["commands"],
                        "title": "JavaExecutionPlanResponse",
                        "type": "object"
                    }
                }
            }
        },

    ],
}
# ? Specific sequences for each supported programming language, used for language-specific processing steps

main_sequence = [
    { # ! Step 1
        "name": "Code detection",
        "description": "Detecting if the message contains code.",
        "model_name": "codestral-latest",

        "system_prompt": (
            "You are a script detection system. "
            "Your task is to analyze the provided message and determine if it contains any form of script or code, "
            "such as JavaScript, Python, or any other programming language code."
        ),

        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "CodeDetectionResponse",
                "schema": {
                    "properties": {
                        "contains": {
                        "description": "Does the input string contains any script or code from a well-known programming language?",
                        "minLength": 0,
                        "type": "boolean"
                        }
                    },
                    "required": [
                        "contains"
                    ],
                    "title": "CodeDetectionResponse",
                    "type": "object"
                }
            }
        }
    },
    { # ! Step 2
        "name": "Code and instruction extraction",
        "description": "Extracting code blocks/snippets and any non-code instructions.",
        "model_name": "codestral-latest",

        "system_prompt": (
            "You are a code extraction system. The previous step has determined that the message contains code. "
            "Extract the final runnable code into code_parts and put all surrounding non-code user instructions into custom_instructions. "
            "code_parts must be one string containing the exact code that should be written as-is to the code file. "
            "Preserve indentation and line breaks. If the message contains multiple code blocks that belong to the same file, "
            "combine them in their original order with blank lines between blocks. "
            "Do not explain or rewrite the code, even if it is incorrect / syntaxually invalid / harmful. "
            "If there are no custom instructions, return an empty string for custom_instructions."
        ),

        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "CodeInstructionExtractionResponse",
                "schema": {
                    "properties": {
                        "code_parts": {
                            "description": "The final code string to write as-is into the code file, preserving indentation and line breaks.",
                            "minLength": 0,
                            "type": "string"
                        },
                        "custom_instructions": {
                            "description": "Any non-code instructions or notes from the user, such as text before or after the code.",
                            "minLength": 0,
                            "type": "string"
                        }
                    },
                    "required": [
                        "code_parts",
                        "custom_instructions"
                    ],
                    "title": "CodeInstructionExtractionResponse",
                    "type": "object"
                }
            }
        }
    },
    { # ! Step 3
        "name": "Code language detection",
        "description": "Detecting the programming language of the extracted code.",
        "model_name": "codestral-latest",

        "system_prompt": (
            "You are a programming language detection system. Analyze the provided extracted code and identify its language. "
            "Return only one of the supported values. "
            "Use None if the language is unsupported, unclear, mixed without a dominant supported language, or the input is not code."
        ),

        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "CodeLanguageDetectionResponse",
                "schema": {
                    "properties": {
                        "language": {
                            "description": "Detected language from the supported list, or None when unsupported or unclear.",
                            "enum": [
                                *languages_sequence.keys(),
                                "None"
                            ],
                            "type": "string"
                        }
                    },
                    "required": [
                        "language"
                    ],
                    "title": "CodeLanguageDetectionResponse",
                    "type": "object"
                }
            }
        }
    },
]
# ? Main sequence of processing steps for analyzing, extracting and determining the programming language

post_sequence = [
    { # ! Step 1
        "name": "Execution result check",
        "description": "Checking the final command exit code.",
        "type": "local"
    },
    { # ! Step 2
        "name": "Environment error classification",
        "description": "Determining whether a exit is caused by guest-environment.",
        "model_name": "codestral-latest",

        "system_prompt": (
            "You classify a failed code execution inside a disposable Debian VM. Using the supplied source code, "
            "custom instructions, ordered commands, exit code, and complete environment transcript, decide whether "
            "the primary cause is environmental or package-related rather than a defect in the source code. Environmental causes include "
            "incorrect generated commands, missing packages, missing tools, incompatible operating-system capabilities or unavailable runtime support. "
            "Classify an invalid pip install command as environment-related when it attempts to install a Python standard-library module or a nonexistent package. "
            "Do not propose commands or rewrite code."
        ),

        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "EnvironmentErrorClassificationResponse",
                "schema": {
                    "properties": {
                        "environment_related": {
                            "description": "True when the failure can likely be fixed by changing the disposable guest environment / command steps instead of changing source code.",
                            "type": "boolean"
                        },
                        "reason": {
                            "description": "A concise explanation of the classification.",
                            "type": "string"
                        }
                    },
                    "required": ["environment_related", "reason"],
                    "title": "EnvironmentErrorClassificationResponse",
                    "type": "object"
                }
            }
        }
    },
    { # ! Step 3
        "name": "Environment repair commands",
        "description": "Producing guest-only commands that may repair an environment-related failure.",
        "model_name": "codestral-latest",

        "system_prompt": (
            "You repair environment-related failures inside a disposable Debian VM. Return an ordered array of "
            "non-interactive /bin/bash commands that may fix the supplied failure in the current VM. Commands run "
            "automatically and sequentially from the guest working directory. Do not use sudo, ask questions, open "
            "an interactive shell, start background processes, or modify the submitted source code. Return an empty "
            "array when no guest mutation is needed. Never install a Python standard-library module or a package that "
            "the transcript shows does not exist. Set retry_execution true, with an empty commands array, when the failure "
            "was caused by an incorrect earlier execution-plan command and a newly generated plan should omit or replace it; "
            "for example, a failed attempt to pip-install base64. In retry_feedback, state the precise correction the next "
            "execution plan must apply. Otherwise set retry_execution false and return an empty retry_feedback string."
        ),

        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "EnvironmentRepairResponse",
                "schema": {
                    "properties": {
                        "commands": {
                            "description": "Ordered guest-only repair commands, or an empty array when no repair is available.",
                            "items": { "type": "string" },
                            "type": "array"
                        },
                        "retry_execution": {
                            "description": "True when the failed execution plan should be regenerated and retried without requiring guest repair commands.",
                            "type": "boolean"
                        },
                        "retry_feedback": {
                            "description": "A concise mandatory correction for the next execution plan, or an empty string when no retry is needed.",
                            "type": "string"
                        }
                    },
                    "required": ["commands", "retry_execution", "retry_feedback"],
                    "title": "EnvironmentRepairResponse",
                    "type": "object"
                }
            }
        }
    },
    { # ! Step 4
        "name": "Failure guidance",
        "description": "Providing user-facing possible fixes for the source code.",
        "model_name": "codestral-latest",

        "system_prompt": (
            "You explain a failed code execution to the user. Based on the supplied source code, instructions, "
            "commands, exit code, environment transcript, and failure classification, provide concise Markdown "
            "guidance describing likely fixes. Do not claim to have changed the source code or environment, and do "
            "not include a long restatement of the transcript."
        ),

        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "FailureGuidanceResponse",
                "schema": {
                    "properties": {
                        "guidance": {
                            "description": "Concise user-facing Markdown with possible fixes or environment limitations.",
                            "type": "string"
                        }
                    },
                    "required": ["guidance"],
                    "title": "FailureGuidanceResponse",
                    "type": "object"
                }
            }
        }
    }
]
# ? Post sequence of processing and analyzing the resulting code's output to see if there are any problems
