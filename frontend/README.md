# SupplySight frontend

Dependency-free HTML, CSS, and ES modules. No build step or third-party CDN is required. Start the repository's Python server, then open its local URL; opening `index.html` directly cannot provide the API.

`config.js` uses the same origin by default. The supplied CloudFront distribution forwards `/api/*` to API Gateway, so no URL change is needed for that deployment. A separately hosted frontend can set `window.API_BASE` to an API Gateway HTTPS base URL; that alternative also needs an explicit CORS configuration for its frontend origin.

The three views share a store, product, model, and replay cutoff. All analytical figures come from the HTTP API; the demo always labels synthetic data. The daily chart supports pointer inspection and, when focused, Left/Right/Home/End keys. The baseline checkbox toggles its comparison line. Assumption changes invalidate the old simulation, and the user can submit the form to rerun it.

At startup the frontend reads `/api/health` to identify local or AWS mode before requesting `/api/catalog`. An AWS catalog HTTP 404 with the expected unpublished-result message shows **Awaiting the first forecast**. Store, product, replay and simulation controls stay disabled until a complete batch is available. **Check for published forecasts** retries initialization. Other API failures display an error rather than the publication state.

The model lab's release action is an explicitly labelled illustrative workflow in local mode. It uses the current dataset, with a deliberately degraded candidate and a baseline clone. Records have `workflow_demo: true`; `synthetic_demo` is true only when the source dataset is synthetic. This exercise does not promote a trained model or establish forecast improvement, and is distinct from the actual trained-model rehearsal. AWS deployments display published release records and disable the public demo action. Release history remains accessible in AWS mode before a catalog exists, with a clear empty state when no decisions have been published.

The AWS frontend and API passed foundation smoke checks on 8 October 2026; that environment was subsequently torn down. Those checks did not validate live ML execution or forecast publication. Run the Python server for the current local workspace.
