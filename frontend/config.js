// Where the dashboard finds the SafeFace backend.
//
//  - Running locally (backend serves this page at http://localhost:8000): leave "".
//  - Deployed on Firebase Hosting: set this to your backend's public HTTPS URL,
//    e.g. "https://safeface-backend-xxxxx-uc.a.run.app" or your Cloudflare Tunnel URL.
//
// Users can also override it at runtime from the ⚙ Settings dialog (saved in the browser).
window.SAFEFACE_CONFIG = {
  apiBase: "",
};
