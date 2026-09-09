# Legacy hooks

This tree is retained for reference and is no longer executed by the builder.
The active hook root is `configs/hooks`, with exactly three phases:
`pre-chroot`, `chroot` and `post-chroot`.

See [the hook documentation](../docs/hooks.md) for execution order, context,
custom roots and error handling. The builder no longer requires or creates
`<phase>.d` directories. Internal artifact checks and resource cleanup are
handled by the orchestrator independently of user hooks.
