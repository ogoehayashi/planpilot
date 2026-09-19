# Implementation tasks

- [x] Add regional Converse bearer client and persistent bounded usage accounting.
- [x] Add authenticated chat/status endpoints using the compact deterministic pipeline.
- [x] Wire browser messages and real execution traces to the endpoints.
- [x] Add opt-in startup configuration with external credential-file support.
- [x] Run offline regression tests and document the live deployment check.
- [x] Restore the contract-pinned Claude Sonnet 4.5 global inference profile.
- [x] Fail closed on a non-contract model override and remove parallel provider I/O.
- [x] Align startup, environment, container, package and operator documentation.
- [x] Add an offline no-Nova regression guard without calling Bedrock.

Live AWS model access and browser testing are deployment checks, not completed
by the offline verification. See docs/bedrock-web-guide.md.
