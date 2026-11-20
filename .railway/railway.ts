// Import-oriented candidate from the current STAGING project, 2026-11-03.
// Do not apply without a clean 'railway config plan' and explicit approval.
// All values remain on Railway; generated RAILWAY_* environment values are not managed.
// Keep a named partial so unrelated/future resources remain outside this ownership scope.
import { defineRailway, github, preserve, project, service } from "railway/iac";

export const partial = "j2m-core-services";

export default defineRailway(() => {
  const web = service("j2m-web", {
    source: github("Mendez2620/J2M-Interactive", { branch: "staging" }),
    build: { builder: "RAILPACK" },
    healthcheck: "/api/ready",
    env: {
      APPLE_WALLET_MODE: preserve(),
      CORS_ORIGINS: preserve(),
      DATABASE_CONNECT_TIMEOUT: preserve(),
      DATABASE_MAX_OVERFLOW: preserve(),
      DATABASE_POOL_RECYCLE: preserve(),
      DATABASE_POOL_SIZE: preserve(),
      DATABASE_POOL_TIMEOUT: preserve(),
      DATABASE_SSLMODE: preserve(),
      DATABASE_URL: preserve(),
      FLASK_ENV: preserve(),
      GOOGLE_ISSUER_ID: preserve(),
      GOOGLE_SERVICE_ACCOUNT_JSON: preserve(),
      GOOGLE_WALLET_MODE: preserve(),
      GUNICORN_THREADS: preserve(),
      J2M_PROCESS_TYPE: preserve(),
      JWT_SECRET_KEY: preserve(),
      LOG_REQUESTS: preserve(),
      PUBLIC_BASE_URL: preserve(),
      RECOVERY_MAIL_BACKEND: preserve(),
      REFERRAL_EVIDENCE_HMAC_KEYS: preserve(),
      REGISTRATION_EMAIL_ENCRYPTION_KEY: preserve(),
      REGISTRATION_IDENTITY_HMAC_KEY: preserve(),
      SECRET_KEY: preserve(),
      SESSION_COOKIE_SECURE: preserve(),
      TRUSTED_PROXY_COUNT: preserve(),
      UPLOAD_STORAGE_BACKEND: preserve(),
      WEB_CONCURRENCY: preserve(),
    },
  });

  const googleWalletWorker = service("j2m-google-wallet-worker", {
    source: github("Mendez2620/J2M-Interactive", { branch: "staging" }),
    build: { builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    start: "flask --app run.py run-google-wallet-sync-worker",
    env: {
      APPLE_WALLET_MODE: preserve(),
      CORS_ORIGINS: preserve(),
      DATABASE_CONNECT_TIMEOUT: preserve(),
      DATABASE_MAX_OVERFLOW: preserve(),
      DATABASE_POOL_RECYCLE: preserve(),
      DATABASE_POOL_SIZE: preserve(),
      DATABASE_POOL_TIMEOUT: preserve(),
      DATABASE_SSLMODE: preserve(),
      DATABASE_URL: preserve(),
      FLASK_ENV: preserve(),
      GOOGLE_ISSUER_ID: preserve(),
      GOOGLE_SERVICE_ACCOUNT_JSON: preserve(),
      GOOGLE_WALLET_MODE: preserve(),
      GOOGLE_WALLET_SYNC_ALERT_PENDING: preserve(),
      GOOGLE_WALLET_SYNC_ALERT_STALE_SECONDS: preserve(),
      GOOGLE_WALLET_SYNC_BATCH_SIZE: preserve(),
      GOOGLE_WALLET_SYNC_POLL_SECONDS: preserve(),
      J2M_PROCESS_TYPE: preserve(),
      JWT_SECRET_KEY: preserve(),
      PUBLIC_BASE_URL: preserve(),
      REFERRAL_EVIDENCE_HMAC_KEYS: preserve(),
      REGISTRATION_EMAIL_ENCRYPTION_KEY: preserve(),
      REGISTRATION_IDENTITY_HMAC_KEY: preserve(),
      SECRET_KEY: preserve(),
      SESSION_COOKIE_SECURE: preserve(),
      TRUSTED_PROXY_COUNT: preserve(),
      UPLOAD_STORAGE_BACKEND: preserve(),
    },
  });

  // A ONE-SHOT migrator; do not convert this command to an unconditional
  // preDeploy on the web or Google worker. Never change its branch to staging.
  const migrator = service("j2m-migrator-77a", {
    source: github("Mendez2620/J2M-Interactive", { branch: "main" }),
    build: { builder: "RAILPACK" },
    start: "python -m flask --app run.py db upgrade 9d0e1f2a3b4c",
    deploy: { restartPolicyType: "NEVER" },
    env: {
      CORS_ORIGINS: preserve(),
      DATABASE_CONNECT_TIMEOUT: preserve(),
      DATABASE_SSLMODE: preserve(),
      DATABASE_URL: preserve(),
      FLASK_ENV: preserve(),
      GOOGLE_WALLET_MODE: preserve(),
      JWT_SECRET_KEY: preserve(),
      PUBLIC_BASE_URL: preserve(),
      RECOVERY_MAIL_BACKEND: preserve(),
      REFERRAL_EVIDENCE_HMAC_KEYS: preserve(),
      REGISTRATION_EMAIL_ENCRYPTION_KEY: preserve(),
      REGISTRATION_IDENTITY_HMAC_KEY: preserve(),
      SECRET_KEY: preserve(),
      SESSION_COOKIE_SECURE: preserve(),
      TRUSTED_PROXY_COUNT: preserve(),
      UPLOAD_STORAGE_BACKEND: preserve(),
    },
  });

  return project("j2m-interactive-staging", {
    resources: [web, googleWalletWorker, migrator],
  });
});
