// Copy this file to runtime-config.js at the deployed React origin when
// /runtime-config.js is not provided by FastAPI or a reverse proxy.
window.CORTEX_RUNTIME_CONFIG = {
  apiBase: "http://127.0.0.1:8000",
  enableDevSessionLogin: true,
  workEnabled: false,
  // devSessionLoginToken: "optional-local-dev-token",
};
