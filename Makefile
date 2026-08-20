.PHONY: sync lock-check browsers lint fmt typecheck test test-browser test-all build \
	check-wheel check-floor check schema-check version check-version release-notes \
	publish clean release-patch release-minor release-major

# Every CI step is one of these targets; the workflows only call make.

sync:
	uv sync --all-groups

lock-check:
	uv lock --check

# The Chromium the checker drives. CI passes PLAYWRIGHT_ARGS=--with-deps for the
# system libraries a bare runner lacks.
browsers:
	uv run playwright install $(PLAYWRIGHT_ARGS) chromium

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run pyright

# Contract tests that need no browser: usage errors, --version, the schema command.
test:
	uv run pytest -q -m "not browser"

# Exports the fixture apps with a pinned shinylive and runs the CLI against them in
# headless Chromium: the green app passes, each broken one fails for its own reason.
# Needs `make browsers` once, uvx on PATH, and network for the first shinylive export.
test-browser:
	uv run pytest -q -m browser

test-all: test test-browser

build:
	rm -rf dist
	uv build

# Installs the freshly built wheel into a throwaway venv and runs the non-browser suite
# against it, so what ships (py.typed and the entry point included) is what was tested,
# not the editable checkout.
check-wheel: build
	rm -rf .wheel-venv
	uv venv --quiet .wheel-venv
	uv pip install --quiet --python .wheel-venv/bin/python dist/*.whl pytest
	.wheel-venv/bin/python -c "import shinylive_check; print('shinylive_check', shinylive_check.__version__)"
	cd tests && ../.wheel-venv/bin/python -m pytest -q -p no:cacheprovider -m "not browser" \
		--rootdir=.. -c ../pyproject.toml .
	rm -rf .wheel-venv

# Installs the package with playwright at the oldest version pyproject.toml allows
# (uv's lowest-direct resolution) into a throwaway venv and runs the whole suite,
# browser included, so the declared bound is tested rather than hoped. Installs the
# Chromium that playwright version wants.
check-floor:
	rm -rf .floor-venv
	uv venv --quiet .floor-venv
	uv pip install --quiet --python .floor-venv/bin/python --resolution lowest-direct .
	uv pip install --quiet --python .floor-venv/bin/python pytest
	.floor-venv/bin/python -c "from importlib.metadata import version; print('playwright', version('playwright'))"
	.floor-venv/bin/python -m playwright install $(PLAYWRIGHT_ARGS) chromium
	cd tests && ../.floor-venv/bin/python -m pytest -q -p no:cacheprovider \
		--rootdir=.. -c ../pyproject.toml .
	rm -rf .floor-venv

check: lock-check lint typecheck test test-browser check-wheel check-floor

# Validates the `schema` output against The CLI Spec, when clispec is on PATH.
# Requires clispec in PATH (cargo install clispec); not part of `check` so CI
# does not need a Rust toolchain. Run before releasing.
schema-check: sync
	@mkdir -p tmp
	clispec score .venv/bin/shinylive-check -o json > tmp/clispec.json
	@python3 -c "import json,sys; d=json.load(open('tmp/clispec.json')); \
	print(f\"clispec: {d['score']}/{d['max']} {d['grade']}\"); \
	sys.exit(0 if d['grade'] == 'Excellent' else 1)"

# The version in pyproject.toml, the single source for the package version.
version:
	@sed -n 's/^version = "\(.*\)"$$/\1/p' pyproject.toml

# Fails unless pyproject.toml carries VERSION; the release workflow runs it with the tag.
check-version:
	@test -n "$(VERSION)" || { echo "usage: make check-version VERSION=X.Y.Z" >&2; exit 2; }
	@v="$$($(MAKE) -s version)"; test "$$v" = "$(VERSION)" \
		|| { echo "pyproject.toml is $$v, expected $(VERSION)" >&2; exit 1; }

# Writes the CHANGELOG.md section for VERSION to tmp/release-notes.md; that text becomes
# the GitHub release body. Fails when the section is missing or empty.
release-notes:
	@test -n "$(VERSION)" || { echo "usage: make release-notes VERSION=X.Y.Z" >&2; exit 2; }
	@mkdir -p tmp
	@awk -v v="$(VERSION)" '\
		/^## \[/ { on = index($$0, "## [" v "]") == 1; next } \
		on && /^\[/ && /\]: / { next } \
		on { print }' CHANGELOG.md > tmp/release-notes.md
	@grep -q '[^[:space:]]' tmp/release-notes.md \
		|| { echo "CHANGELOG.md has no section for $(VERSION)" >&2; exit 1; }

# Uploads dist/* to PyPI through trusted publishing (GitHub Actions OIDC; no token).
# --check-url skips files the index already has, so a re-run after a partial failure
# does not fail on what already landed.
publish:
	uv publish --trusted-publishing always --check-url https://pypi.org/simple/shinylive-check/ dist/*

clean:
	rm -rf dist tmp .wheel-venv .floor-venv .pytest_cache .ruff_cache

release-patch:
	vership bump patch

release-minor:
	vership bump minor

release-major:
	vership bump major
