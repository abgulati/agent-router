from logging.handlers import RotatingFileHandler
from settings import read_config, write_config, safe_write_config
from pathlib import Path
import requests
import argparse
import logging
import signal
import json
import math
import os

from flask import Flask, jsonify, request, Response, stream_with_context
from flask_cors import CORS

from waitress import serve

app = Flask(__name__)
CORS(app)


#########################------------Observability & Error Logging-------------###############################
OBSERVA_PRINTABILITY = False
def print_observability(*messages: object):
    if OBSERVA_PRINTABILITY:
        print(*messages)


LOGS_DIR = Path.cwd() / 'logs'
os.makedirs(LOGS_DIR, exist_ok=True)
LOG_PATH = LOGS_DIR / 'router_log.log'

try:
    LOGGER = logging.getLogger('my_logger')
    LOGGER.setLevel(logging.ERROR)
    
    handler = RotatingFileHandler(LOG_PATH, maxBytes=1024*1024*5, backupCount=2)
    handler.setLevel(logging.ERROR)
    
    formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(message)s')
    handler.setFormatter(formatter)
    
    LOGGER.addHandler(handler)
except Exception as e:
    print(f"\n\nCould not establish logger, encountered error: {e}")


def central_error_logging(message:str, exception:Exception=None):
    error_message = f"{message} {str(exception) if exception else '; No exception info.'}".strip()
    #traceback_details = traceback.format_exc()
    #full_message = f"\n\n{error_message}\n\nTraceback: {traceback_details}\n\n"
    full_message = f"\n\n{error_message}\n\n"
    if LOGGER:
        LOGGER.error(full_message)
        print(full_message)
    else:
        print(full_message)

    return error_message

def handle_api_error(message:str, exception:Exception=None):
    error_message = central_error_logging(message, exception)
    return jsonify(success=False, error=error_message), 500 #internal server error

def handle_local_error(message:str, exception:Exception=None):
    _ = central_error_logging(message, exception)
    if exception:
        raise  # preserves the original error & traceback
    raise RuntimeError(message)

def handle_error_no_return(message:str, exception:Exception=None):
    _ = central_error_logging(message, exception)

def handle_completions_error(
    message:str,
    exception:Exception=None,
    error_type:str="server_error",
    param:str=None,
    error_sub_code:str=None,
    http_status_code:int=500
) -> tuple[Response, int]:
    err_obj = {
        "message": message,
        "type": error_type,
        "param": param,
        "code": error_sub_code
    }
    _ = central_error_logging(message, exception)
    return jsonify(error=err_obj), http_status_code


############################----------------------------------------------###############################


@app.route('/read_config', methods=['POST'])
def config_reader_api():    
    try:
        keys = request.json['keys']
    except Exception as e:
        return handle_api_error("Request error - could not read 'keys' from request body:", e)

    try:
        values = read_config(keys)
    except Exception as e:
        return handle_api_error("Server-side error - could not read keys from config.json: ", e)
    
    return jsonify(success=True, values=values)


@app.route('/write_config', methods=['POST'])
def config_writer_api():

    try:
        config_updates = request.json['config_updates']
    except Exception as e:
        return handle_api_error(
            "Request error - could not read 'config_updates' from request body: ", e)
    
    try:
        write_return = write_config(config_updates)
    except Exception as e:
        return handle_api_error("Server-side error - could not write keys to config.json: ", e)
    
    return jsonify(success=True, message="Config written successfully", write_return=write_return)

############################----------------------------------------------###############################

HOP_BY_HOP = {
    'connection', 'keep-alive', 'proxy-authenticate',
    'proxy-authorization', 'te', 'trailer', 'transfer-encoding', 'upgrade',
}   # These headers are not forwardable and should be removed by a proxy per HTTP spec
# "hop-by-hop" official definition: https://datatracker.ietf.org/doc/html/rfc7230#section-6.1

OUTGOING_STRIP     = HOP_BY_HOP | {'host', 'authorization', 'content-length'}
PASSTHROUGH_STRIP  = HOP_BY_HOP | {'content-encoding', 'content-length'}


def add_auth_to_header(headers: dict, auth_type: str, auth_value: str) -> dict:
    authed_header = headers.copy()
    if auth_type == "bearerToken" and auth_value:
        authed_header['Authorization'] = f'Bearer {auth_value}'
    elif auth_type in (None, "none"):
        pass # local unauthenticated provider
    else:
        raise RuntimeError(f"Invalid auth type: {auth_type}")
    return authed_header


def handle_openai_streaming(
    selected_provider_and_model: dict,
    request: request
) -> Response:
    '''
    client thinks:
    client <---- SSE stream ---- LLM provider

    actual:
    client <---- SSE stream ---- router <---- SSE stream ---- provider
    '''
    try:
        print_observability("\nHandling OpenAI streaming response...\n")
        
        selected_provider = selected_provider_and_model.get("provider")
        selected_model = selected_provider_and_model.get("model")

        outgoing_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in OUTGOING_STRIP
        }
        '''
        Host / Authorization stripped because they're router-specific
        Content-Length / Content-Encoding: stripped because body is transformed here.
        Content-Length is also recomputed by Flask for the body returned.
        '''
        outgoing_headers['content-type'] = 'application/json'

        authed_header = add_auth_to_header(
            outgoing_headers,
            selected_provider.get("authType"),
            selected_provider.get("bearerToken")
        )

        payload = request.get_json()
        payload["model"] = selected_model["id"]
        payload["stream"] = True

        upstream = requests.post(
            selected_model["url"],
            headers=authed_header,
            json=payload,
            timeout=selected_model.get("timeout", 300),
            stream=True,
        )

        # Reconstruct a Flask response, forwarding safe upstream headers back
        passthrough_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in PASSTHROUGH_STRIP
        }   # see above for stripping details

        # Helpful for SSE-style OpenAI streaming
        passthrough_headers.setdefault('Content-Type', 'text/event-stream')
        passthrough_headers.setdefault('Cache-Control', 'no-cache')
        passthrough_headers.setdefault('X-Accel-Buffering', 'no') # in-case behind Nginx
        # tells Nginx not to buffer the response - not part of the OpenAi spec but harmless

        def generate():
            try:
                for chunk in upstream.iter_content(chunk_size=None):    # check iter def for details
                    if chunk:
                        yield chunk
            finally:
                upstream.close()

        return Response(
            stream_with_context(generate()),
            status=upstream.status_code,
            headers=passthrough_headers,
            direct_passthrough=True,    # check Response/ResponseBase
        )

    except Exception as e:
        handle_local_error("Error handling OpenAI streaming response: ", e)


def handle_openai_non_streaming(
    selected_provider_and_model: dict,
    request: request
) -> Response:
    try:
        print_observability("\nHandling OpenAI non-streaming response...\n")

        selected_provider = selected_provider_and_model.get("provider")
        selected_model = selected_provider_and_model.get("model")

        outgoing_headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in OUTGOING_STRIP
        }
        outgoing_headers['content-type'] = 'application/json'

        authed_header = add_auth_to_header(
            outgoing_headers,
            selected_provider.get("authType"),
            selected_provider.get("bearerToken")
        )

        payload = request.get_json()
        payload["model"] = selected_model["id"]

        upstream = requests.post(
            selected_model["url"],
            headers=authed_header,
            json=payload,
            timeout=selected_model.get("timeout", 300) # 5 minutes default
        )

        passthrough_headers = {
            k: v for k, v in upstream.headers.items()
            if k.lower() not in PASSTHROUGH_STRIP
        }   # see streaming route above for stripping details

        return Response(
            upstream.content,
            status=upstream.status_code,
            headers=passthrough_headers,
        )

    except Exception as e:
        handle_local_error("Error handling OpenAI non-streaming response: ", e)


CHARS_PER_TOKEN = 3
def estimate_request_tokens(data: dict) -> dict:
    """
    Estimate the number of tokens in a request payload.

    Methodology:
    - Extracts the 'messages' field from the input data, as it is the primary contributor to token count.
    - Serializes the extracted payload to a JSON string.
    - Estimates the token count by dividing the length of the JSON string by a constant (CHARS_PER_TOKEN),
      which approximates the average number of characters per token.
    - Returns a dictionary containing the estimated token count, the serialized text, and the total number of tools.

    Args:
        data (dict): The request payload containing 'messages' and optionally 'tools'.

    Returns:
        dict: A dictionary with keys 'request_token_count', 'text', and 'total_tools'.
    """
    try:
        token_relevant_payload = {
            "messages": data.get("messages", []),
            # "tools": data.get("tools", None),
            # "tool_choice": data.get("tool_choice", None),
            # "response_format": data.get("response_format", None),
        }

        text = json.dumps(
            token_relevant_payload,
            ensure_ascii=False
        )
        estimated_request_token_count = max(
            1,
            math.ceil(len(text) / CHARS_PER_TOKEN)
        )

        total_tools = 0
        if data.get("tools"):
            total_tools = len(data.get("tools", []))


        print_observability(
            f"Estimated request token count: {estimated_request_token_count}\n"
            f"Total tools: {total_tools}\n"
        )
        return {
            "request_token_count": estimated_request_token_count,
            "text": text,
            "total_tools": total_tools
        }

    except Exception as e:
        handle_local_error("Error calculating request token count: ", e)


def get_description_string_for_models(models: list[dict]) -> str:
    description_string = f"""
    Choose only ONE of the following model IDs for this provider.
    
    <models>
    {json.dumps(models, indent=4)}
    </models>
    """
    return description_string


def get_providers_as_tools_list(providers_by_name: dict) -> list[dict]:
    
    provider_choices_as_tools_list = []
    
    for provider_name, provider in providers_by_name.items():
        if provider.get("enabled"):
            models_description = get_description_string_for_models(provider.get("models", []))

            provider_choices_as_tools_list.append({
                "type": "function",
                "function": {
                    "name": provider_name,
                    "description": provider.get("description"),
                    "parameters": {
                        "additionalProperties": False,
                        "properties": {
                            "model": {
                                "type": "string",
                                "description": models_description,
                                "enum": [model["id"] for model in provider.get("models", [])]
                            },
                        },
                        "required": ["model"],
                        "type": "object"
                    }
                }
            })
    
    print_observability(
        "\nTools-listed configured providers: "
        f"{json.dumps(provider_choices_as_tools_list, indent=4)}\n"
    )
    return provider_choices_as_tools_list


QUERY_SAMPLE_TOKEN_LIMIT = 5000
def get_completions_compatible_llm_classifier_request_object(
    data: dict,
    tools: list[dict],
    model_id: str
) -> dict:
    try:
        request_token_count_info = estimate_request_tokens(data)
        text = request_token_count_info.get("text")
        total_tools = request_token_count_info.get("total_tools")
        
        max_chars = QUERY_SAMPLE_TOKEN_LIMIT * CHARS_PER_TOKEN
        if request_token_count_info.get("request_token_count") > QUERY_SAMPLE_TOKEN_LIMIT:
            omitted = math.ceil((len(text) - max_chars) / CHARS_PER_TOKEN)
            query_sample = f"[truncated first {omitted} tokens]...\n{text[-max_chars:]}"
        else:
            query_sample = text

        system_prompt = """
        You are a helpful assistant that helps the user select the best provider & model for their request.
        You act as a query router, routing the user's query to the most suitable LLM.

        You will be given a tools list specifying the available providers and their models.
        You will also be given the user's query, or a truncated sample if the messages object was too lengthy.
        
        Select the best LLM basis the complexity and requirements of the request.
        If unsure, play it safe and select the LLM you think is the most capable on the list.
        """

        user_prompt = f"""
        Provided below is the conversational context of the user's interaction with the agent so far,
        inclusive of the user's latest message.

        Their request history comprises a total of {total_tools} tools provided by their harness application.

        Select a provider and model combo from the tools list in this request to route their conversation 
        to the most suitable LLM.

        <query_sample>
        {query_sample}
        </query_sample>

        Select the best LLM basis the complexity and requirements of the request.
        If unsure, play it safe and select the LLM you think is the most capable on the list.
        """

        final_request_object = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "tools": tools,
            "tool_choice": "required",
            "stream": False
        }

        print_observability(
            "\nRequest object for LLM classifier:\n"
            f"{json.dumps(final_request_object, indent=4)}\n"
        )
        return final_request_object

    except Exception as e:
        handle_local_error("Error getting completions-compatible LLM classifier request object: ", e)


def select_provider_by_llm_classifier(
    llm_classifier_provider_name: str,
    llm_classifier_model_id: str,
    providers_by_name: dict,
    data: dict
) -> dict[str, dict]:
    try:
        print_observability("\nSelecting provider by LLM classifier...\n")

        if not providers_by_name :
            raise RuntimeError("No providers configured")

        provider_choices_as_tools_list = get_providers_as_tools_list(providers_by_name)
        completions_request_object = get_completions_compatible_llm_classifier_request_object(
            data,
            provider_choices_as_tools_list,
            llm_classifier_model_id
        )
        completions_request_payload = json.dumps(completions_request_object)

        classifier_provider_details = providers_by_name.get(llm_classifier_provider_name)
        classifier_provider_models_list = classifier_provider_details.get("models")
        classifier_provider_models_dict = {m["id"]: m for m in classifier_provider_models_list}
        classifier_url = classifier_provider_models_dict.get(llm_classifier_model_id).get("url")

        headers = {
            'Content-Type': 'application/json'
        }
        authed_header = add_auth_to_header(
            headers,
            classifier_provider_details.get("authType"),
            classifier_provider_details.get("bearerToken")
        )
        
        classifier_response = requests.request(
            "POST",
            classifier_url,
            headers=authed_header,
            data=completions_request_payload,
            timeout=30
        )
        if classifier_response.status_code != 200:
            raise RuntimeError(f"Error calling classifier: {classifier_response.status_code}")
        
        classifier_response_json = classifier_response.json()
        provider_as_tool_selection = (
            classifier_response_json
            .get("choices")[0]
            .get("message")
            .get("tool_calls")[0]
        )
   
        provider_name = provider_as_tool_selection.get("function").get("name")
        model_as_args = provider_as_tool_selection.get("function").get("arguments")
        
        parsed_model_as_args = json.loads(model_as_args)
        model_id = parsed_model_as_args.get("model")
        
        selected_provider = providers_by_name.get(provider_name)
        selected_model = None
        if selected_provider:
            selected_provider_models_dict = {
                m["id"]: m for m in selected_provider.get("models", [])
            }
            selected_model = selected_provider_models_dict.get(model_id)
        
        if not selected_provider or not selected_model:
            raise RuntimeError(f"Provider {provider_name} or model {model_id} not found")

        classifier_selection = {
            "provider": selected_provider,
            "model": selected_model
        }
        print_observability(
            "\n\nSelected provider and model via LLM classifier strategy:\n"
            f"{json.dumps(classifier_selection, indent=4)}\n\n"
        )
        return classifier_selection
        
    except Exception as e:
        handle_local_error("Error selecting provider by LLM classifier: ", e)
    

def select_provider_by_token_threshold(
    token_thresholds: list[dict],
    providers_by_name: dict,
    data: dict
) -> dict[str, dict]:
    try:
        print_observability("Selecting provider by token threshold...")

        if not token_thresholds:
            raise RuntimeError("No token thresholds configured")

        request_token_count_info = estimate_request_tokens(data)
        request_token_count = request_token_count_info.get("request_token_count")

        min_viable_threshold = float('inf')
        selected_provider = None
        selected_model = None
        
        for provider_spec in token_thresholds:
            if (
                provider_spec.get("maxInputTokens") >= request_token_count
                and provider_spec.get("maxInputTokens") < min_viable_threshold
            ):  # provider can handle request and it's the best fit so far

                provider_name = provider_spec.get("provider_name")
                model_id = provider_spec.get("model_id")

                if (
                    provider_name not in providers_by_name
                    or not providers_by_name[provider_name].get("enabled", False)
                ):
                    continue

                print_observability(f"\nEvaluating provider: {provider_name}\n")

                models_by_id = {
                    m["id"]: m for m in providers_by_name[provider_name].get("models", [])
                }   # models configured for this provider
                
                if model_id not in models_by_id:
                    continue

                print_observability(f"\nModel: {model_id} found in provider: {provider_name}\n")
               
                min_viable_threshold = provider_spec.get("maxInputTokens")
                selected_provider = providers_by_name[provider_name]
                selected_model = models_by_id[model_id]

                print_observability(f"\nNew viable provider found: {provider_name} with model {model_id}\n")

        if selected_provider is None or selected_model is None:
            raise RuntimeError(
                "No viable provider found\n"
                f"Selected provider: {selected_provider}\n"
                f"Selected model: {selected_model}\n"
            )
        
        token_threshold_selection = {
            "provider": selected_provider,
            "model": selected_model
        }
        print_observability(
            "\n\nSelected provider and model via token threshold strategy:\n"
            f"{json.dumps(token_threshold_selection, indent=4)}\n\n"
        )
        return token_threshold_selection

    except Exception as e:
        handle_local_error("Error selecting provider by token threshold: ", e)


def select_provider(providers_by_name: dict, data: dict) -> dict[str, dict]:
    try:
        router_config = read_config(["router"]).get("router")
        routing_strategy = router_config.get("routing_strategy")

        if (
            routing_strategy == "llm_classifier"
            and router_config.get("llm_classifier_provider_name")
            and router_config.get("llm_classifier_model_id")
        ):
            return select_provider_by_llm_classifier(
                router_config.get("llm_classifier_provider_name"),
                router_config.get("llm_classifier_model_id"),
                providers_by_name,
                data
            )
        
        elif (
            routing_strategy == "token_threshold"
            and router_config.get("token_thresholds")
        ):
            return select_provider_by_token_threshold(
                router_config.get("token_thresholds"),
                providers_by_name,
                data
            )
        
        else:
            raise RuntimeError(f"Invalid routing strategy: {routing_strategy}")

    except Exception as e:
        handle_local_error("Error selecting provider: ", e)


def get_available_providers() -> list[dict]:
    try:
        providers = read_config(["providers"]).get("providers")

        enabled_providers = []
        for provider in providers:
            if provider.get("enabled", False):
                enabled_providers.append(provider)
        
        return enabled_providers
    
    except Exception as e:
        handle_local_error("Error getting available providers: ", e)


@app.route('/v1/chat/completions', methods=['POST'])
def openai_compatible_api():
    """
    OpenAI-compatible chat completions endpoint that routes to available providers.
    """

    route_invoked_log = f'''
    \n\n========== OPENAI API REQUEST ==========\n
    Headers: {dict(request.headers)}\n
    Body: {request.get_data(as_text=True)}\n
    ==========================================\n\n
    '''
    print_observability(route_invoked_log)

    print("\n\nOpenAI v1/chat/completions route triggered\n\n")
    
    try:
        providers = get_available_providers()
        if not providers:
            raise RuntimeError("No providers configured/enabled")
        providers_by_name = {p["name"]: p for p in providers}
    except Exception as e:
        return handle_completions_error(
            f"Error getting available providers: {str(e)}",
            exception=e,
            error_type="server_error",
            param=None,
            error_sub_code=None,
            http_status_code=500
        )
    
    try:
        data = request.json
        if isinstance(data, str):
            data = json.loads(data)
    except Exception as e:
        return handle_completions_error(
            f"Invalid request format: {str(e)}",
            exception=e,
            error_type="invalid_request_error",
            param=None,
            error_sub_code=None,
            http_status_code=400
        )

    try:
        selected_provider_and_model = select_provider(providers_by_name, data)
        print(
            "\n\nSelected provider and model:\n"
            f"{json.dumps(selected_provider_and_model, indent=4)}\n\n"
        )
    except Exception as e:
        return handle_completions_error(
            f"Error selecting provider: {str(e)}",
            exception=e,
            error_type="server_error",
            param=None,
            error_sub_code=None,
            http_status_code=500
        )

    selected_provider_api_type = selected_provider_and_model.get("provider").get("apiType")

    if selected_provider_api_type == "chat-completions":
        try:
            if data.get("stream", False):
                return handle_openai_streaming(selected_provider_and_model, request)
            else:
                return handle_openai_non_streaming(selected_provider_and_model, request)
        except Exception as e:
            return handle_completions_error(
                f"Error handling OpenAI chat-completions response: {str(e)}",
                exception=e,
                error_type="server_error",
                param=None,
                error_sub_code=None,
                http_status_code=500
            )
    else:
        return handle_completions_error(
            f"Invalid API type: {selected_provider_api_type}",
            error_type="server_error",
            http_status_code=500
        )




###########################---------------Server Startup----------------#################################

def parse_arguments() -> argparse.Namespace:

    try:
        parser = argparse.ArgumentParser(description="Server for routing requests to the appropriate provider")

        config = read_config(["router", "providers"])

        if parser:
            parser.add_argument(
                "--reset_to_defaults", action="store_true", default=False,
                help="Erase all custom settings and reset to defaults."
            )

            parser.add_argument(
                "--router_serving_address", type=str, default=config["router"]["router_serving_address"],
                help="Address to serve the router on. Remembers previously set value. Default: 0.0.0.0"
            )

            parser.add_argument(
                "--router_serving_port", type=int, default=config["router"]["router_serving_port"],
                help="Port to serve the router on. Remembers previously set value. Default: 8765"
            )

            parser.add_argument(
                "--router_access_address", type=str, default=config["router"]["router_access_address"],
                help="Address to access the router on. Remembers previously set value. Default: localhost"
            )

            parser.add_argument(
                "--routing_strategy", type=str, default=config["router"]["routing_strategy"],
                help="Strategy to use for routing requests. Remembers previously set value. Default: token_threshold"
            )

            parser.add_argument(
                "--llm_classifier_provider_name", type=str, default=config["router"]["llm_classifier_provider_name"],
                help="Provider to use for the LLM classifier. Remembers previously set value. Default: None"
            )

            parser.add_argument(
                "--llm_classifier_model_id", type=str, default=config["router"]["llm_classifier_model_id"],
                help="Model to use for the LLM classifier. Remembers previously set value. Default: None"
            )

            args = parser.parse_args()

            if args.reset_to_defaults:
                print("\n\nLoading Server with Safe Defaults\n\n")
                
                with open(Path.cwd() / 'config.json', 'w') as file:
                    json.dump({}, file, indent=4)   # Empty config.json

                # Set defaults
                read_config(["router", "providers"])

            else:
                write_config({
                    "router": {
                        "router_serving_address": args.router_serving_address,
                        "router_serving_port": args.router_serving_port,
                        "router_access_address": args.router_access_address,
                        "routing_strategy": args.routing_strategy,
                        "llm_classifier_provider_name": args.llm_classifier_provider_name,
                        "llm_classifier_model_id": args.llm_classifier_model_id,
                        "token_thresholds": config["router"].get("token_thresholds", []),
                    }
                })
            
            return args
        
        return None

    except Exception as e:
        handle_local_error("Error parsing launch args: ", e)


def get_host_and_port() -> tuple[str, int]:
    try:
        router_config = read_config(["router"]).get("router")
        return (
            router_config.get("router_serving_address"),
            router_config.get("router_serving_port")
        )
    except Exception as e:
        handle_error_no_return("Could not get host and port from config.json: ", e)


def signal_handler(sig, frame):
    '''
    Signal handler for the main process.
    Shuts down the server gracefully by intercepting the interrupt "SIGINT" signal (Ctrl+C).

    Hard exit:
    os._exit(0) is used instead of sys.exit(0) because it skips Python's cleanup handlers (except 
    finally blocks) and immediately terminates the process.
    '''
    print("\n\n⚠️  CTRL+C Detected! Force stopping workers...\n")
    print("👋 Exiting immediately.")
    os._exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    _ = parse_arguments()
    router_host, router_port = get_host_and_port()
    print(f"\n\nServing Router on {router_host} port {router_port}\n\n")
    # app.run(debug=True)
    # app.run(host='0.0.0.0', port=5000)
    max_request_body_size = 1 * 1024 * 1024 * 1024  # 1GB upload limit
    serve(app, host=router_host, port=router_port, max_request_body_size=max_request_body_size)

############################----------------------------------------------###############################