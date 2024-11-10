import copy
from enum import Enum
from typing import Any, Awaitable, Callable, List, cast
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam, ChatCompletionChunk
from config import IS_DEBUG_ENABLED
from debug.DebugFileWriter import DebugFileWriter
from image_processing.utils import process_image
import json
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from utils import pprint_prompt


# Actual model versions that are passed to the LLMs and stored in our logs
class Llm(Enum):
    GPT_4_VISION = "gpt-4-vision-preview"
    GPT_4_TURBO_2024_04_09 = "gpt-4-turbo-2024-04-09"
    GPT_4O_2024_05_13 = "gpt-4o-2024-05-13"
    CLAUDE_3_SONNET = "claude-3-sonnet-20240229"
    CLAUDE_3_OPUS = "claude-3-opus-20240229"
    CLAUDE_3_HAIKU = "claude-3-haiku-20240307"
    CLAUDE_3_5_SONNET_2024_06_20 = "claude-3-5-sonnet-20240620"
    CLAUDE_3_5_SONNET_2024_10_22 = "claude-3-5-sonnet-20241022"
    # AWS_CLAUDE_3_5_SONNET = "anthropic.claude-3-5-sonnet-20241022-v1:0"
    AWS_CLAUDE_3_5_SONNET = "anthropic.claude-3-5-sonnet-20240620-v1:0"
    AWS_CLAUDE_3_OPUS = "anthropic.claude-3-opus-20240229-v1:0"

# Will throw errors if you send a garbage string
def convert_frontend_str_to_llm(frontend_str: str) -> Llm:
    if frontend_str == "gpt_4_vision":
        return Llm.GPT_4_VISION
    elif frontend_str == "claude_3_sonnet":
        return Llm.CLAUDE_3_SONNET
    else:
        return Llm(frontend_str)


async def stream_openai_response(
    messages: List[ChatCompletionMessageParam],
    api_key: str,
    base_url: str | None,
    callback: Callable[[str], Awaitable[None]],
    model: Llm,
) -> str:
    client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    # Base parameters
    params = {
        "model": model.value,
        "messages": messages,
        "stream": True,
        "timeout": 600,
        "temperature": 0.0,
    }

    # Add 'max_tokens' only if the model is a GPT4 vision or Turbo model
    if (
        model == Llm.GPT_4_VISION
        or model == Llm.GPT_4_TURBO_2024_04_09
        or model == Llm.GPT_4O_2024_05_13
    ):
        params["max_tokens"] = 4096

    stream = await client.chat.completions.create(**params)  # type: ignore
    full_response = ""
    async for chunk in stream:  # type: ignore
        assert isinstance(chunk, ChatCompletionChunk)
        if (
            chunk.choices
            and len(chunk.choices) > 0
            and chunk.choices[0].delta
            and chunk.choices[0].delta.content
        ):
            content = chunk.choices[0].delta.content or ""
            full_response += content
            await callback(content)

    await client.close()

    return full_response


# TODO: Have a seperate function that translates OpenAI messages to Claude messages
async def stream_claude_response(
    messages: List[ChatCompletionMessageParam],
    api_key: str,
    callback: Callable[[str], Awaitable[None]],
    model: Llm,
) -> str:

    client = AsyncAnthropic(api_key=api_key)

    # Base parameters
    max_tokens = 8192
    temperature = 0.0

    # Translate OpenAI messages to Claude messages

    # Deep copy messages to avoid modifying the original list
    cloned_messages = copy.deepcopy(messages)

    system_prompt = cast(str, cloned_messages[0].get("content"))
    claude_messages = [dict(message) for message in cloned_messages[1:]]
    for message in claude_messages:
        if not isinstance(message["content"], list):
            continue

        for content in message["content"]:  # type: ignore
            if content["type"] == "image_url":
                content["type"] = "image"

                # Extract base64 data and media type from data URL
                # Example base64 data URL: data:image/png;base64,iVBOR...
                image_data_url = cast(str, content["image_url"]["url"])

                # Process image and split media type and data
                # so it works with Claude (under 5mb in base64 encoding)
                (media_type, base64_data) = process_image(image_data_url)

                # Remove OpenAI parameter
                del content["image_url"]

                content["source"] = {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64_data,
                }

    # Stream Claude response
    async with client.messages.stream(
        model=model.value,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_prompt,
        messages=claude_messages,  # type: ignore
        extra_headers={"anthropic-beta": "max-tokens-3-5-sonnet-2024-07-15"},
    ) as stream:
        async for text in stream.text_stream:
            await callback(text)

    # Return final message
    response = await stream.get_final_message()

    # Close the Anthropic client
    await client.close()

    return response.content[0].text

async def stream_claude_response_native(
    system_prompt: str,
    messages: list[Any],
    api_key: str,
    callback: Callable[[str], Awaitable[None]],
    include_thinking: bool = False,
    model: Llm = Llm.CLAUDE_3_OPUS,
) -> str:

    client = AsyncAnthropic(api_key=api_key)

    # Base model parameters
    max_tokens = 4096
    temperature = 0.0

    # Multi-pass flow
    current_pass_num = 1
    max_passes = 2

    prefix = "<thinking>"
    response = None

    # For debugging
    full_stream = ""
    debug_file_writer = DebugFileWriter()

    while current_pass_num <= max_passes:
        current_pass_num += 1

        # Set up message depending on whether we have a <thinking> prefix
        messages_to_send = (
            messages + [{"role": "assistant", "content": prefix}]
            if include_thinking
            else messages
        )

        pprint_prompt(messages_to_send)

        async with client.messages.stream(
            model=model.value,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=messages_to_send,  # type: ignore
        ) as stream:
            async for text in stream.text_stream:
                print(text, end="", flush=True)
                full_stream += text
                await callback(text)

        response = await stream.get_final_message()
        response_text = response.content[0].text

        # Write each pass's code to .html file and thinking to .txt file
        if IS_DEBUG_ENABLED:
            debug_file_writer.write_to_file(
                f"pass_{current_pass_num - 1}.html",
                debug_file_writer.extract_html_content(response_text),
            )
            debug_file_writer.write_to_file(
                f"thinking_pass_{current_pass_num - 1}.txt",
                response_text.split("</thinking>")[0],
            )

        # Set up messages array for next pass
        messages += [
            {"role": "assistant", "content": str(prefix) + response.content[0].text},
            {
                "role": "user",
                "content": "You've done a good job with a first draft. Improve this further based on the original instructions so that the app is fully functional and looks like the original video of the app we're trying to replicate.",
            },
        ]

        print(
            f"Token usage: Input Tokens: {response.usage.input_tokens}, Output Tokens: {response.usage.output_tokens}"
        )

    # Close the Anthropic client
    await client.close()

    if IS_DEBUG_ENABLED:
        debug_file_writer.write_to_file("full_stream.txt", full_stream)

    if not response:
        raise Exception("No HTML response found in AI response")
    else:
        return response.content[0].text

def initialize_bedrock_client(access_key: str, secret_key: str, region: str):
    try:
        # Configure retry settings
        config = Config(
            region_name=region,
            retries = {
                'max_attempts': 10,  # 最大重试次数
                'mode': 'adaptive',  # 使用自适应重试模式
                'total_max_attempts': 10  # 总共最大尝试次数（包括初始请求）
            }
        )

        # Initialize the Bedrock Runtime client with retry configuration
        bedrock_runtime = boto3.client(
            service_name='bedrock-runtime',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=config
        )
        return bedrock_runtime
    except ClientError as err:
        message = err.response["Error"]["Message"]
        print(f"A client error occurred: {message}")
        raise err
    except Exception as err:
        print("An error occurred while initializing Bedrock client!")
        raise err

async def stream_bedrock_response(
        bedrock_runtime,
        messages: List[dict],
        system_prompt: str,
        model_id: str,
        max_tokens: int,
        content_type: str,
        accept: str,
        temperature: float,
        callback: Callable[[str], Awaitable[None]]
) -> str:
    try:
        # Convert messages to the format expected by Bedrock
        formatted_messages = []
        for msg in messages:
            if msg["role"] == "system":
                continue  # Skip system messages as they should be in the system field
            
            formatted_msg = {"role": msg["role"]}
            
            # Handle messages with image content
            if isinstance(msg.get("content"), list):
                formatted_content = []
                for content in msg["content"]:
                    if content["type"] == "text":
                        formatted_content.append(content)
                    elif content["type"] == "image_url":
                        # Extract base64 data and media type from data URL
                        image_data_url = content["image_url"]["url"]
                        
                        # Process image and split media type and data
                        (media_type, base64_data) = process_image(image_data_url)
                        
                        # Format for Bedrock
                        formatted_content.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64_data
                            }
                        })
                formatted_msg["content"] = formatted_content
            else:
                # Handle regular text messages
                formatted_msg["content"] = msg["content"]
            
            formatted_messages.append(formatted_msg)

        # Prepare the request body
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "messages": formatted_messages,
            "system": system_prompt,
            "temperature": temperature
        })

        # Invoke the Bedrock Runtime API with response stream
        response = bedrock_runtime.invoke_model_with_response_stream(
            body=body,
            modelId=model_id,
            accept=accept,
            contentType=content_type,
        )
        stream = response.get("body")

        # Stream the response
        final_message = ""
        if stream:
            for event in stream:
                chunk = event.get("chunk")
                if chunk:
                    data = chunk.get("bytes").decode()
                    chunk_obj = json.loads(data)
                    if chunk_obj["type"] == "content_block_delta":
                        text = chunk_obj["delta"]["text"]
                        await callback(text)
                        final_message += text

        return final_message

    except ClientError as err:
        message = err.response["Error"]["Message"]
        print(f"A client error occurred: {message}")
        raise err
    except Exception as err:
        print("An error occurred!")
        raise err

async def stream_claude_response_native_aws_bedrock(
        system_prompt: str,
        messages: list[Any],
        callback: Callable[[str], Awaitable[None]],
        model: Llm = Llm.AWS_CLAUDE_3_5_SONNET,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
        include_thinking: bool = False
) -> str:
    bedrock_runtime = initialize_bedrock_client(access_key, secret_key, region)

    # Set model parameters
    max_tokens = 4096
    content_type = 'application/json'
    accept = '*/*'
    temperature = 0.0

    # Multi-pass flow
    current_pass_num = 1
    max_passes = 2

    prefix = "<thinking>"
    final_response = ""
    
    while current_pass_num <= max_passes:
        # Set up message depending on whether we have a <thinking> prefix
        messages_to_send = (
            messages + [{"role": "assistant", "content": prefix}]
            if include_thinking
            else messages
        )

        try:
            response_text = await stream_bedrock_response(
                bedrock_runtime,
                messages_to_send,
                system_prompt,
                model.value,
                max_tokens,
                content_type,
                accept,
                temperature,
                callback,
            )
            
            # Store the final response from the last pass
            final_response = response_text

            # Set up messages array for next pass
            messages = messages + [
                {"role": "assistant", "content": str(prefix) + response_text},
                {
                    "role": "user",
                    "content": "You've done a good job with a first draft. Improve this further based on the original instructions so that the app is fully functional and looks like the original video of the app we're trying to replicate.",
                },
            ]

        except Exception as e:
            print(f"Error in pass {current_pass_num}: {str(e)}")
            if current_pass_num == 1:
                # If first pass fails, we should raise the error
                raise e
            else:
                # If subsequent pass fails, we can return the last successful response
                print("Using result from previous successful pass")
                break

        current_pass_num += 1

    if not final_response:
        raise Exception("No response generated from AWS Bedrock")

    return final_response