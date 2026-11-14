import { createAuthClient } from '@neon/auth';

const baseUrl = import.meta.env.VITE_NEON_AUTH_URL;
export const authClient = baseUrl ? createAuthClient(baseUrl) : null;
