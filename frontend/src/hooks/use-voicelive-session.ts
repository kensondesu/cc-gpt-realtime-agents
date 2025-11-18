import { useState, useRef, useCallback } from 'react';
import { ChatMessage } from '@/lib/types';
import { CLIENT_CONFIG as CONFIG, SYSTEM_PROMPT } from '@/lib/constants';
import { wrapWithPortugueseSSML, isLikelyPortuguese } from '@/lib/utils';

interface SessionState {
  status: 'idle' | 'connecting' | 'connected' | 'ended';
  isMuted: boolean;
}

interface UseVoiceLiveSessionProps {
  onMessage: (message: ChatMessage) => void;
  onStateChange: (state: any) => void;
}

let cachedTools: any[] | null = null;
let toolChoice = "auto";

export function useVoiceLiveSession({ onMessage, onStateChange }: UseVoiceLiveSessionProps) {
  const [sessionState, setSessionState] = useState<SessionState>({
    status: 'idle',
    isMuted: false,
  });

  const sessionRef = useRef<{
    websocket: WebSocket | null;
    audioContext: AudioContext | null;
    audioWorklet: AudioWorkletNode | null;
    mediaStream: MediaStream | null;
    playbackContext: AudioContext | null;
    nextPlayTime: number;
    languageCorrectionAttempts?: number;
    pendingGreeting?: boolean;
  }>({
    websocket: null,
    audioContext: null,
    audioWorklet: null,
    mediaStream: null,
    playbackContext: null,
    nextPlayTime: 0,
    languageCorrectionAttempts: 0,
    pendingGreeting: false,
  });

  const logMessage = useCallback((message: string) => {
    console.log('[VoiceLive]', message);
  }, []);

  const ensureToolsLoaded = useCallback(async () => {
    if (cachedTools !== null) {
      return;
    }

    const response = await fetch(`${CONFIG.backendBaseUrl}/tools`);
    if (!response.ok) {
      throw new Error(`Unable to retrieve tool definitions (${response.status})`);
    }

    const data = await response.json();
    cachedTools = data.tools ?? [];
    toolChoice = data.tool_choice ?? "auto";

    logMessage(`Loaded ${cachedTools?.length || 0} tool definition(s)`);
  }, [logMessage]);

  const sendSessionUpdate = useCallback((ws: WebSocket, tools: any[], toolChoiceValue: string, voiceConfig?: any) => {
    // Use backend-provided voice (AvaMultilingual supports multiple languages)
    const voiceName = voiceConfig?.voice || "en-US-AvaMultilingualNeural";
    console.log("🎤 Voice Config:", { voiceConfig, voiceName });
    const ssmlInstructions = SYSTEM_PROMPT + `\n\n## CONFIGURAÇÃO DE VOZ OBRIGATÓRIA\nVOCÊ DEVE FALAR SEMPRE EM PORTUGUÊS EUROPEU (pt-PT), NUNCA em português brasileiro (pt-BR).\nUse vocabulário, gramática e pronúncia de Portugal.\nExemplos: "você" (PT-PT) vs "vocês" (PT-BR), "telemóvel" (PT-PT) vs "celular" (PT-BR).\n\n## RESPOSTA INICIAL OBRIGATÓRIA\nIMEDIATAMENTE ao iniciar a sessão, cumprimente o utilizador em Português Europeu (pt-PT). Use uma das frases de saudação fornecidas. NUNCA use inglês para a primeira resposta.`;
    
    // Set flag to send greeting after session is confirmed
    sessionRef.current.pendingGreeting = true;
    
    
    const sessionPayload = {
      type: "session.update",
      session: {
        instructions: ssmlInstructions,
        input_audio_format: "pcm16",
        output_audio_format: "pcm16",
        input_audio_transcription: {
          model: "azure-speech"
        },
        turn_detection: {
          type: "server_vad",
          threshold: 0.5,
          prefix_padding_ms: 300,
          silence_duration_ms: 700
        },
        voice: {
          name: voiceName,
          type: "azure-standard",
          language: "pt-PT",
          temperature: voiceConfig?.temperature || 0.8,
          rate: voiceConfig?.rate || "1.0"
        },
        modalities: ["text", "audio"]
      },
    };

    console.log("📤 Sending session.update with voice:", sessionPayload.session.voice);

    if (Array.isArray(tools) && tools.length > 0) {
      (sessionPayload.session as any).tools = tools;
      (sessionPayload.session as any).tool_choice = toolChoiceValue ?? "auto";
    }

    ws.send(JSON.stringify(sessionPayload));
    logMessage(`Sent session.update with ${tools.length} tools`);
  }, [logMessage]);

  const fulfillFunctionCall = useCallback(async (functionCallItem: any, ws: WebSocket) => {
    const callId = functionCallItem.call_id;
    const functionName = functionCallItem.name;
    const argumentsPayload = functionCallItem.arguments ?? {};

    logMessage(`Model requested function: ${functionName}`);

    // Add tool call message to UI
    onMessage({
      id: crypto.randomUUID(),
      role: 'tool_call',
      content: `Calling ${functionName}`,
      timestamp: Date.now(),
      toolName: functionName,
      toolArgs: argumentsPayload
    });

    try {
      const response = await fetch(`${CONFIG.backendBaseUrl}/function-call`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name: functionName,
          call_id: callId,
          arguments: argumentsPayload,
        }),
      });

      if (!response.ok) {
        const detail = await response.text();
        throw new Error(`Backend error (${response.status}): ${detail}`);
      }

      const result = await response.json();
      
      // Add tool result message to UI
      onMessage({
        id: crypto.randomUUID(),
        role: 'tool_result',
        content: JSON.stringify(result.output, null, 2),
        timestamp: Date.now(),
        toolName: functionName
      });

      sendFunctionCallOutput(ws, callId, result.output);
      logMessage(`Provided output for ${functionName}`);
    } catch (error: any) {
      console.error(`Function ${functionName} failed`, error);
      const errorPayload = { error: error.message };
      sendFunctionCallOutput(ws, callId, errorPayload);
      logMessage(`Function ${functionName} failed: ${error.message}`);
    }
  }, [logMessage, onMessage]);

  const sendFunctionCallOutput = useCallback((ws: WebSocket, callId: string, output: any) => {
    const conversationEvent = {
      type: "conversation.item.create",
      item: {
        type: "function_call_output",
        call_id: callId,
        output: JSON.stringify(output),
      },
    };

    ws.send(JSON.stringify(conversationEvent));
    ws.send(JSON.stringify({ type: "response.create" }));
    logMessage(`Sent function_call_output for call ${callId}`);
  }, [logMessage]);

  const handleResponseDone = useCallback(async (event: any, ws: WebSocket) => {
    const response = event.response;
    if (!response || !Array.isArray(response.output)) {
      return;
    }

    for (const item of response.output) {
      if (item.type === "function_call") {
        await fulfillFunctionCall(item, ws);
      }
    }
  }, [fulfillFunctionCall]);

  const initSession = useCallback(async () => {
    try {
      await ensureToolsLoaded();
      
      setSessionState((prev) => ({ ...prev, status: 'connecting' }));
      logMessage('Establishing Voice Live WebSocket connection...');

      // Get Voice Live endpoint from backend
      const configResponse = await fetch(`${CONFIG.backendBaseUrl}/voicelive/config`);
      if (!configResponse.ok) {
        throw new Error('Failed to get Voice Live configuration');
      }
      const config = await configResponse.json();
      
      // Create WebSocket connection to backend proxy
      // Convert endpoint to WebSocket URL (relative path -> ws:// or wss://)
      let wsUrl = config.endpoint;
      if (wsUrl.startsWith('/')) {
        // Convert relative path to absolute WebSocket URL
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const host = CONFIG.backendBaseUrl.replace(/^https?:\/\//, '');
        wsUrl = `${protocol}//${host}${wsUrl}`;
      }
      
      logMessage(`Connecting to: ${wsUrl}`);
      const ws = new WebSocket(wsUrl);
      ws.binaryType = 'arraybuffer';

      ws.onopen = () => {
        logMessage('Voice Live WebSocket connected');
        sessionRef.current.websocket = ws;
        
        // Send session update with voice config from backend
        sendSessionUpdate(ws, cachedTools || [], toolChoice, config);
        
        setSessionState((prev) => ({ ...prev, status: 'connected' }));
        onStateChange({ connected: true });
      };

      ws.onmessage = async (event) => {
        if (event.data instanceof ArrayBuffer) {
          // Binary audio data
          await playAudio(event.data);
        } else {
          // JSON event
          try {
            const messageData = JSON.parse(event.data);
            await handleServerEvent(messageData, ws);
          } catch (e) {
            console.error('Failed to parse server message:', e);
          }
        }
      };

      ws.onerror = (error) => {
        console.error('Voice Live WebSocket error:', error);
        logMessage('WebSocket error occurred');
      };

      ws.onclose = () => {
        logMessage('Voice Live WebSocket closed');
        setSessionState((prev) => ({ ...prev, status: 'ended' }));
        onStateChange({ connected: false });
        cleanupAudio();
      };

      // Setup audio capture
      await setupAudioCapture(ws);

    } catch (error: any) {
      console.error('Failed to initialize Voice Live session:', error);
      logMessage(`Session initialization failed: ${error.message}`);
      setSessionState((prev) => ({ ...prev, status: 'ended' }));
      throw error;
    }
  }, [ensureToolsLoaded, logMessage, onStateChange, sendSessionUpdate]);

  const setupAudioCapture = useCallback(async (ws: WebSocket) => {
    try {
      // Get microphone access
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          sampleRate: 24000,
          echoCancellation: true, // Enable echo cancellation to prevent feedback
          noiseSuppression: true, // Enable noise suppression
          autoGainControl: true,  // Enable automatic gain control
        }
      });

      sessionRef.current.mediaStream = stream;

      // Create AudioContext for processing
      const audioContext = new AudioContext({ sampleRate: 24000 });
      sessionRef.current.audioContext = audioContext;

      // Resume AudioContext if suspended (required by browsers)
      if (audioContext.state === 'suspended') {
        await audioContext.resume();
      }

      const source = audioContext.createMediaStreamSource(stream);
      
      // Create audio worklet for PCM16 conversion
      try {
        await audioContext.audioWorklet.addModule('/audio-processor.js');
        const workletNode = new AudioWorkletNode(audioContext, 'audio-processor');
        sessionRef.current.audioWorklet = workletNode;

        workletNode.port.onmessage = (event) => {
          // Send PCM16 audio to WebSocket
          if (ws.readyState === WebSocket.OPEN && !sessionState.isMuted) {
            ws.send(event.data);
          }
        };

        source.connect(workletNode);
        logMessage('Audio capture started with AudioWorklet');
      } catch (workletError) {
        // Fallback: use ScriptProcessorNode if AudioWorklet fails
        logMessage('AudioWorklet failed, using ScriptProcessor fallback');
        const bufferSize = 4096;
        const processor = audioContext.createScriptProcessor(bufferSize, 1, 1);
        
        processor.onaudioprocess = (e) => {
          if (ws.readyState === WebSocket.OPEN && !sessionState.isMuted) {
            const inputData = e.inputBuffer.getChannelData(0);
            const pcm16 = new Int16Array(inputData.length);
            
            for (let i = 0; i < inputData.length; i++) {
              const s = Math.max(-1, Math.min(1, inputData[i]));
              pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
            }
            
            ws.send(pcm16.buffer);
          }
        };
        
        source.connect(processor);
        // Don't connect to destination - we only need to read the audio, not play it back
        logMessage('Audio capture started with ScriptProcessor');
      }

    } catch (error) {
      console.error('Failed to setup audio capture:', error);
      throw error;
    }
  }, [sessionState.isMuted, logMessage]);

  const playAudio = useCallback(async (audioData: ArrayBuffer) => {
    try {
      // Use separate playback context to avoid conflicts with capture context
      if (!sessionRef.current.playbackContext) {
        sessionRef.current.playbackContext = new AudioContext({ sampleRate: 24000 });
        sessionRef.current.nextPlayTime = sessionRef.current.playbackContext.currentTime;
      }

      const audioContext = sessionRef.current.playbackContext;
      
      // Convert PCM16 to AudioBuffer
      const pcm16 = new Int16Array(audioData);
      const float32 = new Float32Array(pcm16.length);
      
      for (let i = 0; i < pcm16.length; i++) {
        float32[i] = pcm16[i] / 32768.0;
      }

      const audioBuffer = audioContext.createBuffer(1, float32.length, 24000);
      audioBuffer.getChannelData(0).set(float32);

      const source = audioContext.createBufferSource();
      source.buffer = audioBuffer;
      source.connect(audioContext.destination);
      
      // Schedule audio to play in sequence, not overlapping
      const now = audioContext.currentTime;
      const startTime = Math.max(now, sessionRef.current.nextPlayTime);
      source.start(startTime);
      
      // Update next play time to after this chunk finishes
      sessionRef.current.nextPlayTime = startTime + audioBuffer.duration;

    } catch (error) {
      console.error('Failed to play audio:', error);
    }
  }, []);

  const handleServerEvent = useCallback(async (event: any, ws: WebSocket) => {
    const eventType = event.type;

    switch (eventType) {
      case 'session.updated':
        logMessage('Session configuration confirmed');
        // Send greeting after session is configured
        if (sessionRef.current.pendingGreeting) {
          sessionRef.current.pendingGreeting = false;
          setTimeout(() => {
            const seed = {
              type: "conversation.item.create",
              item: {
                type: "message",
                role: "user",
                content: [
                  { type: "input_text", text: "Olá" }
                ]
              }
            };
            ws.send(JSON.stringify(seed));
            ws.send(JSON.stringify({ type: "response.create" }));
            logMessage("Sent Portuguese greeting seed");
          }, 500);
        }
        break;

      case 'session.created':
        logMessage(`Session ${event.session.id} created`);
        break;

      case 'input_audio_buffer.speech_started':
        onStateChange({ userSpeaking: true });
        break;

      case 'input_audio_buffer.speech_stopped':
        onStateChange({ userSpeaking: false });
        break;

      case 'conversation.item.input_audio_transcription.completed':
        if (event.transcript) {
          onMessage({
            id: crypto.randomUUID(),
            role: 'user',
            content: event.transcript,
            timestamp: Date.now(),
          });
        }
        break;

      case 'response.audio_transcript.delta':
        // Handle partial assistant transcript
        break;

      case 'response.audio_transcript.done':
        if (event.transcript) {
          const transcript: string = event.transcript.trim();
          const ssml = wrapWithPortugueseSSML(transcript);
          onMessage({
            id: event.item_id || crypto.randomUUID(),
            role: 'assistant',
            content: transcript,
            timestamp: Date.now(),
            ssml
          });
        }
        break;

      case 'response.done':
        await handleResponseDone(event, ws);
        break;

      case 'error':
        console.error('Voice Live API error:', event.error);
        logMessage(`Error: ${event.error?.message || 'Unknown error'}`);
        break;

      default:
        // Log other events for debugging
        if (eventType && !eventType.includes('.delta')) {
          logMessage(`Received: ${eventType}`);
        }
    }
  }, [logMessage, onMessage, onStateChange, handleResponseDone]);

  const cleanupAudio = useCallback(() => {
    if (sessionRef.current.audioWorklet) {
      sessionRef.current.audioWorklet.disconnect();
      sessionRef.current.audioWorklet = null;
    }

    if (sessionRef.current.mediaStream) {
      sessionRef.current.mediaStream.getTracks().forEach(track => track.stop());
      sessionRef.current.mediaStream = null;
    }

    if (sessionRef.current.audioContext) {
      sessionRef.current.audioContext.close();
      sessionRef.current.audioContext = null;
    }

    if (sessionRef.current.playbackContext) {
      sessionRef.current.playbackContext.close();
      sessionRef.current.playbackContext = null;
      sessionRef.current.nextPlayTime = 0;
    }
  }, []);

  const endSession = useCallback(() => {
    logMessage('Ending Voice Live session');
    
    if (sessionRef.current.websocket) {
      sessionRef.current.websocket.close();
      sessionRef.current.websocket = null;
    }

    cleanupAudio();
    
    setSessionState({
      status: 'ended',
      isMuted: false,
    });

    onStateChange({ connected: false });
  }, [logMessage, onStateChange, cleanupAudio]);

  const toggleMute = useCallback(() => {
    setSessionState((prev) => {
      const newMuted = !prev.isMuted;
      logMessage(newMuted ? 'Microphone muted' : 'Microphone unmuted');
      return { ...prev, isMuted: newMuted };
    });
  }, [logMessage]);

  return {
    sessionState,
    initSession,
    endSession,
    toggleMute,
  };
}
