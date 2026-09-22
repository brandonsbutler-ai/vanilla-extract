# Releasing vanilla-extract

Written 2026-09-21, against `vanilla-extract` 0.2.0, alongside the first CI
this repository has had. Everything below about the PyPI side and about the
publishing action was read from live documentation on that date; where a
detail could not be confirmed it says so rather than guessing.

The short version: the release workflow publishes with OpenID Connect, so
there is no PyPI token in this repository, in its secrets, or on your laptop.
A tag is the release. Everything else is setup you do once.

---

## 1. What the workflows do

`.github/workflows/ci.yml` runs on pushes to `main` and on pull requests. It
runs the three checks this repository already had:

```
python -m unittest discover -s tests -q
python verify_e2e.py
python packaging/verify_dist.py --dist dist/
```

`.github/workflows/release.yml` runs on a pushed tag matching `v*`, and by
hand from the Actions tab. It runs the same three checks, builds the sdist and
the wheel, attaches them to the run, and only then publishes.

The publish is a separate job. It never checks the source out; it downloads
the files the build job produced. It is the only job that holds
`id-token: write`, and it runs inside a named GitHub environment, which is the
thing PyPI's trusted publisher is registered against.

---

## 2. One-time setup: PyPI

### 2a. The trusted publisher

`vanilla-extract` does not exist on PyPI yet, so this is a **pending
publisher**: a registration made before the project exists, which becomes the
project's normal publisher the first time it is used. PyPI's own wording:

> A "pending" publisher does **not** create a project or reserve a project's
> name **until** it is actually used to publish.

So the name is not held for you. If somebody else registers `vanilla-extract`
first, the pending publisher is invalidated and you find out at upload time.
The name was free on both indexes when this was written: `pypi.org/pypi/
vanilla-extract/json` and the same path on `test.pypi.org` both answered 404
on 2026-09-21. That is a fact with a shelf life; re-check it before you rely
on it.

Go to **<https://pypi.org/manage/account/publishing/>** — this lives under
your *account* sidebar ("Publishing"), not under a project, because there is
no project yet. Choose the GitHub tab and fill in:

| Field on the form | Value to enter |
| --- | --- |
| `PyPI Project Name` | `vanilla-extract` |
| `Owner` | `brandonsbutler-ai` |
| `Repository name` | `vanilla-extract` |
| `Workflow name` | `release.yml` |
| `Environment name` | `pypi` |

Two things worth slowing down for:

- **`Workflow name` wants the filename, not a path.** The form's own help text
  reads: "The filename of the publishing workflow. This file should exist in
  the `.github/workflows/` directory in the repository configured above." So
  `release.yml`, not `.github/workflows/release.yml`.
- **`Environment name` is optional on the form and required by these
  workflows.** PyPI calls a dedicated environment "strongly" encouraged;
  `release.yml` declares `environment: pypi` on the publish job, so leaving
  this blank on the form and filled in the workflow is a mismatch that fails
  at upload.

Once the first publish succeeds, PyPI converts the pending publisher into a
normal one. Nothing further is needed, and the registration then lives under
the project: **Your projects → vanilla-extract → Manage → Publishing**.

### 2b. TestPyPI is a separate account and a separate registration

TestPyPI is a different site with different accounts. Register a second,
independent pending publisher at
**<https://test.pypi.org/manage/account/publishing/>** with the same values
except the environment:

| Field on the form | Value to enter |
| --- | --- |
| `PyPI Project Name` | `vanilla-extract` |
| `Owner` | `brandonsbutler-ai` |
| `Repository name` | `vanilla-extract` |
| `Workflow name` | `release.yml` |
| `Environment name` | `testpypi` |

---

## 3. One-time setup: GitHub

In the repository, **Settings → Environments**, create two environments named
exactly `pypi` and `testpypi`. The names must match the workflow and the PyPI
registration character for character.

Creating them explicitly (rather than letting a workflow run reference them)
is what lets you attach protection rules. Two worth considering, both aimed at
the risk PyPI names in its security model — "anybody who can unconditionally
commit to your repository can also modify your publishing workflow to make it
trigger on events you may not intend":

- a **required reviewer** on the `pypi` environment, so a publish to the real
  index pauses until you approve it;
- a **deployment branch and tag rule** on the `pypi` environment limiting it
  to tags matching `v*`.

Neither is required for the upload to work. Both convert "someone pushed
something" into "someone pushed something and I said yes".

No repository secret is needed. If you ever find a `PYPI_API_TOKEN` secret in
this repository's settings, it is left over from something else and should be
deleted — nothing here reads it.

---

## 4. Cutting a release

1. Set the version in `pyproject.toml` (`[project] version`). It is a literal
   there, not read from the package.
2. Fix the two places that still say this package is not on PyPI, or the first
   release ships a README that contradicts itself:
   - `README.md`, in the "Install it" section: "vanilla-extract is not
     published on PyPI."
   - `pyproject.toml`, the comment above the `gui` extra: "the package is not
     on PyPI".
3. Run the three checks locally first. CI is the second opinion, not the
   first:
   ```
   python -m unittest discover -s tests -q
   python verify_e2e.py
   python -m build && python packaging/verify_dist.py --dist dist/
   ```
   `verify_dist.py` needs Python 3.11.4 or newer and the `build` and `twine`
   modules.
4. Commit, push `main`, and let `ci` go green.
5. Tag and push the tag. The tag is what publishes, and it must carry the same
   number as `pyproject.toml`:
   ```
   git tag -a v0.2.0 -m "vanilla-extract 0.2.0"
   git push origin v0.2.0
   ```
   Push to **`origin`**. This checkout also has a `local` remote pointing at a
   bare repository on disk; pushing the tag there publishes nothing.
   The build job re-checks the tag against the declared version and fails the
   run rather than publishing a mislabelled file.
6. Watch it: **Actions → release**, or `gh run watch`. If you put a required
   reviewer on the `pypi` environment, the run will wait for you there.
7. Confirm at <https://pypi.org/p/vanilla-extract> and by installing it
   somewhere clean:
   ```
   python -m pip install --no-cache-dir vanilla-extract
   vanilla --help
   ```

If a tag was wrong, delete it and tag again — but only **before** a successful
publish. PyPI does not accept the same filename twice, so once a version is
uploaded, the fix is the next version number, never a re-upload.

---

## 5. TestPyPI dry run

Use this the first time, and any time you have changed packaging metadata.

1. **Actions → release → Run workflow**.
2. Leave the `index` input on its default, `testpypi`.
3. The build job runs exactly as it would for a real release; the publish goes
   to <https://test.pypi.org/p/vanilla-extract>.

Then install from TestPyPI in a throwaway environment. TestPyPI does not
mirror PyPI, so anything a package needs from the real index has to come from
the real index — for a package with no runtime dependencies, like this one,
that only matters for build tooling:

```
python -m venv /tmp/tp && /tmp/tp/bin/python -m pip install \
  --index-url https://test.pypi.org/simple/ \
  --extra-index-url https://pypi.org/simple/ \
  vanilla-extract
/tmp/tp/bin/vanilla --help
```

TestPyPI keeps the same rule about filenames: a version uploaded there is
spent there too. Version numbers used up on TestPyPI are not a problem —
nobody depends on that index — but you cannot re-upload over one.

---

## 6. Fallback: publishing by hand with twine

For when GitHub is unreachable, Actions is degraded, or you need to ship from
a machine that is not going through CI. This path uses a PyPI API token, which
is a long-lived credential — prefer the workflow, and revoke the token when
you are done with it.

**The token never goes in a file in this repository.** Not `.pypirc`, not
`.env`, not a shell script. It is read into the environment for one command
and unset. Note that this repository's `.gitignore` covers build litter only;
it has no `.env` or `*.pem` rule, so there is nothing standing between a
credential written to a file here and a careless `git add -A`. The rule below
is therefore the whole protection: the token never gets written down.

1. Create a token: PyPI **Account settings → API tokens → Add API token**
   (<https://pypi.org/manage/account/token/>). Scope it to the
   `vanilla-extract` project if the project exists; account-wide is the only
   option before the first publish. The token value starts with `pypi-` and is
   shown once.
2. Build and verify:
   ```
   python -m pip install --upgrade build twine
   python -m build
   python packaging/verify_dist.py --dist dist/
   ```
   Do not skip `verify_dist.py` because you are in a hurry. Manual publishing
   is exactly when nothing else is checking.
3. Upload, reading the token from a prompt so it never enters the command line
   or the shell history:
   ```
   export TWINE_USERNAME=__token__
   read -rs -p "PyPI token: " TWINE_PASSWORD; echo; export TWINE_PASSWORD
   python -m twine upload --repository-url https://upload.pypi.org/legacy/ dist/*
   unset TWINE_PASSWORD
   ```
   `__token__` is the literal username PyPI expects for an API token; the
   token value, `pypi-` prefix included, is the password.
4. For TestPyPI instead, the same shape with a TestPyPI token and
   `--repository-url https://test.pypi.org/legacy/`.
5. Revoke the token afterwards if this was a one-off.

A hand upload produces **no PEP 740 attestations**. The action generates and
uploads those automatically, and only for Trusted Publishing flows; a token
upload cannot. That is one more reason this is the fallback and not the
routine.

---

## 7. Known gaps, recorded rather than hidden

- **The untested 3.9 floor is closed as of 2026-09-22.** `pyproject.toml` used
  to declare `requires-python = ">=3.9"` and the README repeated "Python 3.9 or
  later is the only requirement", while nothing had ever been run below 3.11:
  `verify_e2e.py` imports `tomllib` without a guard at lines 1158, 1357 and
  1545, `packaging/verify_dist.py` imports it at line 188, and
  `tests/test_vanilla_extract.py` imports it without a guard inside
  `test_installers_agree_on_the_command_name` at line 2031. `tomllib` arrived
  in 3.11, so neither the unit tests nor the claim checkers could start on the
  versions the package said it supported. The declaration now says `>=3.11`,
  which is where the CI matrix already began. The alternative — guarding those
  imports and widening the matrix — was available and was not taken: an
  untested compatibility claim is worse than a narrower tested one.
- **`ubuntu-latest` resolved to Ubuntu 24.04 on 2026-09-21.** If GitHub moves
  that label to 26.04, an older Python in the matrix may stop being available
  and the job will need the explicit `ubuntu-24.04` label.
- **`.github/` does not reach the sdist, and was measured, not assumed.** An
  sdist built from this tree on 2026-09-21 contains no `.git*` member of any
  kind. `MANIFEST.in` adds only named files and `tests/` and `packaging/`, so
  the workflows stay out; `packaging/verify_dist.py` would also reject them,
  since its forbidden-member list includes `(^|/)\.git`.
