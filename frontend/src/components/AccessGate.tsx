import { useState, type FormEvent } from "react";

interface Props {
  onSubmit: (token: string) => void;
  error: string | null;
  busy: boolean;
}

export function AccessGate({ onSubmit, error, busy }: Props) {
  const [value, setValue] = useState("");
  const trimmed = value.trim();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!trimmed) return;
    onSubmit(trimmed);
    setValue(""); // do not leave the secret sitting in the DOM; a retry starts clean
  };

  return (
    <form className="panel narrow" onSubmit={submit} aria-labelledby="gate-title">
      <h2 id="gate-title">Access required</h2>
      <p className="muted">
        This server is protected. Enter the access token you were given. It stays in this browser tab
        and is only ever sent to this server.
      </p>
      <label htmlFor="token">Access token</label>
      <input
        id="token"
        type="password"
        autoComplete="off"
        spellCheck={false}
        value={value}
        onChange={(event) => setValue(event.target.value)}
        aria-describedby={error ? "token-error" : undefined}
        aria-invalid={error ? true : undefined}
      />
      {error && (
        <p id="token-error" role="alert" className="error">
          {error}
        </p>
      )}
      <button type="submit" className="primary" disabled={!trimmed || busy}>
        {busy ? "Checking…" : "Continue"}
      </button>
    </form>
  );
}
