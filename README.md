# LLM Request Router

A small OpenAI-compatible request router for relaying chat completion requests to one of several configured LLM providers.

The router is intended to sit between an OpenAI-compatible client and one or more local, workstation, or cloud LLM backends. Clients send requests to the router's `/v1/chat/completions` endpoint, and the router selects a provider/model, rewrites the provider-facing model ID, forwards the request, and relays the response back to the client.

This project is currently an internal MVP / capability demonstrator.

## Features

- OpenAI-compatible `/v1/chat/completions` endpoint.
- Non-streaming and streaming response relay.
- Provider authentication via bearer token or unauthenticated local providers.
- Runtime config persisted in `config.json`.
- Two routing strategies:
  - `token_threshold`: route by estimated request token count.
  - `llm_classifier`: use a configured LLM provider to choose the target provider/model.

## Requirements

- Python 3.10+
- Dependencies listed in `requirements.txt`

Install dependencies:

```bash
pip install -r requirements.txt
```

## Running

Start the router:

```bash
python router.py
```

By default, the router serves on:

```text
0.0.0.0:8765
```

The default access address stored in config is:

```text
localhost:8765
```

You can override launch settings:

```bash
python router.py --router_serving_address 0.0.0.0 --router_serving_port 8765 --routing_strategy token_threshold
```

Reset generated config back to defaults:

```bash
python router.py --reset_to_defaults
```

## Client Usage

Point an OpenAI-compatible client at the router instead of a provider:

```text
http://localhost:8765/v1/chat/completions
```

The client can use a router-facing model name. The router will select the actual provider/model and replace the outbound provider-facing `model` value before forwarding.

Example request:

```bash
curl http://localhost:8765/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "router",
    "messages": [
      { "role": "user", "content": "Hello!" }
    ],
    "stream": false
  }'
```

Streaming requests are also relayed:

```json
{
  "model": "router",
  "messages": [
    { "role": "user", "content": "Write a short poem." }
  ],
  "stream": true
}
```

## Configuration

The router stores configuration in `config.json`, which is generated automatically on first run. This file is ignored by git.

Top-level config keys:

- `router`: router serving and strategy settings.
- `providers`: configured LLM providers.

Provider shape:

```json
{
  "name": "local-small",
  "vendor": "local",
  "description": "Small local model for short requests",
  "authType": "none",
  "bearerToken": "",
  "apiType": "chat-completions",
  "enabled": true,
  "models": [
    {
      "id": "local-model-id",
      "name": "Local Small",
      "url": "http://localhost:11434/v1/chat/completions",
      "toolCalling": false,
      "vision": false,
      "maxInputTokens": 10000,
      "maxOutputTokens": 16000
    }
  ]
}
```

Bearer-token provider example:

```json
{
  "name": "cloud-provider",
  "vendor": "openai-compatible",
  "description": "Cloud fallback provider",
  "authType": "bearerToken",
  "bearerToken": "YOUR_PROVIDER_TOKEN",
  "apiType": "chat-completions",
  "enabled": true,
  "models": [
    {
      "id": "provider-model-id",
      "name": "Provider Model",
      "url": "https://provider.example.com/v1/chat/completions",
      "toolCalling": true,
      "vision": false,
      "maxInputTokens": 128000,
      "maxOutputTokens": 16000
    }
  ]
}
```

## Routing Strategies

### Token Threshold

`token_threshold` selects the smallest configured threshold that can fit the estimated request size.

```json
{
  "router": {
    "routing_strategy": "token_threshold",
    "token_thresholds": [
      { "maxInputTokens": 10000, "provider_name": "local-small", "model_id": "local-model-id" },
      { "maxInputTokens": 128000, "provider_name": "cloud-provider", "model_id": "provider-model-id" }
    ]
  }
}
```

### LLM Classifier

`llm_classifier` sends a summarized routing request to a configured classifier provider. Available providers are represented as tool choices, and the classifier selects one provider/model pair.

```json
{
  "router": {
    "routing_strategy": "llm_classifier",
    "llm_classifier_provider_name": "cloud-provider",
    "llm_classifier_model_id": "provider-model-id"
  }
}
```

## Config API

The router includes simple config endpoints:

- `POST /read_config`
- `POST /write_config`

These are intended for local/internal MVP use.

Read example:

```bash
curl http://localhost:8765/read_config \
  -H "Content-Type: application/json" \
  -d '{ "keys": ["router", "providers"] }'
```

Write example:

```bash
curl http://localhost:8765/write_config \
  -H "Content-Type: application/json" \
  -d '{
    "config_updates": {
      "router": {
        "routing_strategy": "token_threshold"
      }
    }
  }'
```

## Notes

- `config.json` may contain bearer tokens and is intentionally ignored by git.
- The current token estimator is lightweight and approximate.
- The router is designed for OpenAI-compatible chat completions providers, but additional provider can be easily plugged in.
- Security hardening, authentication for config endpoints, and production deployment concerns are outside the current MVP scope.
