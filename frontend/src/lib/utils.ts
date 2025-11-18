import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"
import { CLIENT_CONFIG } from "./constants"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export async function fetchApiMode(): Promise<{
  mode: string;
  realtime_available: boolean;
  voicelive_available: boolean;
  voice: string;
}> {
  try {
    const response = await fetch(`${CLIENT_CONFIG.backendBaseUrl}/mode`);
    if (!response.ok) {
      console.warn("Failed to fetch API mode, using default");
      return {
        mode: "realtime",
        realtime_available: true,
        voicelive_available: false,
        voice: "alloy"
      };
    }
    return await response.json();
  } catch (error) {
    console.error("Error fetching API mode:", error);
    return {
      mode: "realtime",
      realtime_available: true,
      voicelive_available: false,
      voice: "alloy"
    };
  }
}

// Escape special characters for inclusion inside SSML prosody block
function escapeForSSML(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

export function wrapWithPortugueseSSML(text: string, voiceName: string = 'en-US-AvaMultilingualNeural'): string {
  const escaped = escapeForSSML(text.trim());
  return `<speak version="1.0" xml:lang="pt-PT" xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="https://www.w3.org/2001/mstts">` +
    `\n  <voice name="en-US-AvaMultilingualNeural">\n    <lang xml:lang="pt-PT">\n      <mstts:express-as style="general">\n        <prosody rate="0%" pitch="+0st">${escaped}</prosody>\n      </mstts:express-as>\n    </lang>\n  </voice>\n</speak>`;
}

// Very lightweight heuristic to detect likely Portuguese vs English.
// Counts presence of common Portuguese function words.
const PORTUGUESE_COMMON = [
  'de','que','e','o','a','os','as','um','uma','para','com','não','na','no','em','por','se','ao','mais','já','como','são','vou','pode','poder','sobre','qual','qualquer'
];
export function isLikelyPortuguese(text: string): boolean {
  const lower = text.toLowerCase();
  let hits = 0;
  for (const w of PORTUGUESE_COMMON) {
    if (lower.includes(` ${w} `) || lower.startsWith(w + ' ') || lower.endsWith(' ' + w)) {
      hits++;
    }
  }
  // Arbitrary threshold; tweak as needed
  return hits >= 2;
}
