import { ShieldCheck } from "lucide-react";
import { useState, type FormEvent } from "react";
import { ApiError } from "../api/client";
import { Button, Field, Input, PasswordInput } from "../components/ui";
import { useAuth } from "../store/hooks";

export function LoginPage() {
  const { login } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Unable to sign in. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-brand-900 px-4">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center justify-center gap-2 text-white">
          <ShieldCheck className="size-7 text-brand-100" />
          <span className="text-lg font-semibold tracking-wide">Governix</span>
        </div>
        <form onSubmit={submit} className="space-y-4 rounded-lg bg-surface p-5 shadow-xl sm:p-6">
          <div>
            <h1 className="text-base font-semibold">Sign in</h1>
            <p className="text-sm text-muted">Policy intelligence for your organization</p>
          </div>
          <Field label="Email" htmlFor="email">
            <Input id="email" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
          </Field>
          <Field label="Password" htmlFor="password">
            <PasswordInput id="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
          {error && <p className="rounded-md bg-bad-50 px-3 py-2 text-sm text-bad-600" role="alert">{error}</p>}
          <Button type="submit" className="w-full" loading={busy}>Sign in</Button>
        </form>
      </div>
    </div>
  );
}
