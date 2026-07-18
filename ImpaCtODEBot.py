from Environment import EnvironmentError, QemuEnvironment
from telethon.tl.types import *
from telethon import *

from OpenAI import TextGen
import LanguageSupport
import unicodedata
import contextlib
import asyncio
import random
import config
import html
import json
import sys
import re

LanguageSupport.ensure_language_support_images()

client = TelegramClient('impactode', config.app_id, config.app_hash, connection_retries=None, retry_delay=15).start(bot_token=config.token) # token
_run_queue = asyncio.Semaphore(config.concurrent_runs)
_active_sequences = {}

# # #################### Input Handling ################################################################

async def process_message(event):
    """Process incoming messages and handle commands.
    """
    message = get_message(event.message)
    message_lower = message.lower()

    if check_command(message_lower, "start"):
        await send_rich_message(event, config.usage_instructions, reply_to_msg_id=event.id)
        return
    
    if check_command(message_lower, "run", True):
        # ? Message is a reply, try running the reply_msg
        if event.message.is_reply:
            reply_msg = await event.message.get_reply_message()
            reply_text = get_message(reply_msg)

            if len(reply_text) > 5:
                asyncio.create_task(running_sequence(reply_msg, reply_text, event.sender_id))
                return
        
        # ? Message is not a reply, try running this message
        elif len(message_lower) > 5:
            asyncio.create_task(running_sequence(event, message, event.sender_id))
            return
        
        await event.reply("**Please reply to / send a message containing code(s).**\nUsage: `/run [code]`")
        return


def check_command(text, command, startswith=False):
    """Check if the text matches the command, optionally allowing for a prefix.
    """
    if startswith:
        return text.startswith(f'/{command}') # ? or text.startswith(f'/{command}@{config.bot_username}')
    else:
        return text == f'/{command}' or text == f'/{command}@{config.bot_username}'


def get_message(message):
    """Extract the text content from a message, with handling various message formats.
    """
    try:
        # Prefer the normal message text if it exists
        text = getattr(message, "message", None)
        if isinstance(text, str) and text.strip():
            return text

        # Prefer .text if it exists
        text = getattr(message, "text", None)
        if isinstance(text, str) and text.strip():
            return text
        
        
        # Try rich_message / rich_message.blocks
        rich = getattr(message, "rich_message", None)
        if rich is None:
            return ""
        
        seen = set()

        def flatten(obj, block_sep="\n"):
            if obj is None:
                return ""

            oid = id(obj)
            if oid in seen:
                return ""
            seen.add(oid)

            if isinstance(obj, str):
                return obj

            if isinstance(obj, (list, tuple, set)):
                parts = []
                for item in obj:
                    part = flatten(item, block_sep=block_sep)
                    if part:
                        parts.append(part)
                return block_sep.join(parts)

            if isinstance(obj, dict):
                parts = []
                for value in obj.values():
                    part = flatten(value, block_sep=block_sep)
                    if part:
                        parts.append(part)
                return block_sep.join(parts)

            # Prefer .text when present
            if hasattr(obj, "text"):
                try:
                    part = flatten(getattr(obj, "text"), block_sep=block_sep)
                    if part:
                        return part
                except Exception:
                    pass

            parts = []

            # Common container fields that should be separated by lines
            for attr in ("blocks", "items"):
                try:
                    value = getattr(obj, attr, None)
                    if value:
                        part = flatten(value, block_sep="\n")
                        if part:
                            parts.append(part)
                except Exception:
                    pass

            # Generic fallback for everything else
            d = getattr(obj, "__dict__", None)
            if isinstance(d, dict):
                for key, value in d.items():
                    if key in ("blocks", "items", "text"):
                        continue
                    try:
                        part = flatten(value, block_sep="")
                        if part:
                            parts.append(part)
                    except Exception:
                        pass

            slots = getattr(obj, "__slots__", None)
            if slots:
                for key in slots:
                    if key in ("blocks", "items", "text"):
                        continue
                    try:
                        part = flatten(getattr(obj, key, None), block_sep="")
                        if part:
                            parts.append(part)
                    except Exception:
                        pass

            # Clean up empty lines
            return "\n".join(line for line in parts if line and line.strip())

        result = flatten(getattr(rich, "blocks", rich), block_sep="\n")
        return result or ""

    except Exception:
        return ""

# # #################### Sequence Steps ################################################################

async def running_sequence(event, message_text, owner_id=None):
    """Run the main, language and post sequence
    """
    owner_id = owner_id or event.sender_id
    response_buttons = [[
        types.KeyboardButtonCallback(
            text="❌️ Abort",
            data=f"abort_sequence:{owner_id}".encode(),
            style=types.KeyboardButtonStyle(bg_danger=True),
        )
    ]]

    response_message = await event.reply("Processing your request...", buttons=response_buttons)
    sequence_key = (response_message.chat_id, response_message.id)
    _active_sequences[sequence_key] = asyncio.current_task()

    environment = None
    timeout_task = None
    refresh_task = None
    status_messages = []
    predicted_steps = len(config.main_sequence) + 7
    latest_response = ""
    guidance = ""
    retry_feedback = ""
    sequence_ended = False

    def make_response_message():
        output = environment.output if environment else ""
        output = re.sub(r"\x1B(?:[@-_][0-?]*[ -/]*[@-~]|\[[0-?]*[ -/]*[@-~])", "", output)
        output = output.replace("\x00", "")
        if len(output) > config.telegram_output_limit:
            truncation_message = "[Earlier environment output hidden. Showing the latest output.]\n\n"
            output = truncation_message + output[-(config.telegram_output_limit - len(truncation_message)):]
        output = output or "Environment is not initialized yet..."
        status = "\n".join(f"- {html.escape(message)}" for message in status_messages[-10:])

        response = (
            "<details open>\n<summary>ᯤ Environment output</summary>\n\n"
            f"```text\n{output}\n```\n"
            "</details>\n\n"

            f"<details open>\n<summary>ᯤ Run status ({len(status_messages)}/{predicted_steps})</summary>\n\n"
            f"{status}\n"
            "</details>"
        )
        if guidance:
            response += (
                "\n\n<details open>\n<summary>ᯤ Possible fixes</summary>\n\n"
                f"{guidance}\n"
                "</details>"
            )
        
        return response

    async def update_response(message=None, replace_index=None, predicted_add=0, end_sequence=False):
        nonlocal latest_response, predicted_steps, sequence_ended
        predicted_steps += predicted_add

        if end_sequence:
            sequence_ended = True

        if replace_index is not None:
            status_messages[replace_index] = message
            print(message, flush=True)
        elif message:
            status_messages.append(message)
            print(message, flush=True)

        predicted_steps = max(predicted_steps, len(status_messages))
        markdown_text = make_response_message()

        if (markdown_text != latest_response) or message or predicted_add:
            latest_response = markdown_text
            await edit_rich_message(response_message, markdown_text, response_buttons if not sequence_ended else None)

        return replace_index if replace_index is not None else len(status_messages) - 1

    async def refresh_output():
        while True:
            await asyncio.sleep(config.output_refresh_interval)
            await update_response()

    async def timeout_environment():
        await asyncio.sleep(config.env_timeout)
        if environment and environment.is_running:
            await update_response("❌️ Environment timeout reached. Destroying the VM.", end_sequence=True)
            await environment.destroy(timed_out=True)

    try:
        if _run_queue.locked():
            await update_response(f"🔄 All execution slots ({config.concurrent_runs}) are busy. Run is queued.", predicted_add=1)

        async with _run_queue:
            await update_response("[x] ==Execution started.==")
            refresh_task = asyncio.create_task(refresh_output())

            for attempt in range(1, config.max_attempts + 1):
                await update_response(f"[x] ==Starting attempt {attempt} of {config.max_attempts}.==")
                sequence_data = await run_main_sequence(message_text, update_response)
                if sequence_data is None:
                    return
                sequence_data["retry_feedback"] = retry_feedback

                language_config = next(
                    (step for step in sequence_data["language_steps"] if "overlay_path" in step),
                    None,
                )
                if language_config is None:
                    raise EnvironmentError("Language sequence has no environment configuration.")

                if environment is None:
                    environment_status = await update_response("🔄 Launching disposable environment.")
                    environment = QemuEnvironment(sequence_data["language"], language_config["overlay_path"])
                    await environment.start()
                    timeout_task = asyncio.create_task(timeout_environment())
                    await update_response("[x] ==Environment is ready.==", replace_index=environment_status)

                result, commands = await run_language_sequence(
                    environment,
                    sequence_data,
                    update_response,
                    attempt,
                )
                if result is not None and result.exit_code == 0:
                    await update_response("[x] ==Code execution completed successfully.==", end_sequence=True)
                    return

                retry, guidance, retry_feedback = await run_post_sequence(
                    environment,
                    sequence_data,
                    result,
                    commands,
                    update_response,
                    attempt,
                )
                if not retry:
                    if guidance:
                        await update_response("❌️ ==Run failed. See possible fixes below.==", end_sequence=True)
                    else:
                        await update_response("❌️ ==Run failed and cannot be retried automatically.==", end_sequence=True)
                    return

    except asyncio.CancelledError:
        await update_response("❌️ ==Run aborted by owner.==", end_sequence=True)
    
    except Exception as e:
        await update_response(f"❌️ ==Sequence aborted:== `{e}`", end_sequence=True)

    finally:
        for task in (refresh_task, timeout_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if environment is not None:
            await environment.destroy()
        _active_sequences.pop(sequence_key, None)
        await update_response()


def get_step_fields(step):
    """Return the configured JSON response fields for a sequence step.
    """
    schema = step.get("response_format", {}).get("json_schema", {}).get("schema", {})
    return schema.get("properties", {})


async def generate_step(step, sequence_data):
    """Run one AI sequence step and return its JSON response.
    """
    response = await TextGen.generate(
        model_name=step["model_name"],
        messages=[{
            "role": "user",
            "content": json.dumps(sequence_data, ensure_ascii=False, indent=2),
        }],
        system_prompt=step["system_prompt"],
        max_tokens=step.get("max_tokens", config.sequence_max_tokens),
        temperature=step.get("temperature", 0),
        response_format=step.get("response_format", {"type": "text"}),
    )

    response = response.strip()
    if response.startswith("```"):
        response = "\n".join(response.splitlines()[1:-1]).strip()
    return json.loads(response)


async def run_steps(steps, sequence_data, update_response):
    """Run configured AI steps in order and keep their results in one dictionary.
    """
    for step in steps:
        if "model_name" not in step:
            continue

        await update_response(f"🔘 {step['description']}")
        response = await generate_step(step, sequence_data)
        sequence_data.update(response)

        # The main sequence can stop as soon as a configured result says there is nothing to run.
        if response.get("contains") is False or response.get("code_parts") == "":
            return None

    return sequence_data


async def run_commands(environment, commands, update_response):
    """Run generated guest commands in order, stopping at the first non-zero exit code.
    """
    await update_response(predicted_add=1 if commands else 0)
    result = None
    command_status = None
    for number, command in enumerate(commands, start=1):
        if not isinstance(command, str) or not command.strip():
            continue

        command_status = await update_response(
            f"🔘 Running command {number} of {len(commands)}.",
            replace_index=command_status,
        )
        result = await environment.execute(command)
        if result.exit_code != 0:
            await update_response(
                f"⚠️ command {number} exited with status {result.exit_code}.",
                replace_index=command_status,
            )
            return result
        elif number == len(commands):
            await update_response(
                f"[x] Running command {number} of {len(commands)}.",
                replace_index=command_status,
            )

    return result


async def run_main_sequence(message_text, update_response):
    """Use config.main_sequence to detect, extract, and identify submitted code.
    """
    sequence_data = await run_steps(
        config.main_sequence,
        {"message_text": message_text},
        update_response,
    )
    if sequence_data is None:
        await update_response("❌️ ==No executable code was found. Run was aborted.==", end_sequence=True)
        return None

    language = sequence_data.get("language")
    language_steps = config.languages_sequence.get(language)
    if language == "None" or not language_steps:
        await update_response("❌️ ==Code language is unsupported or is not implemented yet.==", end_sequence=True)
        return None

    code = sequence_data.get("code_parts", "")
    if not code:
        await update_response("❌️ ==No executable code was found. Run was aborted.==", end_sequence=True)
        return None

    return {
        "code": code,
        "custom_instructions": sequence_data.get("custom_instructions", ""),
        "language": language,
        "language_steps": language_steps,
    }


async def run_language_sequence(environment, sequence_data, update_response, attempt):
    """Write the code, collect guest details, generate commands, and run them.
    """
    language_config = next(
        (step for step in sequence_data["language_steps"] if "overlay_path" in step),
        None,
    )
    if language_config is None:
        raise EnvironmentError("Llanguage sequence has no environment configuration.")

    file_name = language_config["file_name"]
    code_status = await update_response(
        f"[ ] Writing extracted {sequence_data['language']} code to {file_name}."
    )
    guest_file = await environment.write_code_file(file_name, sequence_data["code"])
    await update_response(
        f"[x] Writing extracted {sequence_data['language']} code to {file_name}.",
        replace_index=code_status,
    )

    environment_status = await update_response("[ ] Collecting guest environment details.")
    environment_data = (await environment.execute(config.environment_details_command)).output
    await update_response("[x] Collected guest environment details.", replace_index=environment_status)

    planner_data = {
        "attempt": attempt,
        "language": sequence_data["language"],
        "file_name": file_name,
        "guest_file": guest_file,
        "code": sequence_data["code"],
        "custom_instructions": sequence_data["custom_instructions"],
        "environment": environment_data,
        "retry_feedback": sequence_data.get("retry_feedback", ""),
    }

    commands = []
    planner_steps = sum("model_name" in step for step in sequence_data["language_steps"])
    await update_response(predicted_add=max(0, planner_steps - 1))
    for step in sequence_data["language_steps"]:
        if "model_name" not in step:
            continue

        await update_response(f"🔘 {step['description']}")
        response = await generate_step(step, planner_data)
        planner_data.update(response)
        commands.extend(response.get("commands", []))

    if not commands:
        raise EnvironmentError("The language sequence did not return any execution commands.")

    return await run_commands(environment, commands, update_response), commands


async def run_post_sequence(environment, sequence_data, failure, commands, update_response, attempt):
    """Run config.post_sequence and return whether the code should be attempted again.
    """
    post_data = {
        "code": sequence_data["code"],
        "custom_instructions": sequence_data["custom_instructions"],
        "commands": commands,
        "failed_command": failure.command,
        "exit_code": failure.exit_code,
        "environment_output": environment.output[-config.telegram_output_limit:],
    }
    guidance = ""
    retry_feedback = ""

    for step in config.post_sequence:
        if step.get("type") == "local":
            if failure.exit_code == 0:
                return False, guidance, retry_feedback
            await update_response(
                f"❌️ ==Execution failed with exit status {failure.exit_code}. Starting post sequence.==",
                predicted_add=3,
            )
            continue

        fields = get_step_fields(step)
        if "commands" in fields and (
            not post_data.get("environment_related") or attempt >= config.max_attempts
        ):
            continue

        await update_response(f"🔘 {step['description']}")
        response = await generate_step(step, post_data)
        post_data.update(response)
        retry_feedback = response.get("retry_feedback", retry_feedback)

        if "commands" in response:
            repair = await run_commands(environment, response["commands"], update_response)
            repair_succeeded = repair is None or repair.exit_code == 0
            if (
                repair_succeeded
                and response.get("retry_execution")
                and attempt < config.max_attempts
            ):
                await update_response(
                    "[x] ==Execution plan corrected. Starting the next attempt.==",
                    predicted_add=len(config.main_sequence) + 5,
                )
                return True, guidance, retry_feedback
            if repair is not None and repair.exit_code == 0 and attempt < config.max_attempts:
                await update_response(
                    "[x] ==Environment repair completed. Starting the next attempt.==",
                    predicted_add=len(config.main_sequence) + 5,
                )
                return True, guidance, retry_feedback

        if "guidance" in response:
            guidance = response["guidance"]

    if post_data.get("environment_related") and attempt >= config.max_attempts:
        guidance = guidance or "The environment could not be repaired within the configured number of attempts."

    return False, guidance, retry_feedback


def contains_rtl(string):
    """Check if the string contains any right-to-left characters.
    """
    return any(unicodedata.bidirectional(ch) in ("R", "AL", "AN") for ch in string)

# # #################### Rich Messages #################################################################

async def send_rich_message(event, markdown_text, reply_to_msg_id=None, buttons=None):
    """Send a telegram-native rich message with markdown formatting, optionally replying to a specific message.
    """
    await event.client(
        functions.messages.SendMessageRequest(
            peer=event.input_chat,
            reply_to=types.InputReplyToMessage(reply_to_msg_id=reply_to_msg_id) if reply_to_msg_id else None,
            message="",
            random_id=random.getrandbits(63),
            rich_message=types.InputRichMessageMarkdown(
                markdown=markdown_text,
                rtl=contains_rtl(markdown_text)
            ),
            reply_markup=types.ReplyInlineMarkup(
                rows=[types.KeyboardButtonRow(buttons=row) for row in buttons]
            ) if buttons else None,
        )
    )


async def edit_rich_message(event, markdown_text, buttons=None):
    """Edit a telegram-native rich message with markdown formatting.
    """
    await event.client(
        functions.messages.EditMessageRequest(
            peer=event.input_chat,
            id=event.id,
            message="",
            rich_message=types.InputRichMessageMarkdown(
                markdown=markdown_text,
                rtl=contains_rtl(markdown_text)
            ),
            reply_markup=types.ReplyInlineMarkup(
                rows=[types.KeyboardButtonRow(buttons=row) for row in buttons]
            ) if buttons else None,
        )
    )

# # #################### External Handlers #############################################################

@client.on(events.CallbackQuery)
async def callback_handler(event):
    """Handle callback queries.
    """
    data = event.data.decode("utf-8", errors="replace")

    # ? Handle abort sequence requests
    if data.startswith("abort_sequence:"):
        if event.sender_id != int(data.split(":", 1)[1]):
            await event.answer("This panel is not yours.")
            return

        sequence_task = _active_sequences.pop((event.chat_id, event.message_id), None)
        if sequence_task is None or sequence_task.done():
            await event.answer("This run is no longer active.")
            return

        sequence_task.cancel()
        await event.answer("Aborting run...")

# # #################### Startup Settings ##############################################################

async def heartbeat():
    """Periodically check the bot's connection to Telegram and exit if it fails."""
    while True:
        try:
            me = await asyncio.wait_for(client.get_me(input_peer=True), timeout=60)
            if me is None:
                raise RuntimeError("get_me() returned None")
        except Exception as exc:
            print(f"Heartbeat Failed: {exc!r}", flush=True)
            sys.exit(1)
        
        # 3 minutes interval
        await asyncio.sleep(180)


async def main():
    me = await client.get_me()
    config.bot_name = me.first_name
    config.bot_username = me.username
    config.bot_id = me.id
    
    print(f'Connected to {config.bot_name} in @{config.bot_username}', flush=True)
    asyncio.create_task(heartbeat())


@client.on(events.NewMessage())
async def handle_messages(event):
    await process_message(event)


try:
    print('(Press Ctrl+C to stop this)', flush=True)
    client.loop.run_until_complete(main())
    client.run_until_disconnected()
finally:
    client.disconnect()
