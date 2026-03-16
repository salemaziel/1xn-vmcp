import { useState } from 'react';
import { apiClient } from '@/api/client';

export function useGoogleAuth() {
  const [loading, setLoading] = useState(false);

  const startOAuthRedirect = async (
    provider: 'google' | 'oidc',
    isRegister: boolean = false,
    returnUrl?: string,
    username?: string
  ) => {
    setLoading(true);
    try {
      const startUrl = apiClient.buildOAuthStartUrl(provider, {
        mode: isRegister ? 'register' : 'login',
        username,
        returnTo: returnUrl,
      });
      window.location.assign(startUrl);
    } catch (error) {
      setLoading(false);
      throw error;
    }
  };

  const signInWithGoogle = async (isRegister: boolean = false, returnUrl?: string, username?: string) => {
    await startOAuthRedirect('google', isRegister, returnUrl, username);
  };

  const signInWithOidc = async (isRegister: boolean = false, returnUrl?: string, username?: string) => {
    await startOAuthRedirect('oidc', isRegister, returnUrl, username);
  };

  return {
    signInWithGoogle,
    signInWithOidc,
    loading,
  };
}
