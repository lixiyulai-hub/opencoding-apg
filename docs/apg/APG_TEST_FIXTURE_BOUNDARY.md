# APG Test Fixture Boundary

## Project identity
- Purpose: test APG governance and release/deployment orchestration.
- Mode: isolated test fixture.
- Product implementation: out of scope.
- Existing `services/domain` and `apps/miniapp` files: retained as inert fixture artifacts for APG change/evidence tests.

## Actions allowed in fixture
- Local file changes under approved plan-change scope.
- Offline tests, hash checks, receipt validation, deployment manifest linting, and rollback rehearsal.
- Simulated deployment/publication previews with no external side effects.

## Actions excluded
- Real WeChat account access.
- Provider/network calls.
- Credentials or secrets.
- Real child data.
- Runtime deployment, publication, pilot, or release.
- Git mutation.

## Release boundary
A future release test may simulate approval flow only. Any real deployment or publication requires a separate explicit owner transaction.
