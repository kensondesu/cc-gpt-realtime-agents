"""Azure Voice Live API client with SSML support.

This module provides functionality to interact with Azure's Voice Live API,
including SSML generation and text-to-speech conversion.
"""
from __future__ import annotations

import httpx
import logging
from typing import Optional, Dict, Any
from enum import Enum

logger = logging.getLogger(__name__)


class SSMLVoice(str, Enum):
    """Supported SSML voice for Azure TTS (enforced)"""
    AVA = "en-US-AvaMultilingualNeural"


class SSMLGenerator:
    """Generate SSML markup for Azure TTS"""
    
    @staticmethod
    def generate_ssml(
        text: str,
        voice: str = SSMLVoice.AVA,
        rate: str = "medium",
        pitch: str = "medium",
        language: str = "pt-PT"
    ) -> str:
        """
        Generate SSML markup for text-to-speech. Always uses en-US-AvaMultilingualNeural and pt-PT.
        """
        # Escape XML special characters
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")
        text = text.replace('"', "&quot;")
        text = text.replace("'", "&apos;")
        ssml = f'''<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="pt-PT">
  <voice name="en-US-AvaMultilingualNeural">
    <lang xml:lang="pt-PT">
      <mstts:express-as style="general">
        <prosody rate="0%" pitch="+0st">{text}</prosody>
      </mstts:express-as>
    </lang>
  </voice>
</speak>'''
        return ssml
    
    @staticmethod
    def add_emphasis(text: str, level: str = "moderate") -> str:
        """Add emphasis to text. Levels: strong, moderate, reduced"""
        return f'<emphasis level="{level}">{text}</emphasis>'
    
    @staticmethod
    def add_break(duration_ms: int = 500) -> str:
        """Add a pause/break in speech"""
        return f'<break time="{duration_ms}ms"/>'
    
    @staticmethod
    def add_phoneme(text: str, phoneme: str, alphabet: str = "ipa") -> str:
        """Add phonetic pronunciation"""
        return f'<phoneme alphabet="{alphabet}" ph="{phoneme}">{text}</phoneme>'


class VoiceLiveClient:
    """Client for Azure Voice Live API"""
    
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        deployment: str = "tts-model",
        default_voice: str = "en-US-AvaMultilingualNeural"
    ):
        """
        Initialize Voice Live API client.
        
        Args:
            endpoint: Azure Voice Live API endpoint
            api_key: API key for authentication
            deployment: Deployment name
            default_voice: Default voice to use
        """
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.deployment = deployment
        self.default_voice = default_voice
        self.ssml_generator = SSMLGenerator()
    
    async def synthesize_speech(
        self,
        text: str,
        voice: Optional[str] = None,
        use_ssml: bool = True,
        rate: str = "medium",
        pitch: str = "medium",
        language: str = "pt-PT"
    ) -> bytes:
        """
        Convert text to speech using Voice Live API.
        
        Args:
            text: Text to synthesize
            voice: Voice name (uses default if not specified)
            use_ssml: Whether to wrap text in SSML
            rate: Speech rate
            pitch: Speech pitch
            language: Language code
            
        Returns:
            Audio data as bytes
        """
        voice = voice or self.default_voice
        
        # Generate SSML if requested
        if use_ssml and not text.strip().startswith("<speak"):
            ssml_text = self.ssml_generator.generate_ssml(
                text, voice, rate, pitch, language
            )
        else:
            ssml_text = text
        
        url = f"{self.endpoint}/cognitiveservices/v1"
        
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": "audio-24khz-96kbitrate-mono-mp3",
            "User-Agent": "GPT-Realtime-Agents"
        }
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, headers=headers, content=ssml_text)
                response.raise_for_status()
                return response.content
        except httpx.HTTPError as exc:
            logger.error(f"Voice Live API error: {exc}")
            raise
    
    async def get_available_voices(self, language: Optional[str] = None) -> list[Dict[str, Any]]:
        """
        Get list of available voices.
        
        Args:
            language: Filter by language code (e.g., pt-PT)
            
        Returns:
            List of voice metadata
        """
        url = f"{self.endpoint}/cognitiveservices/voices/list"
        
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key
        }
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                voices = response.json()
                
                if language:
                    voices = [v for v in voices if v.get("Locale", "").startswith(language)]
                
                return voices
        except httpx.HTTPError as exc:
            logger.error(f"Failed to get voices: {exc}")
            return []
