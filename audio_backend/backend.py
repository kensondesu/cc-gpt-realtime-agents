"""FastAPI backend for Azure OpenAI Realtime function calling demos.

This service exposes two responsibilities:
- issue short-lived ephemeral keys for WebRTC sessions with Azure OpenAI Realtime.
- execute function-calling callbacks (currently a horoscope generator) on behalf of the browser client.

The design keeps the function registry generic so new tools can be added in a single place
without touching the frontend. Each tool definition mirrors the OpenAI Realtime schema.
"""
from __future__ import annotations


import os
import json
import logging
import sys
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

try:
    from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
    from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK
except ModuleNotFoundError as exc:  # pragma: no cover - module provided via dependencies
    raise RuntimeError(
        "azure-identity must be installed to run the backend service"
    ) from exc

from dotenv import load_dotenv
import inspect
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.json import JSON as RichJSON
import websockets
import asyncio


sys.path.insert(0, str(Path(__file__).parent ))


from tools_registry import *
from voice_live_client import VoiceLiveClient, SSMLGenerator




load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Realtime Function Calling Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # demo purposes only; tighten for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple in-memory key-value store for Spark framework
spark_kv_store: Dict[str, Any] = {}


def _clean_env(name: str, default: str | None = None) -> str:
    raw = os.getenv(name, default)
    if raw is None:
        raise RuntimeError(f"Environment variable {name} must be set")
    return raw.strip().strip('"').strip("'")


def _optional_env(name: str, default: str) -> str:
    raw = os.getenv(name, default)
    if not raw:
        return default
    return raw.strip().strip('"').strip("'")


# API Mode selection
API_MODE = _optional_env("API_MODE", "realtime")  # "realtime" or "voicelive"

# GPT Realtime API Configuration
REALTIME_SESSION_URL = _clean_env("AZURE_GPT_REALTIME_URL")
WEBRTC_URL = _clean_env("WEBRTC_URL")
DEFAULT_DEPLOYMENT = os.getenv("AZURE_GPT_REALTIME_DEPLOYMENT", "gpt-realtime")
DEFAULT_VOICE = _clean_env("AZURE_GPT_REALTIME_VOICE", "alloy")
AZURE_API_KEY = os.getenv("AZURE_GPT_REALTIME_KEY").replace('"', '').replace("'", "")

# Voice Live API Configuration
VOICE_LIVE_ENDPOINT = _optional_env("VOICE_LIVE_WEBSOCKET_ENDPOINT", "")
VOICE_LIVE_API_KEY = _optional_env("VOICE_LIVE_API_KEY", "")
# Use gpt-4o (text-only) to force Azure Speech-to-Text input and Azure TTS output for proper pt-PT
VOICE_LIVE_MODEL = _optional_env("VOICE_LIVE_MODEL", "gpt-4o")
VOICE_LIVE_VOICE = _optional_env("VOICE_LIVE_VOICE", "pt-PT-RaquelNeural")

FRONTEND_DIST_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
FRONTEND_BACKEND_BASE_URL = _optional_env("VITE_BACKEND_BASE_URL", "http://localhost:8080/api")

print("API_MODE", API_MODE)
print("REALTIME_SESSION_URL", REALTIME_SESSION_URL)
print("WEBRTC_URL", WEBRTC_URL)
print("DEFAULT_DEPLOYMENT", DEFAULT_DEPLOYMENT)
print("DEFAULT_VOICE", DEFAULT_VOICE)
print("AZURE_API_KEY", AZURE_API_KEY is not None)
print("VOICE_LIVE_ENDPOINT", VOICE_LIVE_ENDPOINT)
print("VOICE_LIVE_CONFIGURED", bool(VOICE_LIVE_ENDPOINT and VOICE_LIVE_API_KEY))



credential = DefaultAzureCredential(exclude_interactive_browser_credential=False)
token_provider = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")

# Initialize Voice Live client if configured
voice_live_client: VoiceLiveClient | None = None
if VOICE_LIVE_ENDPOINT and VOICE_LIVE_API_KEY:
    voice_live_client = VoiceLiveClient(
        endpoint=VOICE_LIVE_ENDPOINT,
        api_key=VOICE_LIVE_API_KEY,
        deployment=VOICE_LIVE_MODEL,
        default_voice=VOICE_LIVE_VOICE
    )
    logger.info("✅ Voice Live API client initialized")
else:
    logger.info("⚠️  Voice Live API not configured")


class SessionRequest(BaseModel):
    deployment: str | None = Field(default=None, description="Azure OpenAI deployment name")
    voice: str | None = Field(default=None, description="Voice to request in the session")


class SessionResponse(BaseModel):
    session_id: str = Field(..., description="Azure OpenAI WebRTC session id")
    ephemeral_key: str = Field(..., description="Ephemeral client secret for WebRTC auth")
    webrtc_url: str = Field(..., description="Regional WebRTC entry point")
    deployment: str = Field(..., description="Deployment used when requesting the session")
    voice: str = Field(..., description="Voice registered with the session")


class FunctionCallRequest(BaseModel):
    name: str = Field(..., description="Function/tool name requested by the model")
    call_id: str = Field(..., description="Unique call id supplied by Azure Realtime")
    arguments: Dict[str, Any] | str = Field(
        default_factory=dict,
        description="Arguments supplied by the model; may be JSON string or dict",
    )


class FunctionCallResponse(BaseModel):
    call_id: str
    output: Dict[str, Any]


class ApiModeRequest(BaseModel):
    mode: str = Field(..., description="API mode: 'realtime' or 'voicelive'")


ToolExecutor = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]] | Dict[str, Any]]


async def _get_auth_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if AZURE_API_KEY:
        headers["api-key"] = AZURE_API_KEY
        return headers

    # Prefer managed identity / Azure AD tokens when available
    token = await token_provider()
    headers["Authorization"] = f"Bearer {token}"
    return headers


def _parse_arguments(arguments: Dict[str, Any] | str) -> Dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        return json.loads(arguments)
    except json.JSONDecodeError as exc:  # pragma: no cover - invalid payloads are rare
        raise HTTPException(status_code=400, detail=f"Unable to parse arguments JSON: {exc}")



@app.get("/api/mode")
async def get_api_mode() -> Dict[str, Any]:
    """Get current API mode configuration."""
    return {
        "mode": API_MODE,
        "realtime_available": bool(REALTIME_SESSION_URL and AZURE_API_KEY),
        "voicelive_available": bool(voice_live_client),
        "voice": DEFAULT_VOICE if API_MODE == "realtime" else VOICE_LIVE_VOICE,
    }


@app.post("/api/mode")
async def set_api_mode(request: ApiModeRequest) -> Dict[str, Any]:
    """Change API mode dynamically."""
    global API_MODE
    
    mode = request.mode
    if mode not in ["realtime", "voicelive"]:
        raise HTTPException(status_code=400, detail="Mode must be 'realtime' or 'voicelive'")
    
    if mode == "voicelive" and not voice_live_client:
        raise HTTPException(status_code=400, detail="Voice Live API not configured")
    
    if mode == "realtime" and not (REALTIME_SESSION_URL and AZURE_API_KEY):
        raise HTTPException(status_code=400, detail="GPT Realtime API not configured")
    
    API_MODE = mode
    logger.info(f"API mode changed to: {mode}")
    
    return {
        "mode": API_MODE,
        "realtime_available": bool(REALTIME_SESSION_URL and AZURE_API_KEY),
        "voicelive_available": bool(voice_live_client),
        "voice": DEFAULT_VOICE if API_MODE == "realtime" else VOICE_LIVE_VOICE,
    }


# ============================================================================
# Spark Framework Key-Value Storage Endpoints
# ============================================================================

@app.post("/_spark/loaded")
async def spark_loaded() -> Dict[str, str]:
    """Handle Spark framework loaded event."""
    return {"status": "ok"}


@app.get("/_spark/kv/{key}")
async def spark_get_kv(key: str) -> Any:
    """Get value from Spark key-value store."""
    if key not in spark_kv_store:
        raise HTTPException(status_code=404, detail=f"Key '{key}' not found")
    # Return the value directly, not wrapped
    return spark_kv_store[key]


@app.post("/_spark/kv/{key}")
async def spark_set_kv(key: str, request: Request) -> Dict[str, str]:
    """Set value in Spark key-value store."""
    # Parse the request body as JSON
    try:
        body = await request.json()
        # Spark typically sends the value directly or wrapped in {"value": ...}
        if isinstance(body, dict) and "value" in body:
            value = body["value"]
        else:
            value = body
    except:
        # If parsing fails, just store None
        value = None
    
    spark_kv_store[key] = value
    return {"status": "ok", "key": key}


@app.delete("/_spark/kv/{key}")
async def spark_delete_kv(key: str) -> Dict[str, str]:
    """Delete key from Spark key-value store."""
    if key in spark_kv_store:
        del spark_kv_store[key]
    return {"status": "ok"}


# ============================================================================
# Voice Live API Endpoints
# ============================================================================

@app.get("/api/voicelive/config")
async def get_voicelive_config() -> Dict[str, Any]:
    """Get Voice Live WebSocket configuration for frontend."""
    if not VOICE_LIVE_ENDPOINT or not VOICE_LIVE_API_KEY:
        raise HTTPException(status_code=503, detail="Voice Live API not configured")
    
    # Return backend proxy URL (without /api prefix since frontend adds it)
    # Use gpt-4o (text-only) instead of gpt-4o-realtime-preview to force Azure TTS for proper pt-PT
    model = os.getenv("VOICE_LIVE_MODEL", "gpt-4o")
    
    # Use Azure HD voice for better SSML support with pt-PT language
    voice_env = os.getenv("VOICE_LIVE_VOICE", "en-US-Ava:DragonHDLatestNeural")
    return {
        "endpoint": "/voicelive/ws",  # Frontend will prepend /api via backendBaseUrl
        "model": model,
        "voice": voice_env,
        "language": "pt-PT",
        "temperature": float(os.getenv("VOICE_LIVE_TEMPERATURE", "0.8")),
        "rate": os.getenv("VOICE_LIVE_RATE", "1.0"),
    }


@app.websocket("/api/voicelive/ws")
async def voicelive_websocket_proxy(websocket: WebSocket):
    """WebSocket proxy that authenticates with Azure Voice Live API."""
    await websocket.accept()
    
    if not VOICE_LIVE_ENDPOINT or not VOICE_LIVE_API_KEY:
        await websocket.close(code=1008, reason="Voice Live API not configured")
        return
    
    # Construct Azure WebSocket URL with authentication
    # Use gpt-4o (text-only) to force Azure TTS for proper pt-PT pronunciation
    model = os.getenv("VOICE_LIVE_MODEL", "gpt-4o")
    azure_ws_url = f"{VOICE_LIVE_ENDPOINT}&model={model}"
    
    # Add API key as header
    headers = {
        "api-key": VOICE_LIVE_API_KEY,
        "Content-Type": "application/json"
    }
    
    logger.info(f"Connecting to Azure Voice Live API: {azure_ws_url}")
    
    try:
        # Connect to Azure Voice Live API
        async with websockets.connect(azure_ws_url, additional_headers=headers) as azure_ws:
            logger.info("Connected to Azure Voice Live API")
            
            # Create bidirectional relay
            async def relay_to_azure():
                """Relay messages from frontend to Azure."""
                try:
                    while True:
                        # Receive either text or binary data
                        message = await websocket.receive()
                        
                        if "text" in message:
                            data = message["text"]
                            logger.info(f"Frontend -> Azure (text): {data[:200]}...")
                            await azure_ws.send(data)
                        elif "bytes" in message:
                            # For binary audio data, we need to wrap it in input_audio_buffer.append event
                            audio_data = message["bytes"]
                            # Base64 encode the PCM16 audio
                            import base64
                            audio_base64 = base64.b64encode(audio_data).decode('utf-8')
                            audio_event = {
                                "type": "input_audio_buffer.append",
                                "audio": audio_base64
                            }
                            # logger.info(f"Frontend -> Azure (audio event): {len(audio_data)} bytes")
                            await azure_ws.send(json.dumps(audio_event))
                except WebSocketDisconnect:
                    logger.info("Frontend disconnected")
                except (ConnectionClosedError, ConnectionClosedOK) as e:
                    logger.info(f"Azure WebSocket closed: {e}")
                except Exception as e:
                    logger.error(f"Error relaying to Azure: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            
            async def relay_to_frontend():
                """Relay messages from Azure to frontend."""
                try:
                    async for message in azure_ws:
                        if isinstance(message, bytes):
                            # Binary audio from Azure - send directly to frontend
                            logger.info(f"Azure -> Frontend (binary): {len(message)} bytes")
                            await websocket.send_bytes(message)
                        else:
                            # Text message from Azure
                            logger.info(f"Azure -> Frontend (text): {str(message)[:200]}...")
                            # Check if it's an audio response that needs extraction
                            try:
                                msg_json = json.loads(message)
                                msg_type = msg_json.get("type", "")
                                
                                # Handle both legacy and new audio delta event names
                                if msg_type in ("response.audio.delta", "response.output_audio.delta"):
                                    # Some APIs send base64 in 'delta', others in 'audio'
                                    audio_base64 = msg_json.get("delta") or msg_json.get("audio", "")
                                    if audio_base64:
                                        import base64
                                        audio_bytes = base64.b64decode(audio_base64)
                                        logger.info(f"Extracted {len(audio_bytes)} bytes from {msg_type}")
                                        await websocket.send_bytes(audio_bytes)
                                    # Strip audio payload from JSON to avoid forwarding huge blobs
                                    msg_json["delta"] = ""
                                    msg_json["audio"] = ""
                                    await websocket.send_text(json.dumps(msg_json))
                                else:
                                    # For non-audio messages, send as-is
                                    await websocket.send_text(message)
                            except json.JSONDecodeError:
                                # Not JSON, send as-is
                                await websocket.send_text(message)
                except (ConnectionClosedError, ConnectionClosedOK) as e:
                    logger.info(f"Azure WebSocket closed: {e}")
                except Exception as e:
                    logger.error(f"Error relaying to frontend: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
            
            # Run both relay tasks concurrently
            await asyncio.gather(
                relay_to_azure(),
                relay_to_frontend(),
                return_exceptions=True
            )
    
    except Exception as e:
        logger.error(f"Voice Live WebSocket proxy error: {e}")
        await websocket.close(code=1011, reason=str(e))



@app.get("/api/voicelive/voices")
async def list_voicelive_voices(language: str | None = None) -> Dict[str, Any]:
    """Get available Voice Live API voices."""
    if not voice_live_client:
        raise HTTPException(status_code=503, detail="Voice Live API not configured")
    
    voices = await voice_live_client.get_available_voices(language)
    return {"voices": voices}


@app.post("/api/voicelive/synthesize")
async def synthesize_speech(
    text: str,
    voice: str | None = None,
    use_ssml: bool = True,
    rate: str = "medium",
    pitch: str = "medium",
    language: str = "pt-PT"
) -> bytes:
    """Synthesize speech using Voice Live API."""
    if not voice_live_client:
        raise HTTPException(status_code=503, detail="Voice Live API not configured")
    
    try:
        audio_data = await voice_live_client.synthesize_speech(
            text=text,
            voice=voice,
            use_ssml=use_ssml,
            rate=rate,
            pitch=pitch,
            language=language
        )
        return audio_data
    except Exception as exc:
        logger.exception("Failed to synthesize speech")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/tools")
async def list_tools() -> Dict[str, Any]:
    """Return tool definitions for the frontend to register with the realtime session."""
    return {
        "tools": [tool["definition"] for tool in TOOLS_REGISTRY.values()],
        "tool_choice": "auto",
    }


@app.post("/api/session", response_model=SessionResponse)
async def create_session(request: SessionRequest) -> SessionResponse:
    """Issue an ephemeral key suitable for establishing a WebRTC session."""
    deployment = request.deployment or DEFAULT_DEPLOYMENT
    voice = request.voice or DEFAULT_VOICE

    payload = {"model": deployment, "voice": voice}
    headers = await _get_auth_headers()

    print("Creating realtime session with payload:")
    print("===================================")
    print("REALTIME_SESSION_URL:", REALTIME_SESSION_URL)
    print("HEADERS:", headers)
    print("PAYLOAD:", payload)
    
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(REALTIME_SESSION_URL, headers=headers, json=payload)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:  # pragma: no cover - network specific
            logger.exception("Failed to create realtime session: %s", exc)
            raise HTTPException(status_code=exc.response.status_code, detail=exc.response.text)

    data = response.json()
    ephemeral_key = data.get("client_secret", {}).get("value")
    session_id = data.get("id")
    if not ephemeral_key or not session_id:
        raise HTTPException(status_code=500, detail="Malformed session response from Azure")

    return SessionResponse(
        session_id=session_id,
        ephemeral_key=ephemeral_key,
        webrtc_url=WEBRTC_URL,
        deployment=deployment,
        voice=voice,
    )


@app.post("/api/function-call", response_model=FunctionCallResponse)
async def execute_function(request: FunctionCallRequest) -> FunctionCallResponse:
    """Execute a tool requested by the model, return its structured output, and
    display a rich debug pane (if 'rich' is installed) with name, arguments, and result.
    """
    tool = TOOLS_REGISTRY.get(request.name)
    if not tool:
        raise HTTPException(status_code=404, detail=f"Unknown function '{request.name}'")

    arguments = _parse_arguments(request.arguments)
    executor: ToolExecutor = tool["executor"]

    result = executor(arguments)
    if inspect.isawaitable(result):
        result = await result

    if not isinstance(result, dict):
        raise HTTPException(status_code=500, detail="Function executor must return a dict")

    # Rich debug output (best-effort; falls back silently if rich not available)
    try:

        console = Console()

        table = Table.grid(padding=(0, 1))
        table.add_column(justify="right", style="bold cyan")
        table.add_column(style="white")

        table.add_row("Function:", request.name)
        table.add_row("Call ID:", request.call_id)

        # Arguments block
        try:
            args_json = RichJSON.from_data(arguments)
        except Exception:
            args_json = str(arguments)

        # Result block
        try:
            result_json = RichJSON.from_data(result)
        except Exception:
            result_json = str(result)

        console.print(
            Panel.fit(
                table,
                title="Function Call",
                border_style="magenta",
            )
        )
        console.print(Panel(args_json, title="Arguments", border_style="cyan"))
        console.print(Panel(result_json, title="Result", border_style="green"))
    except Exception as e:
        # Swallow any rich / rendering errors to avoid impacting API behavior
        console.print(f"Exception: {e}")

    return FunctionCallResponse(call_id=request.call_id, output=result)


@app.get("/healthz")
async def healthcheck() -> Dict[str, str]:
    return {"status": "ok"}


@app.get("/runtime-config.js", response_class=PlainTextResponse)
async def runtime_config() -> PlainTextResponse:
    payload = json.dumps({"backendBaseUrl": FRONTEND_BACKEND_BASE_URL})
    script = f"window.__APP_CONFIG__ = Object.freeze({payload});"
    return PlainTextResponse(content=script, media_type="application/javascript")


@app.on_event("shutdown")
async def shutdown_event() -> None:
    await credential.close()


# ============================================================================
# ACS Phone Integration (WebSocket only: Phone ↔ ACS ↔ AI Model)
# ============================================================================
try:
    from backend_acs import router as acs_router, startup_event as acs_startup
    app.include_router(acs_router)
    app.on_event("startup")(acs_startup)
    logger.info("✅ ACS Phone integration routes mounted at /acs-phone/*")
except ImportError as e:
    logger.warning("⚠️  ACS Phone integration not available: %s", e)


if FRONTEND_DIST_DIR.exists():
    # Note: StaticFiles mounted at "/" must come AFTER all API route decorators
    # However, it can still catch WebSocket connections in some Starlette versions
    # To fix this, we mount static files more carefully
    from starlette.routing import Mount, Route
    from starlette.responses import FileResponse
    
    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        """Serve frontend files, but let API routes take precedence."""
        # This route has lower priority than explicit routes above
        if full_path == "" or full_path == "index.html":
            return FileResponse(FRONTEND_DIST_DIR / "index.html")
        
        file_path = FRONTEND_DIST_DIR / full_path
        if file_path.is_file():
            return FileResponse(file_path)
        
        # For client-side routing, serve index.html for unknown paths
        return FileResponse(FRONTEND_DIST_DIR / "index.html")
else:
    logger.warning("Frontend build directory not found at %s; React app will not be served.", FRONTEND_DIST_DIR)
