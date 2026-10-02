# AGENTS.md

## Project scope

This repository contains a public Home Assistant custom integration for Ocea
Smart Building and a manual Python CLI for testing the Ocea resident API. Keep
the integration installable with HACS and compatible with the Home Assistant
version targeted by the project. Preserve the existing Azure AD B2C
authentication behavior unless a change is explicitly required and tested.

## Communication and authorization

- Work step by step and communicate with the user in French.
- Do not push, create a tag, publish a release, or perform another external write
  unless the user explicitly asks for it.
- Do not create a commit unless the user explicitly authorizes it.

## Privacy and security

- The repository is public. Never commit personal email addresses, passwords,
  access or refresh tokens, session cookies, authorization codes, CSRF values,
  resident identifiers, dwelling identifiers, captured authenticated HTML, or
  API responses containing account data.
- Never log or expose the email, stored password, tokens, or session cookies from
  a Home Assistant config entry.
- Credentials must be validated against Ocea before a config-flow change is
  saved.
- In the reconfiguration form, never expose or prefill the stored password. The
  unchanged-password sentinel must preserve the currently stored password.
- Keep hidden config-entry data intact by using `data_updates` rather than
  replacing the complete entry data unless a migration explicitly requires it.
- Do not include secrets in tests, fixtures, documentation, changelogs, commit
  messages, screenshots, or diagnostic output.

## Home Assistant integration rules

- The integration domain is `ocea_smart_building`.
- Use the native `async_step_reconfigure` flow for credentials stored in
  `config_entry.data`. Do not move those credentials to an `OptionsFlow` or YAML
  configuration. Credentials are required to set up and reload the integration,
  while an `OptionsFlow` is intended for optional preferences; keeping them in
  the native reconfigure flow also provides Home Assistant's standard
  **Reconfigure** button without changing the existing storage model.
- Keep email and an optional replacement password editable after installation.
  The dwelling `local_id` is discovered from the validated Ocea account and must
  not be exposed as a manually editable field.
- Use `FlowResult` from `homeassistant.data_entry_flow`; do not use
  `ConfigFlowResult`.
- Keep credential validation in `OceaApiClient.validate_credentials` and execute
  synchronous API calls through `hass.async_add_executor_job`.
- Preserve the browser-compatible Azure AD B2C request flow, cookie handling,
  PKCE behavior, redirect parsing, and User-Agent prefix. These details are
  required for authentication reliability.
- Reconfiguration must reload the integration only when entry data, unique ID,
  or title actually changes.
- Keep the entry title and discovered dwelling identifier synchronized with the
  validated resident data.
- Preserve the current five-hour polling interval unless a change is explicitly
  requested and tested.
- Keep `strings.json`, `translations/en.json`, and `translations/fr.json` aligned
  whenever config-flow fields, errors, abort reasons, or entity names change.
- Keep the local brand icon under
  `custom_components/ocea_smart_building/brand/icon.png`.

## Quality scale tracking

- `custom_components/ocea_smart_building/quality_scale.yaml` tracks the Home
  Assistant integration quality scale rules (`done`, `todo`, or `exempt` with a
  comment).
- Update it in the same change whenever a rule becomes fully or partially
  satisfied, or regresses (for example, reintroducing `hass.data` after
  adopting `runtime_data`, removing `PARALLEL_UPDATES`, or dropping a test that
  backs a `done` rule).
- Mark a rule `done` only when the code and tests support it; keep `todo` for
  partial compliance and explain the gap in the pull request.
- Keep every rule listed through platinum and give each `exempt` rule a reason;
  `tests/test_quality_scale.py` enforces this.

## Manual CLI rules

- Keep `ocea_cli.py` usable without Home Assistant. Its only third-party runtime
  dependency is `requests`.
- Preserve the existing command-line flags and machine-independent behavior.
- Never persist the email, password, cookies, or tokens.
- Do not print passwords or session cookies. Token output must remain restricted
  to the explicit `--dump-tokens` option and must never be captured in committed
  files or issue reports.
- Keep normal output understandable for non-technical users and keep verbose
  logging free of passwords and bearer tokens.
- Preserve the browser-compatible B2C request behavior used by the integration.

## Validation

Before presenting a change as complete, run:

```bash
uv run --python 3.14.2 \
  --with homeassistant==2026.7.3 \
  --with pytest-homeassistant-custom-component \
  python -m pytest -q
uv run --python 3.14.2 python -m compileall -q custom_components tests ocea_cli.py
python3 -m json.tool custom_components/ocea_smart_building/manifest.json >/dev/null
python3 -m json.tool custom_components/ocea_smart_building/strings.json >/dev/null
python3 -m json.tool custom_components/ocea_smart_building/translations/en.json >/dev/null
python3 -m json.tool custom_components/ocea_smart_building/translations/fr.json >/dev/null
git diff --check
```

- Validate `.github/workflows/ci.yml` and `.github/workflows/release.yml` whenever
  either workflow changes.
- Run the pinned Ruff lint command used by CI:

```bash
uvx --from "ruff==0.16.1" ruff check custom_components tests
```

- Keep detailed tests for the CI and release workflow contracts, including
  triggers, permissions, pinned tools, quality commands, release-version checks,
  changelog generation, and the absence of release commands from CI.
- Add or update tests for changed API, config-flow, translation, CLI, version, and
  release behavior.

## Versions and releases

- Use semantic versions and keep the same version in:
  - `custom_components/ocea_smart_building/manifest.json`
  - `custom_components/ocea_smart_building/const.py` (`INTEGRATION_VERSION` and
    the versioned User-Agent product token)
- Preserve the existing browser User-Agent prefix when updating the versioned
  product token.
- A normal push to `main` must not create a release.
- Releases are triggered only by pushing a semantic-version tag such as
  `v1.1.0`.
- `.github/workflows/release.yml` validates the tag and project version, creates
  the matching GitHub Release, lists commits between the new tag and the
  preceding tag, and links to the complete GitHub comparison.
- Before tagging, ensure the worktree is clean, tests pass, `main` is synchronized
  with `origin/main`, and the tag version matches the project version.
- Publish in this order:

```bash
git push origin main
git tag -a vX.Y.Z -m "Ocea Smart Building vX.Y.Z"
git push origin vX.Y.Z
```

- After pushing the tag, verify that the GitHub Actions run succeeded and that
  the release is published with the expected changelog.
