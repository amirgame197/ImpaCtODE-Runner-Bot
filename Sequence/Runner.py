from Sequence.Environment import EnvironmentError, QemuEnvironment
from Sequence import TextGen
import contextlib
import asyncio
import config
import inspect
import json

_run_queue = asyncio.Semaphore(config.concurrent_runs)

# # #################### Sequence Steps ################################################################

async def send_update(update_callback, update):
    """Send a normal sequence update to the active interface.
    """
    response = update_callback(update)
    if inspect.isawaitable(response):
        return await response
    return response


async def running_sequence(message_text, update_callback):
    """Run the main, language and post sequence.
    """
    environment = None
    timeout_task = None
    output_task = None
    retry_feedback = ""
    status_count = 0
    latest_environment_output = ""
    state = "completed"

    async def update_response(message=None, replace_index=None, predicted_add=0, end_sequence=False):
        nonlocal status_count
        await send_update(update_callback, {
            "type": "status",
            "message": message,
            "replace_index": replace_index,
            "predicted_add": predicted_add,
            "end_sequence": end_sequence,
        })

        if message and replace_index is None:
            status_count += 1
        return replace_index if replace_index is not None else status_count - 1

    async def update_environment_output():
        last_output = None
        while True:
            await asyncio.sleep(0.05)
            output = latest_environment_output
            if output != last_output:
                last_output = output
                await send_update(update_callback, {
                    "type": "environment",
                    "output": output,
                })
            if output == latest_environment_output:
                return

    def environment_output_update(update):
        nonlocal latest_environment_output, output_task
        latest_environment_output = update.get("output", "")
        if output_task is None or output_task.done():
            output_task = asyncio.create_task(update_environment_output())

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
            await send_update(update_callback, {"type": "started"})

            for attempt in range(1, config.max_attempts + 1):
                await update_response(f"[x] ==Starting attempt {attempt} of {config.max_attempts}.==")
                sequence_data = await run_main_sequence(message_text, update_response)
                if sequence_data is None:
                    state = "failed"
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
                    environment = QemuEnvironment(
                        sequence_data["language"],
                        language_config["overlay_path"],
                        output_observer=environment_output_update,
                    )
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
                    state = "completed"
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
                        await send_update(update_callback, {
                            "type": "guidance",
                            "guidance": guidance,
                        })
                        await update_response("❌️ ==Run failed. See possible fixes below.==", end_sequence=True)
                    else:
                        await update_response("❌️ ==Run failed and cannot be retried automatically.==", end_sequence=True)
                    state = "failed"
                    return

    except asyncio.CancelledError:
        await update_response("❌️ ==Run aborted by owner.==", end_sequence=True)
        state = "aborted"
    
    except Exception as e:
        await update_response(f"❌️ ==Sequence aborted:== `{e}`", end_sequence=True)
        state = "error"

    finally:
        for task in (output_task, timeout_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        if environment is not None:
            await environment.destroy()
            await send_update(update_callback, {
                "type": "environment",
                "output": environment.output,
            })
        await send_update(update_callback, {
            "type": "finished",
            "state": state,
            "ended": True,
        })


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
    if language == "None" or not language_steps or not language_steps[0].get("overlay_path").is_file():
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


async def run_sequence(message_text, update_callback):
    """Run a sequence with a normal update callback.
    """
    await running_sequence(message_text, update_callback)
