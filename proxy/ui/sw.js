// Here only so Chromium offers to install the UI as an app. It caches
// nothing: the page is live state, and offline it would only be lying.
self.addEventListener('fetch', () => {});
