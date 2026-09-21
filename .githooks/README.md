# The push guard

A `pre-push` hook that refuses to publish a machine-specific path, a
credential, or a name that is not meant to be public.

## Why it exists

A history rewrite removed a leaked absolute path from this repository.
Forty minutes later the same class of path was committed again, in a file
that had not existed when the rewrite ran. A rewrite is a point-in-time
operation and nothing was watching afterwards. This watches.

## What it reads

Four surfaces, not a diff:

- the blobs the pushed commits introduce
- the commit messages in the pushed range
- the whole tree at the tip
- annotated tag messages

A new branch, or any tag push, gets everything the remote does not already
hold. That is the case that carried the last leak into the release tags
while the working tree looked clean, and it is why a diff-only check would
have missed it.

## Categories

Absolute paths that identify one machine; credentials and key material;
the tool's own output files, which embed the source of whatever was
surveyed; and a list of private names kept in the hook itself.

## Exemptions

An allowlist entry must match the rule, the path and the exact text before
it suppresses anything, so a single line cannot switch off a category.
When the guard blocks, it prints the line that would allow the finding.

To override once, with a reason that gets logged:

    PUSH_GUARD_OVERRIDE='why this is safe' git push ...

## Audit mode

Run it over every object, not just a push. Do this after any history
rewrite and before cutting a tag:

    python3 .githooks/pre-push --all-history --allow .githooks/push-guard-allow.txt

## What it does not do

`git push --no-verify` skips it. Each clone must set `core.hooksPath`
itself -- committing the hook makes the file survive a clone, not run.
The hook and the allowlist are not scanned for content, so read them
rather than trusting them. It is blind to anything binary, compressed or
encoded, and to a leak that is prose rather than a pattern -- a customer
name or an internal hostname in a sentence is still a reviewer's job.

And it does not un-publish anything. A force-push leaves the old objects
served by SHA until the host is asked to collect them.

It is a seatbelt, not a lock.
