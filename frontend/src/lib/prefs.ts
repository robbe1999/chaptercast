import type { VoiceSettings } from "../api/schemas";

/** Per-browser conveniences only. Storage can be blocked, so every access is guarded. */
export interface Prefs {
  voiceId?: string;
  modelId?: string;
  customSettings?: boolean;
  voiceSettings?: VoiceSettings;
}

const KEY = "chaptercast.prefs";

export function loadPrefs(): Prefs {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(KEY) ?? "{}");
    return parsed && typeof parsed === "object" ? (parsed as Prefs) : {};
  } catch {
    return {};
  }
}

export function savePrefs(prefs: Prefs): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(prefs));
  } catch {
    /* storage unavailable: preferences simply are not remembered */
  }
}
