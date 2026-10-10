# OrderFlow frontend

A dependency-free browser application for the OrderFlow API. The workspace includes an operations overview, searchable orders, inventory reservations and recovery controls. Payments and shipping are explicitly simulated.

Run the local server described in the repository README and open its URL. The server serves this folder and the API from the same origin.

## Access

Local evaluation offers Viewer, Operator and Administrator roles. Local sessions use server-issued HttpOnly cookies and a CSRF token on mutations. AWS mode uses Cognito authorization code flow with PKCE; access tokens remain in memory, and roles and action permissions come from the API. Role selection is unavailable in AWS mode.

The API configuration endpoint selects `local_demo` or `cognito`. Cognito requires the `openid`, `profile` and `orderflow/read` scopes and exact same-origin callback and logout URLs. Include `orderflow/write` for operator and administrator actions; the API also checks the assigned role. Keep `window.API_BASE` empty for the prepared CloudFront deployment. A separate API Gateway origin requires an explicitly configured CORS policy as well as a secure API URL; the current infrastructure uses the same-origin route.

## Workflows

- Create an order with one or more catalog products and a simulated success or failure scenario.
- Inspect its payment, shipment and event timeline.
- Advance local worker steps, inspect blocked work and retry fulfillment.
- Run an isolated recovery drill and inspect the recorded checks.
- Restock inventory with administrator access.

Order submissions retain their idempotency key after uncertain network or server responses. Retry with the same details to retrieve the original result. Changing an uncertain submission requires explicitly starting fresh; review the Orders page first.

The AWS workspace refreshes automatically while an order is queued or processing, work awaits dispatch or a recovery drill is running. Automatic data refresh pauses when the workspace is idle to limit cloud usage; session checks continue. Use Refresh to see changes made by other users in an idle workspace. Creating, retrying, cancelling, restocking and starting a drill always refresh the workspace. Local demonstration behavior is unchanged. Hidden tabs and open dialogs pause automatic refresh.

## Validation

```text
node --check frontend/app.js
node --check frontend/auth.js
node --test tests/frontend*.test.mjs
```

The behavior tests cover safe retries, output escaping, stock labels, cancellation controls, activity-based AWS refresh, role changes, CSRF, session expiry, sign-out and Cognito PKCE callback verification. Browser checks are also needed against the running API before deployment.
