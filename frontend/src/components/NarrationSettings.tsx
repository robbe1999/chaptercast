import type { Model, VoiceSettings } from "../api/schemas";

export type TunedSettings = Required<VoiceSettings>;

/** ElevenLabs' documented defaults: what "no custom settings" sounds like for most voices. */
export const DEFAULT_SETTINGS: TunedSettings = {
  stability: 0.5,
  similarity_boost: 0.75,
  style: 0,
  speed: 1,
};

interface Props {
  models: Model[];
  modelId: string;
  onModel: (modelId: string) => void;
  custom: boolean;
  onCustom: (custom: boolean) => void;
  settings: TunedSettings;
  onSettings: (settings: TunedSettings) => void;
}

export function costLabel(model: Model): string {
  return model.cost_multiplier === 1 ? "" : ` · ${model.cost_multiplier}× credits`;
}

export function NarrationSettings({
  models,
  modelId,
  onModel,
  custom,
  onCustom,
  settings,
  onSettings,
}: Props) {
  const model = models.find((m) => m.model_id === modelId) ?? models[0];
  const set = (key: keyof TunedSettings) => (value: number) => onSettings({ ...settings, [key]: value });

  return (
    <fieldset className="settings">
      <legend>Narration</legend>

      <div className="field">
        <label htmlFor="model">Model</label>
        <select
          id="model"
          value={model?.model_id}
          onChange={(event) => onModel(event.target.value)}
          aria-describedby="model-description"
        >
          {models.map((m) => (
            <option key={m.model_id} value={m.model_id}>
              {m.name}
              {costLabel(m)}
            </option>
          ))}
        </select>
        {model?.description && (
          <p id="model-description" className="muted small">
            {model.description}
          </p>
        )}
      </div>

      <label className="check">
        <input type="checkbox" checked={custom} onChange={(event) => onCustom(event.target.checked)} />
        Fine-tune the voice
      </label>

      {custom && (
        <div className="sliders">
          <Slider
            id="stability"
            label="Stability"
            hint={["More expressive", "More consistent"]}
            min={0}
            max={1}
            step={0.05}
            value={settings.stability}
            onChange={set("stability")}
          />
          <Slider
            id="similarity"
            label="Similarity"
            hint={["Looser", "Closer to the original voice"]}
            min={0}
            max={1}
            step={0.05}
            value={settings.similarity_boost}
            onChange={set("similarity_boost")}
          />
          <Slider
            id="style"
            label="Style exaggeration"
            hint={["Neutral", "Dramatic"]}
            min={0}
            max={1}
            step={0.05}
            value={settings.style}
            onChange={set("style")}
            disabled={!model?.supports_style}
            note={model?.supports_style ? undefined : "Not supported by this model."}
          />
          <Slider
            id="speed"
            label="Speed"
            hint={["Slower", "Faster"]}
            min={0.7}
            max={1.2}
            step={0.05}
            value={settings.speed}
            onChange={set("speed")}
            format={(v) => `${v.toFixed(2)}×`}
          />
          <button type="button" className="link" onClick={() => onSettings(DEFAULT_SETTINGS)}>
            Reset to defaults
          </button>
        </div>
      )}
    </fieldset>
  );
}

interface SliderProps {
  id: string;
  label: string;
  hint: [string, string];
  min: number;
  max: number;
  step: number;
  value: number;
  onChange: (value: number) => void;
  disabled?: boolean;
  note?: string;
  format?: (value: number) => string;
}

function Slider({ id, label, hint, min, max, step, value, onChange, disabled, note, format }: SliderProps) {
  const shown = format ? format(value) : value.toFixed(2);
  return (
    <div className="slider">
      <div className="slider-head">
        <label htmlFor={id}>{label}</label>
        <output htmlFor={id}>{disabled ? "—" : shown}</output>
      </div>
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        aria-valuetext={shown}
        aria-describedby={`${id}-hint`}
        onChange={(event) => onChange(Number(event.target.value))}
      />
      <div id={`${id}-hint`} className="slider-hint muted small">
        <span>{hint[0]}</span>
        <span>{note ?? hint[1]}</span>
      </div>
    </div>
  );
}
