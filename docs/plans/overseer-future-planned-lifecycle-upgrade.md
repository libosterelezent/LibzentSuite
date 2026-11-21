# Implementation Assignment: LibzentSuite Overseer

## Objective

Implement Overseer as a first-class core infrastructure service responsible for suite-wide lifecycle monitoring, health monitoring, security monitoring, and data flow observability.

Overseer must be implemented before the central Task Runtime. The future Task Runtime must be able to register with Overseer and expose its operational state through the same monitoring contracts as other core components.

## Mandatory preliminary work

Before implementation:

1. Inspect the actual repository structure and current branch.
2. Inspect `core/bootstrap.py`, `core/api.py`, the existing managers, Event Bus, persistence layer, account/plugin data system, workspace orchestration, and any existing Overseer implementation.
3. Identify existing capabilities that can be reused.
4. Identify duplicated responsibilities and missing monitoring contracts.
5. Produce a code-traced implementation map before modifying anything.
6. Do not assume the proposed directory structure exists or that every proposed module is necessary.

## Required functional domains

### A. Health monitoring
- Component registration and availability.
- Readiness and responsiveness.
- Healthy, degraded, unavailable, failed and unknown states.
- Failure and recovery detection.
- Dependency-aware health assessment.
- Monitoring worker health.
- Database and infrastructure health where applicable.

### B. Security monitoring
- Authentication and authorization security events.
- Account, plugin, profile and workspace ownership violations.
- Unexpected access patterns.
- Security-sensitive configuration changes.
- Secret exposure detection where technically feasible.
- Structured security audit records.
- Incident severity classification.

Overseer observes and evaluates security. It must not replace authentication, authorization, access control, encryption or existing enforcement mechanisms.

### C. Data flow monitoring
- Operation correlation IDs.
- Component-to-component event tracing.
- Event delivery and processing outcomes.
- Request and operation metadata.
- Failure propagation.
- Processing duration.
- Account/plugin ownership context.
- Stalled and abandoned operation detection.

Do not indiscriminately collect payloads, credentials, tokens or sensitive configuration values.

### D. Lifecycle monitoring
- Component registration.
- Initialization and readiness.
- Activation and deactivation.
- Degraded and recovery transitions.
- Shutdown and termination.
- Unexpected component failure.
- Dependency-aware lifecycle observations.

## Architectural requirements

- Overseer is a core runtime service initialized through the existing bootstrap architecture.
- It must have a component registry and explicit monitoring contracts.
- Components should publish relevant events rather than requiring continuous polling.
- Polling is allowed where event-based monitoring is insufficient.
- Monitoring must not block normal component execution.
- Monitoring failures must not cascade into monitored components.
- The monitoring system must support asynchronous operation and concurrent event ingestion.
- Account/plugin/workspace ownership context must be preserved.
- Monitoring state and important records must survive application restarts.
- Telemetry retention must be bounded and configurable.
- Shutdown must use bounded timeouts.
- Operational access must go through ControlAPI, not direct UI access to Overseer internals.

## Integration requirements

- Integrate with `core/bootstrap.py`.
- Integrate with `ControlAPI`.
- Reuse the existing Event Bus where appropriate.
- Reuse existing persistence infrastructure where appropriate.
- Integrate existing managers through adapters or explicit contracts.
- Ensure existing services can report health and lifecycle state.
- Do not create a parallel event bus, persistence system, rate limiter or authorization system.
- Keep the future Task Runtime integration straightforward without implementing the Task Runtime in this assignment.

## Incident management

Support structured incidents with:
- Unique IDs.
- Severity and category.
- Affected component.
- Timestamps.
- Related correlation IDs.
- Evidence references.
- Acknowledgment and resolution states.
- Auditable incident transitions.

Automatic recovery or destructive remediation must not be enabled by default.

## Testing requirements

Use pytest.

Tests must cover:
- Component registration and deregistration.
- Health state transitions.
- Lifecycle state transitions.
- Failure detection and recovery.
- Security event collection.
- Account/plugin ownership violations.
- Data flow correlation across multiple components.
- Concurrent event ingestion.
- Persistence across restart.
- Telemetry retention.
- Monitoring subsystem failure isolation.
- Bootstrap integration.
- ControlAPI integration and access control.
- Clean shutdown without leaked tasks.

Use deterministic tests and isolated databases. Avoid live Discord dependencies.

## Performance and reliability

Measure monitoring overhead rather than assuming it is negligible.

Implement:
- Bounded telemetry queues.
- Backpressure behavior.
- Event prioritization for critical security events.
- Controlled persistence writes.
- Graceful degradation.
- Bounded shutdown and flush behavior.

Critical security records must not be silently discarded when ordinary telemetry is under pressure. Any loss or persistence failure must be observable.

## Explicit exclusions

Do not implement:
- Task execution or scheduling.
- Workspace chain execution.
- Rate limiting.
- Authentication or authorization replacement.
- Plugin configuration management.
- Account credential encryption.
- Application entry-point replacement.
- A second event bus or duplicate infrastructure manager.

## Delivery sequence

1. Repository audit and responsibility map.
2. Component contracts and registry.
3. Lifecycle and health monitoring.
4. Security monitoring.
5. Data flow monitoring.
6. Persistence and incident management.
7. ControlAPI integration.
8. Bootstrap integration.
9. Full pytest verification.
10. Documentation and architectural integration report.

## Completion criteria

The implementation is complete only when Overseer can provide an accurate, persistent, and queryable view of suite-wide health, security, data flow and lifecycle, with demonstrable isolation from the components it monitors.

Provide:
- Architecture and responsibility map.
- Files changed and reasons.
- Public contracts and APIs.
- Lifecycle and data flow diagrams.
- Test results.
- Performance measurements.
- Known limitations.
- Security considerations.
- Future Task Runtime integration instructions.

Do not implement speculative abstractions or duplicate existing functionality. Prioritize correctness, clear ownership, testability, and minimal coupling.
