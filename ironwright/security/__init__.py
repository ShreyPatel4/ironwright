"""Tier 1 auth and serving controls from docs/enterprise-security.md.

store: the side tables. cookies: the session cookie and its CSRF token. headers:
the response headers. ratelimit: per IP doors and the per email soft lock.
passwords: the policy and the breached list. outbox: mail. mfa: TOTP and recovery
codes. tokens: org API tokens. routes: the /auth routes.
"""
