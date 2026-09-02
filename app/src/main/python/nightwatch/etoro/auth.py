"""eToro authentication headers (DESIGN §1.1).

# TODO(step 2): Build a small helper that, given a
# ``nightwatch.config.Credentials`` and a per-request UUID, returns the three
# headers eToro requires: ``x-api-key``, ``x-user-key``, ``x-request-id``.
# There is no signing and no nonce (DESIGN §1.1) — this module's only job is to
# assemble the headers consistently and never let one leak into a log line
# (repr()/str() must stay empty; rely on nightwatch.logging_setup's redaction
# filter as a second line of defense, not the only one).
"""
