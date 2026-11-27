# Read-only and responsible-use policy

This independent integration is intended only to retrieve information that
the authenticated resident is legitimately authorized to access. It is not
affiliated with or endorsed by OCEA, and this policy does not assert that OCEA
has granted any permission or authorization.

Consultation means retrieving resident, dwelling, equipment, consumption,
measurement or alert information without intentionally modifying server-side
resources. The HTTP verb alone does not establish that behavior: some POST
requests submit filters for a data query, while even a GET may trigger a
stateful workflow. An operation with uncertain effects must remain excluded
until its behavior is sufficiently evidenced and reviewed by a person.

Account or dwelling updates, password and notification changes, consent or
service settings, equipment configuration or remote commands, resource
creation, modification or deletion, and alert acknowledgment, closure or
deletion are outside the supported API. Do not automate operations whose
effects are unknown.

Use only legitimate credentials and access only resources authorized for that
account. Never bypass authentication, access controls, consent restrictions or
other service restrictions. Respect the provider's terms and applicable law;
read-only access does not automatically make a use lawful. This document is
not legal advice.

Minimize requested and retained data. Never publish credentials, tokens,
cookies, personal details, resident or dwelling identifiers, serial numbers or
authenticated captures. Review and minimize any example before sharing it.
Avoid excessive traffic, polling or bulk collection; respect server errors,
rate limits and the integration's existing five-hour polling interval.
