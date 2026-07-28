from copy import deepcopy   # creates a new copy in memory, not a reference to the original
from pathlib import Path
import tempfile
import json
import os


def create_config_file(config_path: Path = Path("config.json")) -> dict:
    try:
        with open(config_path, "w") as f:
            json.dump({}, f, indent=4)
        return {'success': True}
    except Exception as e:
        error_message = f"Error creating config file at {config_path}\n: {e}"
        return {'success': False, 'error': error_message}


def create_config_dict(base_settings, **overrides) -> dict:
    config = deepcopy(base_settings)
    config.update(overrides)
    return config


def get_provider_base_settings() -> dict:
    return {
        "name": "",
        "vendor": "",
        "description": "",
        "authType": None,   # None, "bearerToken"
        "bearerToken": "",
        "apiType": "chat-completions",
        "enabled": False,
        "models": [
            {
                "id": "",
                "name": "",
                "url": "",
                "toolCalling": False,
                "vision": False,
                "maxInputTokens": 128000,
                "maxOutputTokens": 16000
            }
        ]
    }


def get_router_base_settings() -> dict:
    '''
    Available routing strategies:
        - token_threshold   (specify provider and threshold)
        - llm_classifier    (specify provider)
    '''
    return {
        "router_serving_address": "0.0.0.0",
        "router_access_address": "localhost",
        "router_serving_port": 8765,
        "routing_strategy": "token_threshold",
        "llm_classifier_provider_name": "",
        "llm_classifier_model_id": "",
        "token_thresholds": [
            { "maxInputTokens": 10000, "provider_name": "", "model_id": "" },
            { "maxInputTokens": 64000, "provider_name": "", "model_id": "" },
            { "maxInputTokens": 128000, "provider_name": "", "model_id": "" },
            { "maxInputTokens": 262144, "provider_name": "", "model_id": "" },
        ]
    }


def read_config(
    keys: list[str],
    config_path: Path = Path("config.json")
) -> dict:

    result = {}
    updated_config = {}
    
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except FileNotFoundError:
        create_config_file(config_path)
        config = {}
    except Exception as e:
        raise RuntimeError(f"Error reading config file: {e}") from e

    for key in keys:
        if key in config:
            result[key] = config[key]
        else:
            default_config = {
                "router": get_router_base_settings(),
                "providers": [
                    get_provider_base_settings(),
                ]
            }.get(key, None)
            
            result[key] = default_config
            updated_config[key] = default_config

    if updated_config: safe_write_config(updated_config, config_path)

    return result


def deep_merge_config(existing: dict, updates: dict) -> dict:
    merged = deepcopy(existing)

    for key, value in updates.items():
        if key in merged and type(merged[key]) is not type(value):
            raise ValueError(
                f"Config type mismatch for {key}: "
                f"expected {type(merged[key]).__name__}, got {type(value).__name__}"
            )
        
        if (
            key in merged
            and isinstance(merged[key], dict)
            and isinstance(value, dict)
        ):
            merged[key] = deep_merge_config(merged[key], value)  # recursive merge of dicts
        else:
            # Lists and scalars replace the existing value
            merged[key] = deepcopy(value)

    return merged


def write_config(
    config_updates:dict,
    config_path: Path = Path("config.json")
) -> dict:
    try:
        try:
            with open(config_path, "r") as f:
                existing_config = json.load(f)
        except FileNotFoundError:
            existing_config = {}

        updated_config = deep_merge_config(existing_config, config_updates)

        config_path = Path(config_path) #safety incase a string is passed in
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=config_path.parent or ".",
        ) as tmp:
            json.dump(updated_config, tmp, indent=4)
            tmp.write("\n") # optional style nicety
            temp_path = tmp.name    # path to the temporary file

        os.replace(temp_path, config_path)

        return {'success': True}
    
    except Exception as e:
        raise RuntimeError(f"Error writing to config file at {config_path}: {e}") from e


def safe_write_config(
    config_updates:dict,
    config_path: Path = Path("config.json")
) -> dict:
    '''
    Wrapper for write-config() that handles errors without halting.
    Directly invoke write-config() instead of this method anytime 
    a write-specific error must be raised.
    '''
    try:
        return write_config(config_updates, config_path)
    except Exception as e:
        print("Could not write to config.json, encountered error: ", e)
        return {'success': False, 'error': str(e)}