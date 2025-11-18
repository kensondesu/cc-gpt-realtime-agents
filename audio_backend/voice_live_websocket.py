"""Azure Voice Live API WebSocket client.

This module provides WebSocket-based conversational AI using Azure's Voice Live API.
Voice Live API is an enhanced version of OpenAI Realtime API with Azure-specific features.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional, Dict, Any, Callable, Awaitable
import asyncio
import websockets
from websockets.client import WebSocketClientProtocol

logger = logging.getLogger(__name__)


class VoiceLiveWebSocketClient:
    """WebSocket client for Azure Voice Live API conversational AI"""
    
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str = "gpt-4o-realtime-preview",
        voice: str = "en-US-Ava:DragonHDLatestNeural",
        temperature: float = 0.8,
        rate: str = "1.0",
    ):
        """
        Initialize Voice Live WebSocket client.
        
        Args:
            endpoint: WebSocket endpoint (e.g., wss://resource.services.ai.azure.com/voice-live/realtime?api-version=2025-10-01)
            api_key: API key for authentication
            model: Model to use (e.g., gpt-4o-realtime-preview, gpt-realtime)
            voice: Azure voice name (e.g., en-US-Ava:DragonHDLatestNeural)
            temperature: Voice temperature for HD voices (0.0-1.0)
            rate: Speaking rate (0.5-1.5)
        """
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.temperature = temperature
        self.rate = rate
        self.websocket: Optional[WebSocketClientProtocol] = None
        self.is_connected = False
        
    async def connect(self) -> bool:
        """
        Establish WebSocket connection to Voice Live API.
        
        Returns:
            True if connection successful, False otherwise
        """
        try:
            # Add model parameter to endpoint
            connection_url = f"{self.endpoint}&model={self.model}"
            
            # Connect with API key authentication
            headers = {
                "api-key": self.api_key
            }
            
            logger.info(f"Connecting to Voice Live API: {connection_url}")
            self.websocket = await websockets.connect(
                connection_url,
                extra_headers=headers,
                max_size=10 * 1024 * 1024,  # 10MB max message size
                ping_interval=20,
                ping_timeout=10,
            )
            
            self.is_connected = True
            logger.info("Voice Live WebSocket connection established")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect to Voice Live API: {e}")
            self.is_connected = False
            return False
    
    async def send_session_update(
        self,
        instructions: str,
        tools: Optional[list] = None,
        tool_choice: str = "auto",
        input_audio_format: str = "pcm16",
        output_audio_format: str = "pcm16",
        turn_detection_type: str = "azure_semantic_vad",
    ) -> bool:
        """
        Send session configuration update.
        
        Args:
            instructions: System instructions for the AI
            tools: List of function tools available to the AI
            tool_choice: Tool selection mode ("auto", "none", "required")
            input_audio_format: Format for input audio
            output_audio_format: Format for output audio
            turn_detection_type: VAD type (azure_semantic_vad, azure_semantic_vad_multilingual, server_vad)
            
        Returns:
            True if sent successfully, False otherwise
        """
        if not self.is_connected or not self.websocket:
            logger.error("Cannot send session update: not connected")
            return False
        
        try:
            session_config = {
                "type": "session.update",
                "session": {
                    "instructions": instructions,
                    "input_audio_format": input_audio_format,
                    "output_audio_format": output_audio_format,
                    "input_audio_transcription": {
                        "model": "azure-speech",  # Use Azure Speech for transcription
                    },
                    "turn_detection": {
                        "type": turn_detection_type,
                        "threshold": 0.3,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500,
                        "remove_filler_words": True,
                        "end_of_utterance_detection": {
                            "model": "semantic_detection_v1_multilingual",
                            "threshold_level": "default",
                            "timeout_ms": 1000
                        }
                    },
                    "voice": {
                        "name": self.voice,
                        "type": "azure-standard",
                        "temperature": self.temperature,
                        "rate": self.rate
                    },
                    "input_audio_noise_reduction": {
                        "type": "azure_deep_noise_suppression"
                    },
                    "input_audio_echo_cancellation": {
                        "type": "server_echo_cancellation"
                    },
                    "modalities": ["text", "audio"],
                }
            }
            
            # Add tools if provided
            if tools:
                session_config["session"]["tools"] = tools
                session_config["session"]["tool_choice"] = tool_choice
            
            await self.websocket.send(json.dumps(session_config))
            logger.info(f"Sent session.update with voice {self.voice}, turn detection {turn_detection_type}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send session update: {e}")
            return False
    
    async def send_audio(self, audio_data: bytes) -> bool:
        """
        Send audio data to the API.
        
        Args:
            audio_data: PCM16 audio bytes
            
        Returns:
            True if sent successfully, False otherwise
        """
        if not self.is_connected or not self.websocket:
            logger.error("Cannot send audio: not connected")
            return False
        
        try:
            # Audio is sent as binary WebSocket frames
            await self.websocket.send(audio_data)
            return True
        except Exception as e:
            logger.error(f"Failed to send audio: {e}")
            return False
    
    async def send_text(self, text: str) -> bool:
        """
        Send text message to the API.
        
        Args:
            text: Text content to send
            
        Returns:
            True if sent successfully, False otherwise
        """
        if not self.is_connected or not self.websocket:
            logger.error("Cannot send text: not connected")
            return False
        
        try:
            message = {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": text
                        }
                    ]
                }
            }
            await self.websocket.send(json.dumps(message))
            
            # Trigger response generation
            await self.websocket.send(json.dumps({"type": "response.create"}))
            return True
            
        except Exception as e:
            logger.error(f"Failed to send text: {e}")
            return False
    
    async def receive_events(
        self,
        callback: Callable[[Dict[str, Any]], Awaitable[None]]
    ) -> None:
        """
        Receive and process events from the API.
        
        Args:
            callback: Async function to handle received events
        """
        if not self.is_connected or not self.websocket:
            logger.error("Cannot receive events: not connected")
            return
        
        try:
            async for message in self.websocket:
                if isinstance(message, bytes):
                    # Binary audio data
                    await callback({
                        "type": "audio.data",
                        "audio": message
                    })
                else:
                    # JSON event
                    try:
                        event = json.loads(message)
                        await callback(event)
                    except json.JSONDecodeError as e:
                        logger.error(f"Failed to parse JSON event: {e}")
                        
        except websockets.exceptions.ConnectionClosed:
            logger.info("Voice Live WebSocket connection closed")
            self.is_connected = False
        except Exception as e:
            logger.error(f"Error receiving events: {e}")
            self.is_connected = False
    
    async def send_function_call_output(
        self,
        call_id: str,
        output: Any
    ) -> bool:
        """
        Send function call result back to the API.
        
        Args:
            call_id: Function call ID from the API
            output: Function execution result
            
        Returns:
            True if sent successfully, False otherwise
        """
        if not self.is_connected or not self.websocket:
            logger.error("Cannot send function output: not connected")
            return False
        
        try:
            message = {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output)
                }
            }
            await self.websocket.send(json.dumps(message))
            
            # Trigger response generation
            await self.websocket.send(json.dumps({"type": "response.create"}))
            logger.info(f"Sent function call output for {call_id}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to send function output: {e}")
            return False
    
    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        if self.websocket:
            await self.websocket.close()
            self.is_connected = False
            logger.info("Voice Live WebSocket disconnected")
    
    @classmethod
    def from_env(cls) -> "VoiceLiveWebSocketClient":
        """
        Create client from environment variables.
        
        Expected variables:
            - VOICE_LIVE_WEBSOCKET_ENDPOINT
            - VOICE_LIVE_API_KEY
            - VOICE_LIVE_MODEL (optional)
            - VOICE_LIVE_VOICE (optional)
            - VOICE_LIVE_TEMPERATURE (optional)
            - VOICE_LIVE_RATE (optional)
        """
        endpoint = os.getenv("VOICE_LIVE_WEBSOCKET_ENDPOINT")
        api_key = os.getenv("VOICE_LIVE_API_KEY")
        
        if not endpoint or not api_key:
            raise ValueError("VOICE_LIVE_WEBSOCKET_ENDPOINT and VOICE_LIVE_API_KEY must be set")
        
        return cls(
            endpoint=endpoint,
            api_key=api_key,
            model=os.getenv("VOICE_LIVE_MODEL", "gpt-4o-realtime-preview"),
            voice=os.getenv("VOICE_LIVE_VOICE", "en-US-AvaMultilingualNeural"),
            temperature=float(os.getenv("VOICE_LIVE_TEMPERATURE", "0.8")),
            rate=os.getenv("VOICE_LIVE_RATE", "1.0"),
        )
