# Application API

The local API serves JSON and the static website from one loopback origin. Domain errors use an HTTP status and an `error` message. All application data and actions require a signed-in session; health and local sign-in setup are public.

| Method | Route | Purpose and permission |
|---|---|---|
| GET | `/api/health` | Public service and version status |
| GET | `/api/auth/config` | Public local/Cognito sign-in configuration |
| GET | `/api/auth/session` | Current sign-in state; local anonymous response is allowed |
| POST | `/api/auth/login` | Loopback-only role demonstration |
| POST | `/api/auth/logout` | Revoke local session |
| GET | `/api/dashboard` | Summary, recent orders and activity; signed-in roles |
| GET | `/api/catalog` | Synthetic products, USD prices and available stock; signed-in roles |
| GET | `/api/orders` | Order list with optional `status` and `search`; signed-in roles |
| GET | `/api/orders/{id}` | One order and its timeline; signed-in roles |
| POST | `/api/orders` | Reserve and create an order; Operator/Admin |
| POST | `/api/orders/{id}/cancel` | Cancel unshipped work and compensate; Operator/Admin |
| GET | `/api/operations` | Work, failures, audit history and drill reports; signed-in roles |
| POST | `/api/operations/process` | Advance a pass of work; local Operator/Admin only |
| POST | `/api/operations/retry/{id}` | Recover failed fulfillment; Operator/Admin |
| POST | `/api/inventory/{sku}/adjust` | Add inventory; Admin |
| POST | `/api/operations/drill` | Isolated recovery evidence; Admin |
| GET | `/api/operations/drills/{id}` | Inspect drill evidence; signed-in roles |

The AWS worker consumes queue messages automatically; the manual `process` route returns a local-only conflict in cloud mode. Cloud drills start asynchronously and the client reads their recorded status. The cloud API does not provide local role login.

## Order semantics

An order identifies one or more catalog products and positive integer quantities. Prices come from the server's catalog; the client cannot set a price or inventory balance. Payment and fulfillment scenario choices control the labelled simulation only.

Example order body:

```json
{
  "customer": "Example Studio",
  "items": [{"sku": "TOTE-001", "quantity": 2}],
  "scenario": "happy_path",
  "idempotency_key": "example-request-0001"
}
```

Customer labels are 1–80 printable characters. Requests have 1–10 distinct products, each with a quantity from 1 to 100. Repeated SKU lines and unrecognized fields are rejected. An idempotency key contains 8–128 letters, digits, dashes or underscores. Scenarios are `happy_path`, `payment_declined`, `fulfillment_retry` and `fulfillment_dead_letter`.

Supply a stable idempotency key when submitting an order. The same identity, key and normalized payload returns the existing order. Reusing the key with changed content returns a conflict. Insufficient available inventory is a conflict, and the failed request must not leave a partial reservation or order.

New orders return HTTP 201 with `{"order": {...}, "replayed": false}`. An identical replay returns HTTP 200 with the original order and `replayed: true`; a conflict returns HTTP 409. HTTP requests may also provide `Idempotency-Key`, which must agree with the body when both are present. Keep the key in the JSON body when invoking the transport-independent engine directly.

An order moves from `queued` to `processing` and then `completed`, `failed` or `cancelled`. Payment decline marks the order failed, completes its work and releases stock. Fulfillment failures retain the reservation and payment, retry up to three attempts and then mark work `dead_letter`. Recovery changes the simulated provider to a healthy scenario and resumes fulfillment. It is a demonstration repair, not an automatic repair of a real provider.

Cancellation and retry actions require `{}`. Restocking accepts `{"quantity": 3}` and adds 1–1,000 units. Local processing accepts `{}` or `{"limit": 20}` with a limit from 1 to 20; it advances one current stage of each selected order. A happy order therefore takes two processing passes. A shipment already recorded cannot be cancelled; a returns workflow would be required.

Cancellation atomically releases the reservation, records any simulated refund and discards outgoing work that has not been published. A message already queued may still arrive; the completed cancellation prevents it from advancing the order.

Order creation records the reservation and first outgoing work atomically. Background processing advances the workflow and records audit events. Reads do not process work or move inventory. Failed work and an authorized explicit recovery action remain visible in operations history.

## Local request protection

The role demonstration creates an opaque cookie session. Subsequent mutating requests need the session's CSRF token and the correct origin. Invalid JSON, unsupported values, oversized bodies and unauthorized actions are rejected without domain mutations. Static file serving must not expose databases, private configuration, source files or parent directories.

Local login accepts only `{"role": "viewer"}`, `operator` or `admin` and requires the server's `Origin`. It returns an HTTP-only cookie and a CSRF token. Send that token in `X-CSRF-Token`, the cookie and the same origin for subsequent POST requests. A local POST requires `application/json`, one `Content-Length`, no transfer encoding and a body of at most 16 KB. API responses disable caching.

Domain errors contain `{"error": "description", "code": "machine_code"}`. Authentication failures are HTTP 401, permission/origin/CSRF failures 403, validation failures 400, missing resources 404, conflicts 409, oversized bodies 413 and unsupported media types 415. Unavailable local storage returns a generic 503 response; private exceptions are logged, not sent to the browser.

Cloud API authentication uses verified authorizer claims, not local cookies or client-selected roles. See [authentication](authentication.md).
