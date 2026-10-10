# Sign-in and permissions

Local sign-in is a role demonstration. It assigns a generated identity to a browser session so permissions can be exercised without an external identity provider. It does not validate a person's name, email or password. Do not expose this mode as public production sign-in.

| Role | Read orders, inventory and operations | Create, process, retry and cancel orders | Restock inventory and run drill |
|---|---|---|---|
| Viewer | Yes | No | No |
| Operator | Yes | Yes | No |
| Admin | Yes | Yes | Yes |

The server owns authorization. Changing a label in the browser, adding a role to a JSON body or providing a custom role header does not grant access. Anonymous requests to application data return 401; signed-in users without permission receive 403.

Local sessions use opaque HTTP-only cookies. Mutating requests require a same-origin request and the session's CSRF token. Sessions are temporary and are not a production credential store. The local server binds to loopback by default; keep it there.

Local sessions expire after one hour and are cleared on server restart. The same role uses the same generated demonstration identity across browser sessions; these are workspace roles, not independent customer accounts. Idempotency keys are scoped to that identity. The application is a shared staff workspace: authorized roles inspect its orders, and there is no customer tenant isolation.

## AWS identity boundary

The prepared cloud API uses Cognito and API Gateway's JWT authorizer. API Gateway validates the token issuer and intended audience; Lambda uses the authorizer's verified claims to determine the allowed role. Cognito groups must be managed by an administrator. No public self-service request can make its caller an Admin.

The temporary Sydney deployment on 10 October 2026 verified hosted Cognito sign-in and sign-out for Viewer, Operator and Admin, plus forbidden role actions through the real API. All temporary users were removed with the stack. The local role selector must not be enabled for the cloud API. Hosted token expiry and wrong issuer/audience rejection still need live checks before a real launch. See [verification](verification.md).

The demo is not a complete commerce security program. Customer-facing use needs real identity lifecycle controls, tenant and order ownership rules appropriate to the business, abuse controls, operational access review and privacy decisions before customer records are introduced.
