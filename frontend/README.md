# Shelfcast frontend

Dependency-free HTML, CSS, and ES modules. No build step or third-party CDN is required. Start the repository's Python server, then open its local URL; opening `index.html` directly cannot provide the API.

`config.js` uses the same origin by default. The supplied CloudFront distribution forwards `/api/*` to API Gateway, so no URL change is needed for that deployment. A separately hosted frontend can set `window.API_BASE` to an API Gateway HTTPS base URL; that alternative also needs an explicit CORS configuration for its frontend origin.

The three views share a store, product, model, and replay cutoff. All analytical figures come from the HTTP API; the demo always labels synthetic data. The daily chart supports pointer inspection and, when focused, Left/Right/Home/End keys. The baseline checkbox toggles its comparison line. Assumption changes invalidate the old simulation, and the user can submit the form to rerun it.

The model lab's release action is an explicitly labelled workflow demonstration in local mode. AWS deployments display published release records and disable the public demo action.
