import os
from pathlib import Path
ROOT = Path(__file__).resolve().parent

# # #################### Basic Bot Configuration #######################################################

bot_username = "Automatic" # ? These will change on startup after a successful auth
bot_name = "Automatic"     # ? ^^^^^^^^^^^^^^^^^^^^
bot_id = "Automatic"       # ? ^^^^^^^^^^^^^^^^^^^^

def get_environment_variable(name):
    """Get an environment variable value, or return a default if not set.
    """
    variable = os.environ.get(name, None)
    if variable is None:
        raise ValueError(f"Environment variable '{name}' is not set.")
    
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
| **C#** | **C++** |
| **Rust** | **Go** |
| **Python** | **Java** |
| **Java Script** | **Type Script** |


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

qemu_serial_chunk_size = 1
qemu_serial_chunk_delay = 0.01
# ? Pace serial-console input so long base64 transfers are not dropped by the guest terminal

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

base_image_url = "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/download/latest//base.qcow2.tar.xz"
# ? URL to the base Debian image used for all language overlays (LanguageSupport.py)

languages_sequence = {
    "Python": [
        { 
            "overlay_path": overlays_dir / "python-base.qcow2", "file_name": "code.py",
            "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/download/latest/python-base.qcow2.tar.xz"
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
        # { 
        #     "overlay_path": overlays_dir / "javascript-base.qcow2", "file_name": "code.js",
        #     "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/download/latest/python-base.qcow2.tar.xz"
        # },

    ],
    "TypeScript": [
        # { 
        #     "overlay_path": overlays_dir / "javascript-base.qcow2", "file_name": "code.ts",
        #     "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/download/latest/javascript-base.qcow2.tar.xz"
        # },

    ],
    "C#": [
        # { 
        #     "overlay_path": overlays_dir / "csharp-base.qcow2", "file_name": "code.cs",
        #     "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/download/latest/csharp-base.qcow2.tar.xz"
        # },

    ],
    "C++": [
        # { 
        #     "overlay_path": overlays_dir / "cpp-base.qcow2", "file_name": "code.cpp",
        #     "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/download/latest/cpp-base.qcow2.tar.xz"
        # },

    ],
    "Go": [
        # { 
        #     "overlay_path": overlays_dir / "go-base.qcow2", "file_name": "code.go",
        #     "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/download/latest/go-base.qcow2.tar.xz"
        # },

    ],
    "Rust": [
        # { 
        #     "overlay_path": overlays_dir / "rust-base.qcow2", "file_name": "code.rs",
        #     "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/download/latest/rust-base.qcow2.tar.xz"
        # },

    ],
    "Java": [
        # { 
        #     "overlay_path": overlays_dir / "java-base.qcow2", "file_name": "code.java",
        #     "image_url": "https://github.com/amirgame197/ImpaCtODE-Runner-Bot/releases/download/latest/java-base.qcow2.tar.xz"
        # },

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
