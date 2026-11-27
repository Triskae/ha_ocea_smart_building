# OCEA resident API reference

This independent, incomplete reference describes the resident API using the
portal's client-side code delivered to the browser, observations of data
available to an authenticated resident account, and this integration. Account
data is accessible only within that account's permissions. It is not an
official OCEA contract, an SDK, a guarantee of availability or an invitation
to access data without authorization. Read the [read-only policy](READ_ONLY_POLICY.md)
before using or extending it.
The inventory is limited to operations relevant to the integration's scope;
it is not an exhaustive list of portal operations.

## Contents

- [OpenAPI 3.1](openapi.yaml): sufficiently evidenced consultation operations.
- [Endpoint inventory](inventory/endpoints.yaml): evidenced consultation
  operations within the integration's scope.
- [Client coverage](inventory/client_coverage.yaml): operation-level mapping
  to the existing Python client, not a claim that all fields are interpreted.
- [Authentication](authentication.md) and [domain notes](domains/README.md).
- [Incomplete data formats](data-formats.md): known gaps and evidence needed.
- [Architecture](../architecture.md): current integration behavior and limits.

Paths are relative to the API host used by the integration, defined in
[`ocea/const.py`](../../custom_components/ocea_smart_building/ocea/const.py).
The portal's generated client also contains a development default; its presence
does not establish the deployment used in production. Supplied observations
were analyzed offline; no additional API calls were made for this reference.

## Evidence and classifications

One inventory entry represents an HTTP operation, not a URL alone; methods on
the same URL count separately. Source references identify functions or usage
in the portal's client-side code. Observations come from requests and responses
available during a resident session; they are not published as raw captures.
The analysis is limited to code delivered to the browser and data the account
is authorized to access, without bypassing access controls. Resident data
remains account-specific.

Evidence labels describe distinct aspects:

- `discovered`: a declaration in the portal's client-side code, even if not used.
- `observed`: at least the request or the response was recorded during a
  resident session. Each entry's `observations` lists `request`, `response`,
  both, or neither; it is not inferred from fixture availability.
- `verified`: portal usage and the supplied exchanges agree on the specific
  consultation behavior described in the entry's note. This does not verify
  every field, response status, permission, parameter, or server-side effect.
- `implemented`: the integration's Python client implements the operation,
  regardless of whether it interprets every field.

The supplied request records identify a method and path; for the observed POSTs,
they also include JSON bodies. They do not establish all request headers. The
corresponding recorded responses are HTTP 200 JSON; they are examples, not
proof that all other statuses or response shapes are impossible. Empty
`observations` means no exchange was supplied, not that the operation is
unavailable. Fixtures are minimized excerpts and cover only some observations.

`read_only` means evidence indicates a consultation; it does not prove that the
service has no internal logging or other invisible side effects. Operations
with insufficiently established effects and state-changing operations are
outside the inventory and OpenAPI. A POST can be a
read-only query: the four included POSTs submit filters or PDS identifiers and
the portal consumes returned data. Their classification is based on the
specific usage, not the method alone. Do not treat a GET as automatically safe.

The generated portal client accepts 200 and 204 for the listed operations;
this is client behavior, not proof the
server returns each status. The integration currently accepts only 200 for API
JSON and retries once after a 401. The independent manual CLI also accepts
only 200 JSON and covers resident, dwelling, dashboard, PDS and both consumption
series reads, but not the PDS device lookup; coverage in the inventory refers
to the integration, not the CLI. OpenAPI response fields without a type
represent names seen in portal use, not a promise of type, requiredness or
nullability. Even typed fields are partial client-side expectations, not a
complete server contract. No query parameters are known for supported calls.

## Baseline

The inventory contains **19** in-scope resident-API operations classified
read-only. **19** are discovered; **7** have an observed request, **7** an
observed response, and **7** have narrowly verified consultation behavior.
The integration's Python client implements **7** of the 19
read-only operations, leaving **12** not implemented at operation level.
Implementation does not imply full field or fluid coverage. The separate
maintenance service observed in the capture is not counted as a resident API
operation.

Dashboard and measurement data, resident, dwelling, PDS, devices, consumption,
alerts and goals have portal evidence. Comfort, alert and goal response
contracts and permissions remain particularly incomplete.
Resident-wide threshold alerts and dwelling alerts have distinct scopes.
The integration's leak binary sensor is derived from consumption data, not an
implementation of either alert API.

The portal hides PDS history for non-telemetry meters, while the integration
does not apply that same `typeReleve` filter. Portal UI limits and consent
restrictions are not proven server guarantees. Daily and monthly aggregation,
date boundaries, leak-estimate units and the scope of access require further
evidence before client changes. See [domain notes](domains/README.md).

## Fixtures and privacy

The supplied captures contain account data even where values look obfuscated.
The [fixture manifest](fixtures/manifest.yaml) describes captured requests and
responses, with selected entries reduced and sensitive values replaced.
Placeholder dates, measurements, coefficients and flags are not observations
of actual account values. The excerpts are independent and their placeholders
do not establish relationships between fixtures. No comfort request or
response was present in the supplied captures, so no comfort fixture is
provided. Do not copy raw captures
into issues, logs, tests or the repository. Future fixtures must be minimized,
reviewed for all personal information and identifiers, preserve only supported
structure, and record their source, anonymization and evidence level. Do not
invent a response to fill a gap.

## Maintenance and validation

1. Review an operation's actual portal usage and available independent
   observations; record relevant source context and uncertainties.
2. Explicitly review any potentially state-changing behavior. If uncertain,
   keep the operation outside the inventory and OpenAPI; never infer safety
   from GET or POST.
3. Update the inventory, coverage and OpenAPI together. Preserve unique
   operation IDs, original wire names and the access extension. Update the
   counts and domain notes when evidence changes.
4. Validate offline with the existing test environment and the pinned
   OpenAPI validator:

   ```sh
   uv run --python 3.14.2 --with pytest --with pyyaml \
     python -m pytest --noconftest -q tests/test_api_reference.py
   uvx --from "openapi-spec-validator==0.7.2" \
     openapi-spec-validator docs/api/openapi.yaml
   uv run --python 3.14.2 --with "jsonschema==4.25.1" --with pyyaml \
     python scripts/validate_api_fixtures.py
   ```

The validators are development-only tools, pinned in CI; they are not installed
by the integration. They validate the full OpenAPI 3.1 document rather than
only YAML syntax, and fixture JSON against its corresponding schema. Tests
check cross-references, coverage, access classification and operation
consistency. Run the repository's complete validation commands before proposing
a change.
