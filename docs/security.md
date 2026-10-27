# Security and configuration

- Keep OpenAI credentials in backend environment variables. Never use `VITE_` variables for secrets.
- The AI call has a 20-second timeout and one bounded SDK retry. Provider exception messages and secrets are not returned to clients.
- CORS permits only `FRONTEND_ORIGIN` (default local Vite origin).
- No authentication or user-specific persistence exists yet. Keep the development server on localhost / a trusted network.
- Live trading is not implemented. Zerodha and Upstox remain `NOT_CONFIGURED`, and paper orders cannot invoke a broker. The `CONNECTED` DEMO catalog row means only that the synthetic provider is locally available.
- Do not send API keys, broker tokens, or account credentials in chat, screenshots, tests, or source files. Rotate any accidentally exposed credential at its provider.
- Before production: add authentication and authorization, CSRF protection if cookie sessions are introduced, rate limits, request limits, encrypted token storage, database migrations, structured redacted logs, dependency scanning, and deployment secrets management.
