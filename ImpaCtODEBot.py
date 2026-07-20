from telethon.tl.types import *
from telethon import *

from Sequence import Runner
import unicodedata
import contextlib
import asyncio
import random
import config
import html
import sys
import re

config.token = config.get_environment_variable("IMPACTODE_TELEGRAM_BOT_TOKEN")
config.app_id = config.get_environment_variable("IMPACTODE_TELEGRAM_APP_ID")
config.app_hash = config.get_environment_variable("IMPACTODE_TELEGRAM_APP_HASH")

client = TelegramClient('impactode', config.app_id, config.app_hash, connection_retries=None, retry_delay=15).start(bot_token=config.token) # token
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

    refresh_task = None
    status_messages = []
    predicted_steps = len(config.main_sequence) + 7
    latest_response = ""
    guidance = ""
    environment_output = ""
    sequence_ended = False

    def make_response_message():
        output = re.sub(r"\x1B(?:[@-_][0-?]*[ -/]*[@-~]|\[[0-?]*[ -/]*[@-~])", "", environment_output)
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
            await asyncio.sleep(config.telegram_output_refresh_interval)
            await update_response()

    async def receive_runner_update(update):
        # ? Runner sends neutral updates and the bot keeps its existing rich-message UI
        nonlocal environment_output, guidance, refresh_task

        if update["type"] == "started":
            refresh_task = asyncio.create_task(refresh_output())

        elif update["type"] == "environment":
            environment_output = update["output"]

        elif update["type"] == "guidance":
            guidance = update["guidance"]

        elif update["type"] == "status":
            return await update_response(
                update["message"],
                replace_index=update["replace_index"],
                predicted_add=update["predicted_add"],
                end_sequence=update["end_sequence"],
            )

    try:
        await Runner.running_sequence(message_text, receive_runner_update)

    finally:
        if refresh_task is not None:
            refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresh_task

        _active_sequences.pop(sequence_key, None)
        await update_response()


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
