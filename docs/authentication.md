# Sign-in and permissions

SupplySight separates viewing a forecast from running a planning action. The API enforces permissions before accessing the repository. Hiding a button is an additional usability measure, not the authorization boundary.

| Role | Forecasts, models, release history and operations | Inventory simulations | Local recovery and release demonstrations |
|---|---|---|---|
| Viewer | Read | Denied | Denied |
| Planner | Read | Allowed | Allowed locally |

Recovery drills and illustrative release demonstrations remain unavailable in AWS mode, including to planners. Model promotion, ingestion, infrastructure changes and role administration stay under operator IAM permissions; a planner does not gain deployment privileges.

## Local access preview

Start the existing local server and choose **Viewer** or **Planner** on the sign-in screen. The preview uses generated identities and needs no password, AWS account or external identity provider. Role selection is intentionally available to anyone who can access this loopback-only development server. It exercises HTTP authorization and interface behavior; it does not authenticate a real person or provide production credential security.

The server issues an opaque, expiring session cookie. The cookie is HTTP-only and restricted to the same site. Sessions stay in server memory and disappear on restart. Local POST requests require matching origin checks; authenticated actions also require the session's CSRF token. Client-supplied role headers, unsigned tokens and fabricated cookies do not establish an identity.

The development server accepts only loopback bindings. Do not expose it to a LAN, tunnel or public proxy. Local Python commands and artifact files remain accessible to the workspace owner outside the HTTP permission model.

Try the following checks:

1. Sign in as Viewer and inspect the forecast and Operations views. Simulation and local demonstration buttons should explain that Planner access is required.
2. Sign out. The application clears visible planning data and returns to the sign-in screen.
3. Sign in as Planner and run a replenishment comparison or isolated recovery drill.
4. Refresh the page to verify the local session is retained. Restart the server to verify the session expires.

HTTP 401 means a valid session is required. HTTP 403 means the requested action is forbidden. Direct API requests have the same restrictions as browser actions.

## Cognito deployment configuration

Terraform prepares a Cognito user pool, a public browser app client, a hosted sign-in domain and the Viewer/Planner groups. Account creation is by administrator invitation. The browser client has no client secret and uses the authorization-code flow with PKCE S256; implicit token grants are disabled. Callback and logout addresses use the frontend's HTTPS root so they do not depend on a nonexistent S3 application route.

The browser keeps an access token in memory, never in a URL, local storage or a cookie. A short-lived, one-use state/PKCE transaction is stored in session storage while the browser redirects through Cognito. Callback parameters are removed from the address bar, and mismatched or expired transactions are rejected. Reloading the cloud application requires establishing a new browser session; the implementation does not persist refresh tokens.

API Gateway validates the token signature, issuer, audience, expiry and the required read scope. Lambda accepts only the verified authorizer claims and checks the access-token purpose, configured issuer/client, validity and assigned group. An ID token, arbitrary role header or decoded-but-unverified JWT cannot grant API access. The planner group authorizes planning actions. OAuth scopes alone do not determine role membership.

Users with no permitted group are denied application access. The execution roles do not receive Cognito user/group administration permissions. Assign or remove membership through an authorized operator account after deployment; do not put user email addresses or passwords in Terraform or repository files.

Sign-out clears the browser's application session and ends Cognito's hosted sign-in session. Already issued JWTs can remain valid until expiry with an offline JWT authorizer. Role changes similarly take effect when tokens are renewed. Use shorter token lifetimes or an additional server-side session/revocation check if immediate removal is required.

This change is prepared and tested locally. Cognito sign-in, actual JWT validation at API Gateway, account invitation and group administration require a deployed end-to-end test before cloud authentication is described as verified. AWS resources stay off until a separate deployment is intended.

## Validation evidence

On 8 October 2026, all 129 Python tests passed on Windows and in the Linux image with container networking disabled. The 22 frontend tests passed, including PKCE state/replay rejection, memory-only access tokens, expiry, stale-response cancellation, permission controls and clearing cached records on sign-out. Four Terraform plans using a mock provider passed, alongside formatting and configuration validation.

Twenty requests against the actual local HTTP server verified anonymous and forged identities are denied, Viewer reads are allowed, Viewer actions are forbidden, Planner simulation is allowed, and incorrect origins or CSRF tokens are denied. The imported dataset's hash remained unchanged. Browser checks also confirmed both roles, successful simulation, session retention on refresh, removal of prior results from the DOM and sign-out propagation to a second tab. The phone layout had no horizontal overflow, with sign-out accessible above the workspace. No AWS service calls or deployed-resource changes were made for these checks.

The local access preview can be inspected in the [sign-in screenshot](images/signin-demo.png). It contains no user credentials or dataset observations.

References: [API Gateway JWT authorization](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api-jwt-authorizer.html), [Cognito authorization and PKCE](https://docs.aws.amazon.com/cognito/latest/developerguide/authorization-endpoint.html), and [token revocation](https://docs.aws.amazon.com/cognito/latest/developerguide/token-revocation.html).
