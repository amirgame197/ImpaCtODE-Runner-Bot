from openai import AsyncOpenAI
import config

client = AsyncOpenAI(api_key=config.openai_api_key, base_url=config.openai_base_url)

async def generate(model_name, messages, system_prompt, max_tokens, temperature=0.7, response_format={"type": "text"}):
    """Generate a response using the OpenAI SDK using the specified model and parameters.
    """
    messages = [{"role": "system", "content": system_prompt}] + messages

    res = await client.chat.completions.create(
        model=model_name,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=False,
        response_format=response_format
    )

    print(res, flush=True)
    return res.choices[0].message.content
