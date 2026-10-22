import { useState, type FormEvent } from 'react';
import { Link, useNavigate } from 'react-router';
import { Eye, EyeOff } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useAuthStore } from '@/stores/auth-store';
import { useTranslation } from 'react-i18next';

export default function LoginPage() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [twoFactorRequired, setTwoFactorRequired] = useState(false);
  const [code, setCode] = useState('');
  const { login, isLoading, error, clearError } = useAuthStore();
  const navigate = useNavigate();
  const { t } = useTranslation();

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      const result = await login({ username, password, code: twoFactorRequired ? code : undefined });
      if (result.twoFactorRequired) {
        setTwoFactorRequired(true);
        return;
      }
      navigate('/');
    } catch {
      // Error is already handled in store
    }
  };

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-semibold">{t('login.title', 'Sign in to your account')}</h1>

      <form onSubmit={handleSubmit} className="space-y-4">
        {error && (
          <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}

        {!twoFactorRequired ? (
          <>
            <div className="space-y-2">
              <Label htmlFor="username">{t('login.username', 'Username')}</Label>
              <Input
                id="username"
                type="text"
                value={username}
                onChange={(e) => {
                  setUsername(e.target.value);
                  clearError();
                }}
                placeholder={t('login.username_placeholder', 'Enter username')}
                autoComplete="username"
                autoFocus
                required
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="password">{t('login.password', 'Password')}</Label>
              <div className="relative">
                <Input
                  id="password"
                  type={showPassword ? 'text' : 'password'}
                  value={password}
                  onChange={(e) => {
                    setPassword(e.target.value);
                    clearError();
                  }}
                  placeholder={t('login.password_placeholder', 'Enter password')}
                  autoComplete="current-password"
                  className="pr-9"
                  required
                />
                <button
                  type="button"
                  onClick={() => setShowPassword((v) => !v)}
                  aria-label={showPassword ? t('login.hide_password', 'Hide password') : t('login.show_password', 'Show password')}
                  className="absolute inset-y-0 right-0 flex items-center px-2.5 text-muted-foreground transition-colors hover:text-foreground"
                >
                  {showPassword ? <Eye className="h-4 w-4" /> : <EyeOff className="h-4 w-4" />}
                </button>
              </div>
            </div>
          </>
        ) : (
          <div className="space-y-2">
            <Label htmlFor="code">{t('login.two_factor_code', 'Authentication code')}</Label>
            <Input
              id="code"
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={code}
              onChange={(e) => {
                setCode(e.target.value);
                clearError();
              }}
              placeholder={t('login.two_factor_placeholder', '6-digit code or backup code')}
              autoFocus
              required
            />
            <p className="text-xs text-muted-foreground">
              {t('login.two_factor_hint', 'Enter the code from your authenticator app, or a backup code.')}
            </p>
          </div>
        )}

        <div className="flex items-center gap-4 pt-1">
          <Button type="submit" disabled={isLoading}>
            {isLoading ? (
              <span className="flex items-center gap-2">
                <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                {t('login.signing_in', 'Signing in...')}
              </span>
            ) : twoFactorRequired ? (
              t('login.verify', 'Verify')
            ) : (
              t('login.sign_in', 'Sign in')
            )}
          </Button>
          {twoFactorRequired ? (
            <button
              type="button"
              onClick={() => { setTwoFactorRequired(false); setCode(''); clearError(); }}
              className="text-sm font-medium text-primary transition-opacity hover:opacity-80"
            >
              {t('login.back', 'Back')}
            </button>
          ) : (
            <Link
              to="/forgot-password"
              className="text-sm font-medium text-primary transition-opacity hover:opacity-80"
            >
              {t('login.forgot_password', 'Forgot password')}
            </Link>
          )}
        </div>
      </form>
    </div>
  );
}
